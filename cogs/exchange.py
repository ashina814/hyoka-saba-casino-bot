"""両替(ゼニー ↔ カジノコイン)。

設計:
- ゼニーは別Botの通貨でこのBotから操作できないため、
  「申請 → 運営承認(目視) → 発行(or 手動送付) → DM通知」のワークフローとする。
- カジノコイン → ゼニー の場合、申請時にカジノコインを即時エスクロー(差引)し、
  承認で焼却(差引のままにする)、拒否/期限切れで返金する。
- ゼニー → カジノコイン は、お釈迦さま(OWNER_ID)へユーザーが直接ゼニーを送付し、
  運営が目視確認の上で承認することでカジノコインを発行する。
  (拒否時はBot側からゼニーを返せないため、運営判断に委ねる旨を案内する)
- 手数料は受け取り側から控除(等価レートで 1:1、10% カット)。
- 日次上限は方向ごとに受領額ベースで管理。
- 申請は exchange_request_ttl_hours で自動失効、起動時と定期で sweep。
"""
from __future__ import annotations

import asyncio
import logging
import math

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db.dao import InsufficientFunds
from ui import common

log = logging.getLogger("casino.exchange")

DIR_Z2C = "zeny_to_coin"   # ゼニー → カジノコイン
DIR_C2Z = "coin_to_zeny"   # カジノコイン → ゼニー

DIR_LABEL = {
    DIR_Z2C: "ゼニー → カジノコイン",
    DIR_C2Z: "カジノコイン → ゼニー",
}


def _calc(send_amount: int, fee_rate: float) -> tuple[int, int]:
    """送額から受領額と手数料を算出(受け取り側から控除)。"""
    receive = int(math.floor(send_amount * (1 - fee_rate)))
    fee = send_amount - receive
    return receive, fee


# ───────────────────────── 承認View(永続) ─────────────────────────
class ApprovalView(discord.ui.View):
    """承認チャンネルに送る、承認/拒否ボタン付き View。

    custom_id に req_id を埋め込んで永続化。Bot 再起動後も動くよう、
    起動時に Cog の on_ready で再登録する。
    """

    def __init__(self, cog: "ExchangeCog", req_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        # ボタンを動的に追加(custom_id に req_id を含める)
        self.add_item(self._ApproveButton(req_id))
        self.add_item(self._RejectButton(req_id))

    class _ApproveButton(discord.ui.Button):
        def __init__(self, req_id: int | None) -> None:
            super().__init__(
                label="承認",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"ex:approve:{req_id}" if req_id else "ex:approve:?",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            view: ApprovalView = self.view  # type: ignore[assignment]
            req_id = int(self.custom_id.rsplit(":", 1)[1])  # type: ignore[union-attr]
            await view.cog.decide(interaction, req_id, approved=True)

    class _RejectButton(discord.ui.Button):
        def __init__(self, req_id: int | None) -> None:
            super().__init__(
                label="拒否",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"ex:reject:{req_id}" if req_id else "ex:reject:?",
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            view: ApprovalView = self.view  # type: ignore[assignment]
            req_id = int(self.custom_id.rsplit(":", 1)[1])  # type: ignore[union-attr]
            await view.cog.decide(interaction, req_id, approved=False)


# ───────────────────────── 金額入力モーダル ─────────────────────────
class AmountModal(discord.ui.Modal):
    def __init__(self, cog: "ExchangeCog", direction: str) -> None:
        title = f"両替申請 — {DIR_LABEL[direction]}"
        super().__init__(title=title)
        self.cog = cog
        self.direction = direction
        send_label = "送るゼニー" if direction == DIR_Z2C else "焼くカジノコイン"
        self.amount = discord.ui.TextInput(
            label=send_label,
            placeholder="例: 1000 / 1k / 1.5万",
            required=True,
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = common.parse_bet(str(self.amount.value))
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await self.cog.submit_request(interaction, self.direction, value)


# ───────────────────────── 両替パネル(永続) ─────────────────────────
class ExchangePanel(discord.ui.View):
    def __init__(self, cog: "ExchangeCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="ゼニー → カジノ", emoji="💱",
                       style=discord.ButtonStyle.success,
                       custom_id="ex:open:z2c")
    async def z2c(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AmountModal(self.cog, DIR_Z2C))

    @discord.ui.button(label="カジノ → ゼニー", emoji="🪙",
                       style=discord.ButtonStyle.primary,
                       custom_id="ex:open:c2z")
    async def c2z(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AmountModal(self.cog, DIR_C2Z))

    @discord.ui.button(label="ルール", emoji="❓",
                       style=discord.ButtonStyle.secondary,
                       custom_id="ex:open:rule")
    async def rule(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=self.cog.rule_embed(), ephemeral=True
        )


# ───────────────────────── Cog 本体 ─────────────────────────
class ExchangeCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._sweep_started = False

    # ── ライフサイクル ──
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # 永続 View を再登録(Bot再起動後もボタンが効くように)
        self.bot.add_view(ExchangePanel(self))
        self.bot.add_view(ApprovalView(self))   # custom_id ベースで再ルーティング
        if not self._sweep_started:
            self.sweep_expired.start()
            self._sweep_started = True

    def cog_unload(self) -> None:
        try:
            self.sweep_expired.cancel()
        except Exception:  # noqa: BLE001
            pass

    # ── ヘルプ Embed (クライアント向け説明文) ──
    def rule_embed(self) -> discord.Embed:
        db = self.bot.db
        fee_pct = int(round(float(db.setting("exchange_fee_rate", 0.10)) * 100))
        cap = int(db.setting("exchange_daily_cap", 50000))
        ttl = int(db.setting("exchange_request_ttl_hours", 48))
        owner_id = int(db.setting("owner_id", 0) or 0)
        owner_mention = f"<@{owner_id}>" if owner_id else "**未設定**"
        body = (
            "**等価レート 1:1**(1ゼニー = 1カジノコイン)。\n"
            f"**手数料 {fee_pct}%** は **受け取り側から控除** します。\n"
            f"**日次上限**: 方向ごとに **受領 {cap:,}** まで(超過分は翌日)。\n"
            f"申請の有効期限は **{ttl}時間**、過ぎたら自動失効します。\n"
            "\n"
            "**🔁 ゼニー → カジノコイン**\n"
            f"1. お釈迦さま({owner_mention}) に **送るゼニー額**を送付\n"
            "2. このパネルから **同じ額** で申請\n"
            "3. 運営が受領確認 → 承認 → 受領額のカジノコインが発行\n"
            "4. 結果は DM で通知されます\n"
            "（拒否時はゼニーをBotから返せません。運営判断に委ねます）\n"
            "\n"
            "**🔁 カジノコイン → ゼニー**\n"
            "1. パネルから **焼くカジノコイン額** を申請\n"
            "2. 申請の瞬間、そのカジノコインはエスクロー(差引)されます\n"
            "3. 運営が承認 → 別Botで **受領額のゼニー** を送付\n"
            "4. 承認時にエスクローしたコインは **焼却**(消滅)\n"
            "5. 拒否/失効時は **エスクロー額を全額返金**\n"
        )
        e = common.embed("💱 両替について", body, color=common.COLOR_INFO)
        return e

    # ── パネル表示 ──
    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=self.panel_embed(), view=ExchangePanel(self), ephemeral=True
        )

    def panel_embed(self) -> discord.Embed:
        db = self.bot.db
        fee_pct = int(round(float(db.setting("exchange_fee_rate", 0.10)) * 100))
        cap = int(db.setting("exchange_daily_cap", 50000))
        enabled = bool(db.setting("exchange_enabled", True))
        owner_id = int(db.setting("owner_id", 0) or 0)
        log_id = int(db.setting("exchange_log_channel_id", 0) or 0)

        e = common.embed("💱 両替", color=common.COLOR_MAIN)
        e.description = (
            f"等価交換・手数料 **{fee_pct}%**(受領側控除)・日次 **{cap:,}**(方向別)"
        )
        if not enabled:
            e.add_field(name="状態", value="🛑 現在停止中", inline=False)
        else:
            problems = []
            if not owner_id:
                problems.append("お釈迦さま未設定")
            if not log_id:
                problems.append("承認チャンネル未設定")
            if problems:
                e.add_field(
                    name="⚠️ 利用不可",
                    value="運営: " + " / ".join(problems),
                    inline=False,
                )
        e.add_field(
            name="ボタン",
            value=(
                "💱 **ゼニー → カジノ** — 先にお釈迦さまへ送ってから申請\n"
                "🪙 **カジノ → ゼニー** — 申請即エスクロー、承認で焼却\n"
                "❓ **ルール** — 詳細・手順を表示"
            ),
            inline=False,
        )
        return e

    # ── 申請作成 ──
    async def submit_request(
        self, interaction: discord.Interaction, direction: str, send_amount: int
    ) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        user = interaction.user

        # 機能ON/OFF・設定済みチェック
        if not db.setting("exchange_enabled", True):
            await interaction.response.send_message(
                "🛑 両替は現在停止中です。", ephemeral=True
            )
            return
        owner_id = int(db.setting("owner_id", 0) or 0)
        log_channel_id = int(db.setting("exchange_log_channel_id", 0) or 0)
        if not owner_id or not log_channel_id:
            await interaction.response.send_message(
                "⚠️ 運営側の初期設定(お釈迦さま/承認チャンネル)が未完了です。",
                ephemeral=True,
            )
            return

        if send_amount <= 0:
            await interaction.response.send_message(
                "⚠️ 1以上の数値で入力してください。", ephemeral=True
            )
            return

        if await db.is_frozen(user.id):
            await interaction.response.send_message(
                "🧊 凍結中は両替を申請できません。", ephemeral=True
            )
            return

        fee_rate = float(db.setting("exchange_fee_rate", 0.10))
        receive, fee = _calc(send_amount, fee_rate)
        if receive <= 0:
            await interaction.response.send_message(
                "⚠️ 受領額が0以下になります。もう少し大きい額で申請してください。",
                ephemeral=True,
            )
            return

        # 日次上限(受領額ベース・方向別)
        cap = int(db.setting("exchange_daily_cap", 50000))
        already = await db.daily_exchange_received(user.id, direction)
        if already + receive > cap:
            remain = max(0, cap - already)
            await interaction.response.send_message(
                f"⚠️ 日次上限({cap:,})に達します。本日この方向で残り受領可能額は "
                f"**{remain:,}** です。",
                ephemeral=True,
            )
            return

        # カジノ→ゼニー: ここで即エスクロー差引
        if direction == DIR_C2Z:
            async with db.user_lock(user.id):
                try:
                    await db.adjust_balance(
                        user.id, -send_amount, "exchange_escrow",
                        ref=None,  # ref はあとで申請ID で再ログしてもよいが簡素化
                    )
                except InsufficientFunds:
                    await interaction.response.send_message(
                        "残高が足りません。", ephemeral=True
                    )
                    return

        # 申請レコード作成
        req_id = await db.create_exchange_request(
            user.id, direction, send_amount, receive, fee
        )

        # 承認チャンネルへ送信
        ch = self.bot.get_channel(log_channel_id)
        if ch is None:
            # 取得できないので fetch を試みる
            try:
                ch = await self.bot.fetch_channel(log_channel_id)
            except (discord.NotFound, discord.Forbidden):
                ch = None
        if ch is None:
            # 承認チャンネルが取れないので申請を取り消し(エスクローも返金)
            await self._rollback_creation(user.id, direction, send_amount, req_id)
            await interaction.response.send_message(
                "⚠️ 承認チャンネルにアクセスできませんでした。運営にご連絡ください。",
                ephemeral=True,
            )
            return

        embed = self._approval_embed(req_id, user, direction, send_amount, receive, fee)
        try:
            msg = await ch.send(embed=embed, view=ApprovalView(self, req_id))
        except discord.HTTPException:
            await self._rollback_creation(user.id, direction, send_amount, req_id)
            await interaction.response.send_message(
                "⚠️ 承認チャンネルへの送信に失敗しました。", ephemeral=True
            )
            return
        await db.attach_exchange_message(req_id, msg.channel.id, msg.id)

        # ユーザーへの ephemeral 案内
        if direction == DIR_Z2C:
            note = (
                f"🪙 お釈迦さま <@{owner_id}> に **{send_amount:,} ゼニー** を送ってください。\n"
                f"運営が受領を確認して承認すると、**{receive:,} カジノコイン** が発行されます。\n"
                f"結果は DM でお知らせします(申請ID: `#{req_id}`)。"
            )
        else:
            note = (
                f"🔒 **{send_amount:,} カジノコイン** をエスクローしました。\n"
                f"運営が承認すると、別Botで **{receive:,} ゼニー** が送付されます。\n"
                f"拒否・失効時は全額返金されます(申請ID: `#{req_id}`)。"
            )
        await interaction.response.send_message(
            embed=common.embed(
                "💱 両替申請を受け付けました", note, color=common.COLOR_INFO
            ),
            ephemeral=True,
        )

    async def _rollback_creation(
        self, user_id: int, direction: str, send_amount: int, req_id: int
    ) -> None:
        """承認チャンネル送信失敗時の巻き戻し: エスクロー返金 + 申請キャンセル。"""
        db = self.bot.db
        if direction == DIR_C2Z:
            async with db.user_lock(user_id):
                await db.adjust_balance(
                    user_id, send_amount, "exchange_refund", ref=str(req_id)
                )
        await db.set_exchange_status(req_id, "cancelled", None)

    def _approval_embed(
        self, req_id: int, user, direction: str, send: int, receive: int, fee: int
    ) -> discord.Embed:
        cfg = self.bot.cfg
        e = common.embed(
            f"🆕 両替申請 #{req_id}",
            f"**方向:** {DIR_LABEL[direction]}",
            color=common.COLOR_ADMIN,
        )
        e.add_field(name="申請者", value=f"<@{user.id}> (`{user.id}`)", inline=False)
        send_unit = "ゼニー" if direction == DIR_Z2C else "カジノコイン"
        recv_unit = "カジノコイン" if direction == DIR_Z2C else "ゼニー"
        e.add_field(name=f"送る({send_unit})", value=f"{send:,}")
        e.add_field(name=f"受領({recv_unit})", value=f"{receive:,}")
        e.add_field(name=f"手数料({send_unit})", value=f"{fee:,}")
        if direction == DIR_Z2C:
            owner_id = int(self.bot.db.setting("owner_id", 0) or 0)
            e.set_footer(
                text=f"承認前にお釈迦さま (id={owner_id}) のゼニー受領を確認してください。"
            )
        else:
            e.set_footer(
                text="承認したら別Botで受領額のゼニーをユーザーへ送付してください。"
            )
        return e

    # ── 承認/拒否 ──
    async def decide(
        self, interaction: discord.Interaction, req_id: int, approved: bool
    ) -> None:
        bot = self.bot
        db = self.bot.db
        # 権限チェック
        if not common.is_admin(bot, interaction.user):
            await interaction.response.send_message(
                "🚫 承認権限がありません。", ephemeral=True
            )
            return

        row = await db.get_exchange_request(req_id)
        if row is None:
            await interaction.response.send_message(
                "⚠️ 申請が見つかりません。", ephemeral=True
            )
            return
        if row["status"] != "pending":
            await interaction.response.send_message(
                f"⚠️ この申請は既に **{row['status']}** です。", ephemeral=True
            )
            return

        await interaction.response.defer()
        user_id = int(row["user_id"])
        direction = row["direction"]
        send_amount = int(row["send_amount"])
        receive = int(row["receive_amount"])

        if approved:
            # 承認処理
            if direction == DIR_Z2C:
                # カジノコインを発行(ゼニーは既にお釈迦さま側=Bot外)
                async with db.user_lock(user_id):
                    await db.adjust_balance(
                        user_id, receive, "exchange_in", ref=str(req_id)
                    )
            else:  # DIR_C2Z
                # エスクロー額はそのまま消滅(運営は別Botでゼニーを手動送付)
                # Bot側で追加処理はなし(差引は申請時に済んでいる)
                pass
            await db.set_exchange_status(req_id, "approved", interaction.user.id)
            await db.log_admin(
                interaction.user.id, "exchange_approve", user_id,
                f"req={req_id} dir={direction} recv={receive}",
            )
            await self._update_approval_message(row, "approved", interaction.user.id)
            await self._dm_user(
                user_id,
                title="✅ 両替が承認されました",
                desc=self._decided_user_message(direction, send_amount, receive, True),
                color=common.COLOR_WIN,
            )
            await interaction.followup.send(
                f"✅ 申請 #{req_id} を承認しました。", ephemeral=True
            )
        else:
            # 拒否処理
            if direction == DIR_C2Z:
                # エスクロー返金
                async with db.user_lock(user_id):
                    await db.adjust_balance(
                        user_id, send_amount, "exchange_refund", ref=str(req_id)
                    )
            await db.set_exchange_status(req_id, "rejected", interaction.user.id)
            await db.log_admin(
                interaction.user.id, "exchange_reject", user_id,
                f"req={req_id} dir={direction}",
            )
            await self._update_approval_message(row, "rejected", interaction.user.id)
            await self._dm_user(
                user_id,
                title="🚫 両替が拒否されました",
                desc=self._decided_user_message(direction, send_amount, receive, False),
                color=common.COLOR_LOSE,
            )
            await interaction.followup.send(
                f"🚫 申請 #{req_id} を拒否しました。", ephemeral=True
            )

    def _decided_user_message(
        self, direction: str, send: int, receive: int, approved: bool
    ) -> str:
        if approved:
            if direction == DIR_Z2C:
                return (
                    f"**{send:,} ゼニー → {receive:,} カジノコイン** を発行しました。\n"
                    "残高をご確認ください。"
                )
            return (
                f"**{send:,} カジノコイン → {receive:,} ゼニー** の申請が承認されました。\n"
                "別Botで受領をご確認ください。"
            )
        # rejected
        if direction == DIR_Z2C:
            return (
                f"**{send:,} ゼニー → {receive:,} カジノコイン** の申請は拒否されました。\n"
                "送金済みのゼニーの扱いは運営にご確認ください。"
            )
        return (
            f"**{send:,} カジノコイン → {receive:,} ゼニー** の申請は拒否されました。\n"
            "エスクローしていた **カジノコインは全額返金** されました。"
        )

    async def _update_approval_message(self, row, status: str, approver_id: int) -> None:
        """承認チャンネルのメッセージを最終状態に更新してボタンを無効化。"""
        ch_id = row["log_channel_id"]
        msg_id = row["log_message_id"]
        if not ch_id or not msg_id:
            return
        ch = self.bot.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(ch_id))
            except (discord.NotFound, discord.Forbidden):
                return
        try:
            msg = await ch.fetch_message(int(msg_id))
        except (discord.NotFound, discord.Forbidden):
            return
        e = msg.embeds[0] if msg.embeds else discord.Embed()
        e.color = (
            common.COLOR_WIN if status == "approved"
            else common.COLOR_LOSE if status == "rejected"
            else common.COLOR_INFO
        )
        label = {"approved": "✅ 承認済", "rejected": "🚫 拒否",
                 "expired": "⌛ 失効", "cancelled": "↩️ 取消"}[status]
        e.add_field(name="結果", value=f"{label} (by <@{approver_id}>)", inline=False)
        try:
            await msg.edit(embed=e, view=None)
        except discord.HTTPException:
            pass

    async def _dm_user(self, user_id: int, title: str, desc: str, color: int) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            e = common.embed(title, desc, color=color)
            await user.send(embed=e)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("DM 送信に失敗 user=%s: %s", user_id, exc)

    # ── 期限切れ自動 sweep ──
    @tasks.loop(minutes=15)
    async def sweep_expired(self) -> None:
        try:
            await self._do_sweep()
        except Exception:  # noqa: BLE001
            log.exception("sweep_expired で例外")

    async def _do_sweep(self) -> None:
        db = self.bot.db
        rows = await db.expired_pending_requests()
        for row in rows:
            req_id = int(row["id"])
            user_id = int(row["user_id"])
            direction = row["direction"]
            send_amount = int(row["send_amount"])
            if direction == DIR_C2Z:
                async with db.user_lock(user_id):
                    await db.adjust_balance(
                        user_id, send_amount, "exchange_refund", ref=str(req_id)
                    )
            await db.set_exchange_status(req_id, "expired", None)
            await self._update_approval_message(row, "expired", 0)
            await self._dm_user(
                user_id,
                title="⌛ 両替申請が失効しました",
                desc=(
                    f"申請 #{req_id} は有効期限を超えたため自動失効しました。"
                    + ("\nエスクローしていたカジノコインは返金されました。"
                       if direction == DIR_C2Z else "")
                ),
                color=common.COLOR_INFO,
            )

    @sweep_expired.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ── スラッシュコマンド ──
    @app_commands.command(name="両替", description="両替パネルを開く")
    async def exchange_cmd(self, interaction: discord.Interaction) -> None:
        await self.entry(interaction)


async def setup(bot) -> None:
    await bot.add_cog(ExchangeCog(bot))
