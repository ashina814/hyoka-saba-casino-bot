"""管理ダッシュボード。

設計方針:
- スラッシュコマンドは `/管理` の1本だけ。すべての操作はパネル/ボタン/モーダル経由。
- 安全設計を多重化:
  1. **確認ステップ**: 残高変更・凍結は ConfirmView を必ず挟む
  2. **金額閾値**: 1回 high_threshold(既定10万) 超は **理由必須**
  3. **自分への付与禁止**: 管理者本人の残高は増やせない(凍結/監査はOK)
  4. **owner_id 保護**: お釈迦さま口座への付与/没収を拒否
  5. **クールダウン**: 同じ管理者の操作を 5秒以内に連打不可
  6. **監査ログ即時通知**: 操作を承認チャンネルに自動投稿(他管理者が即気付ける)
  7. **Undo**: 直近1件(自分が行った付与/没収/セット)を1回だけ取り消し可能
  8. **全操作ログ**: admin_logs + tx_logs に必ず記録(既存設計をそのまま活用)
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import economy
from ui import common


# 同じ管理者が連打を防ぐクールダウン秒数
_COOLDOWN_SEC = 5.0

# 「これ以上は理由テキスト必須」の金額閾値の設定キー
_HIGH_THRESHOLD_KEY = "admin_confirm_threshold"


def _check_admin(bot, user: discord.abc.User) -> str | None:
    """管理者でなければエラー文を返す。OK なら None。"""
    if not common.is_admin(bot, user):
        return "🚫 このコマンドは管理者専用です。"
    return None


# ───────────────────────── クールダウン管理 ─────────────────────────
class _Cooldowns:
    """admin_id -> 最終操作時刻 を持ち、N秒以内の連打を弾く。"""

    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    def check_and_set(self, admin_id: int) -> float:
        """OKなら 0、NGなら残り秒数を返す。"""
        now = time.monotonic()
        prev = self._last.get(admin_id, 0.0)
        remain = _COOLDOWN_SEC - (now - prev)
        if remain > 0:
            return remain
        self._last[admin_id] = now
        return 0.0


# ───────────────────────── 確認ステップ ─────────────────────────
class ConfirmView(discord.ui.View):
    """『本当に実行しますか?』ボタン。timeout=60秒。

    押せるのは elicit_user_id (起票した管理者) のみ。承認で _on_confirm を呼ぶ。
    """

    def __init__(self, admin_id: int, on_confirm, label_yes: str = "実行する") -> None:
        super().__init__(timeout=60)
        self.admin_id = admin_id
        self._on_confirm = on_confirm
        # ラベルを差し替え可能に
        self.confirm.label = label_yes

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "起票者のみ操作できます。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="実行する", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self._on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="キャンセル", emoji="❌",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ キャンセルしました。", view=self
        )
        self.stop()


# ───────────────────────── 残高操作モーダル ─────────────────────────
class BalanceOpModal(discord.ui.Modal):
    """付与/没収/セット を1つのモーダルで処理。

    operation: 'give' | 'take' | 'set'
    """

    user_id = discord.ui.TextInput(
        label="対象ユーザーID",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )
    amount = discord.ui.TextInput(
        label="金額(セット時は新しい残高そのもの)",
        placeholder="例: 1000",
        required=True, max_length=12,
    )
    reason = discord.ui.TextInput(
        label="理由(高額時は必須)",
        placeholder="補填 / イベント / 訂正 など",
        required=False, max_length=100,
        style=discord.TextStyle.short,
    )

    OPERATION_LABEL = {"give": "付与", "take": "没収", "set": "セット"}

    def __init__(self, cog: "AdminCog", operation: str) -> None:
        super().__init__(title=f"残高{self.OPERATION_LABEL[operation]}")
        self.cog = cog
        self.operation = operation

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_uid = str(self.user_id.value).strip()
        if not raw_uid.isdigit():
            await interaction.response.send_message(
                "⚠️ ユーザーIDは数字のみで入力してください。", ephemeral=True
            )
            return
        target_id = int(raw_uid)
        try:
            amount = common.parse_bet(str(self.amount.value))
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        reason_txt = str(self.reason.value).strip()
        await self.cog.handle_balance_op(
            interaction, self.operation, target_id, amount, reason_txt
        )


class FreezeModal(discord.ui.Modal, title="ユーザー凍結/解凍"):
    user_id = discord.ui.TextInput(
        label="対象ユーザーID",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )

    def __init__(self, cog: "AdminCog", freeze: bool) -> None:
        super().__init__()
        self.cog = cog
        self.freeze = freeze
        self.title = "ユーザーを凍結" if freeze else "ユーザー凍結を解除"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "⚠️ ユーザーIDは数字のみ。", ephemeral=True
            )
            return
        await self.cog.handle_freeze(interaction, int(raw), self.freeze)


class AuditModal(discord.ui.Modal, title="取引履歴を確認"):
    user_id = discord.ui.TextInput(
        label="対象ユーザーID",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "⚠️ ユーザーIDは数字のみ。", ephemeral=True
            )
            return
        await self.cog.show_audit(interaction, int(raw))


class ReloadModal(discord.ui.Modal, title="Cog を再読み込み"):
    cog_name = discord.ui.TextInput(
        label="Cog 名(例: slot, blackjack, exchange)",
        required=True, max_length=40,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.cog_name.value).strip()
        try:
            await self.cog.bot.reload_extension(f"cogs.{name}")
        except Exception as e:  # noqa: BLE001
            await interaction.response.send_message(f"⚠️ 失敗: {e}", ephemeral=True)
            return
        await self.cog.bot.db.log_admin(
            interaction.user.id, "reload", None, f"cogs.{name}"
        )
        await interaction.response.send_message(
            f"🔄 `cogs.{name}` を再読み込みしました。", ephemeral=True
        )


# ───────────────────────── 設定変更まわり(既存を保持) ─────────────────────────
class ConfigModal(discord.ui.Modal):
    def __init__(self, cog: "AdminCog", key: str, vtype: str, current: str) -> None:
        super().__init__(title=f"設定変更: {key}")
        self.cog = cog
        self.key = key
        self.field = discord.ui.TextInput(
            label=f"新しい値 ({vtype})",
            default=current,
            placeholder="bool は 1/0、float は 0.05 のように",
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            newval = await self.cog.bot.db.set_setting(self.key, str(self.field.value))
        except (KeyError, ValueError) as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await self.cog.bot.db.log_admin(
            interaction.user.id, "config", None, f"{self.key}={newval}"
        )
        await self.cog._post_audit_log(
            interaction, f"⚙️ 設定変更 `{self.key}` = `{newval}`"
        )
        await interaction.response.send_message(
            f"✅ `{self.key}` を **{newval}** に変更しました。", ephemeral=True
        )


class ConfigSelect(discord.ui.Select):
    def __init__(self, cog: "AdminCog", rows) -> None:
        self.cog = cog
        self._meta = {r["key"]: (r["vtype"], r["value"]) for r in rows}
        options = [
            discord.SelectOption(
                label=r["key"], description=f"{r['label']} (現在: {r['value']})"[:100]
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="変更する設定を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        vtype, current = self._meta[key]
        await interaction.response.send_modal(
            ConfigModal(self.cog, key, vtype, current)
        )


# ───────────────────────── メインダッシュボード ─────────────────────────
class AdminDashboard(discord.ui.View):
    def __init__(self, cog: "AdminCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    # row 0: ユーザー操作
    @discord.ui.button(label="残高 付与", emoji="➕", row=0,
                       style=discord.ButtonStyle.success)
    async def give(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BalanceOpModal(self.cog, "give"))

    @discord.ui.button(label="残高 没収", emoji="➖", row=0,
                       style=discord.ButtonStyle.danger)
    async def take(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BalanceOpModal(self.cog, "take"))

    @discord.ui.button(label="残高 セット", emoji="🎯", row=0,
                       style=discord.ButtonStyle.secondary)
    async def setbal(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BalanceOpModal(self.cog, "set"))

    @discord.ui.button(label="直前の操作を取消", emoji="↩️", row=0,
                       style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_undo(interaction)

    # row 1: 凍結 / 監査
    @discord.ui.button(label="凍結", emoji="🧊", row=1,
                       style=discord.ButtonStyle.primary)
    async def freeze(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(FreezeModal(self.cog, freeze=True))

    @discord.ui.button(label="解凍", emoji="☀️", row=1,
                       style=discord.ButtonStyle.primary)
    async def unfreeze(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(FreezeModal(self.cog, freeze=False))

    @discord.ui.button(label="取引履歴 監査", emoji="🔍", row=1,
                       style=discord.ButtonStyle.secondary)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AuditModal(self.cog))

    # row 2: 経済・設定
    @discord.ui.button(label="経済ダッシュボード", emoji="📊", row=2,
                       style=discord.ButtonStyle.primary)
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = EconomyDashboardView(self.cog)
        await interaction.response.send_message(
            embed=await self.cog.eco_embed_overview(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="設定一覧", emoji="📋", row=2,
                       style=discord.ButtonStyle.secondary)
    async def listcfg(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.settings_embed(), ephemeral=True
        )

    @discord.ui.button(label="設定を変更", emoji="🛠️", row=2,
                       style=discord.ButtonStyle.success)
    async def editcfg(self, interaction: discord.Interaction, _: discord.ui.Button):
        rows = await self.cog.bot.db.settings_meta()
        view = discord.ui.View(timeout=120)
        view.add_item(ConfigSelect(self.cog, rows))
        await interaction.response.send_message(
            "変更したい設定を選んでください。", view=view, ephemeral=True
        )

    # row 3: 両替まわり
    @discord.ui.button(label="承認CHをここに設定", emoji="📥", row=3,
                       style=discord.ButtonStyle.primary)
    async def set_log_ch(self, interaction: discord.Interaction, _: discord.ui.Button):
        ch = interaction.channel
        if ch is None or not hasattr(ch, "id"):
            await interaction.response.send_message(
                "⚠️ このチャンネルは設定先にできません。", ephemeral=True
            )
            return
        await self.cog.bot.db.set_setting("exchange_log_channel_id", str(ch.id))
        await self.cog.bot.db.log_admin(
            interaction.user.id, "config", None, f"exchange_log_channel_id={ch.id}"
        )
        await self.cog._post_audit_log(
            interaction, f"📥 両替承認CHを <#{ch.id}> に設定"
        )
        await interaction.response.send_message(
            f"✅ 両替承認チャンネルを <#{ch.id}> に設定しました。", ephemeral=True
        )

    @discord.ui.button(label="お釈迦さま設定", emoji="🔥", row=3,
                       style=discord.ButtonStyle.primary)
    async def set_owner(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(OwnerIdModal(self.cog))

    @discord.ui.button(label="両替申請一覧", emoji="📋", row=3,
                       style=discord.ButtonStyle.secondary)
    async def list_pending(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.pending_exchange_embed(), ephemeral=True
        )

    # row 4: イベント & システム
    @discord.ui.button(label="🚀 ブースト開始", row=4,
                       style=discord.ButtonStyle.success)
    async def boost_start(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await interaction.response.send_modal(BoostStartModal(self.cog))

    @discord.ui.button(label="🛑 ブースト終了", row=4,
                       style=discord.ButtonStyle.danger)
    async def boost_end(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        was_active = common.boost_remaining_sec(self.cog.bot) > 0
        await self.cog.bot.db.set_setting("boost_until_ts", "0")
        await self.cog.bot.db.set_setting("boost_multiplier", "1.0")
        await self.cog.bot.db.log_admin(
            interaction.user.id, "boost_end", None, ""
        )
        if was_active:
            e = common.embed(
                "🛑 イベント終了",
                "配当ブーストが終了しました。お疲れさまでした！",
                color=common.COLOR_INFO,
            )
            await common.post_casino_log(self.cog.bot, embed=e)
        await self.cog._post_audit_log(interaction, "🛑 ブースト終了")
        await interaction.response.send_message(
            "✅ ブーストを停止しました。", ephemeral=True
        )

    @discord.ui.button(label="お喋りCH をここに", emoji="📢", row=4,
                       style=discord.ButtonStyle.primary)
    async def set_chat_ch(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        ch = interaction.channel
        if ch is None or not hasattr(ch, "id"):
            await interaction.response.send_message(
                "⚠️ このチャンネルは設定先にできません。", ephemeral=True
            )
            return
        await self.cog.bot.db.set_setting("casino_log_channel_id", str(ch.id))
        await self.cog.bot.db.log_admin(
            interaction.user.id, "config", None, f"casino_log_channel_id={ch.id}"
        )
        await interaction.response.send_message(
            f"✅ お喋りログを <#{ch.id}> に設定しました。", ephemeral=True
        )

    @discord.ui.button(label="🏆 大会を開催", row=2,
                       style=discord.ButtonStyle.success)
    async def tournament_start(self, interaction: discord.Interaction,
                               _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        cog = self.cog.bot.get_cog("TournamentCog")
        if cog is None:
            await interaction.response.send_message(
                "⚠️ 大会機能が無効です。", ephemeral=True
            )
            return
        from cogs.tournament import TournamentStartChoiceView
        await interaction.response.send_message(
            "🏆 開催する大会の種類を選んでください。",
            view=TournamentStartChoiceView(cog),
            ephemeral=True,
        )

    @discord.ui.button(label="👥 管理者一覧", row=4,
                       style=discord.ButtonStyle.secondary)
    async def list_admins(self, interaction: discord.Interaction,
                          _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=await self.cog.admins_embed(), ephemeral=True
        )

    @discord.ui.button(label="➕ 管理者追加", row=4,
                       style=discord.ButtonStyle.success)
    async def add_admin(self, interaction: discord.Interaction,
                        _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await interaction.response.send_modal(AddAdminModal(self.cog))

    @discord.ui.button(label="➖ 管理者削除", row=4,
                       style=discord.ButtonStyle.danger)
    async def remove_admin(self, interaction: discord.Interaction,
                           _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveAdminModal(self.cog))

    @discord.ui.button(label="🛠️ メンテモード切替", row=4,
                       style=discord.ButtonStyle.secondary)
    async def toggle_maint(self, interaction: discord.Interaction,
                           _: discord.ui.Button):
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        db = self.cog.bot.db
        cur = bool(db.setting("maintenance_mode", False))
        new = "0" if cur else "1"
        await db.set_setting("maintenance_mode", new)
        await db.log_admin(
            interaction.user.id, "maintenance",
            None, f"{'ON' if not cur else 'OFF'}",
        )
        label = "🛠️ メンテモード ON(一般プレイ停止)" if not cur \
            else "✅ メンテモード OFF(通常運用に復帰)"
        await self.cog._post_audit_log(interaction, label)
        # お喋りログにも告知
        if not cur:
            e = common.embed(
                "🛠️ メンテナンスのお知らせ",
                "ただいまカジノを一時停止しています。完了までしばらくお待ちください。",
                color=common.COLOR_INFO,
            )
            await common.post_casino_log(self.cog.bot, embed=e)
        else:
            e = common.embed(
                "✅ メンテナンス完了",
                "通常運用を再開しました！ぜひお楽しみください。",
                color=common.COLOR_WIN,
            )
            await common.post_casino_log(self.cog.bot, embed=e)
        await interaction.response.send_message(label, ephemeral=True)

    @discord.ui.button(label="Cogリロード", emoji="🔄", row=4,
                       style=discord.ButtonStyle.secondary)
    async def reload(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ReloadModal(self.cog))


class BoostStartModal(discord.ui.Modal, title="🚀 ブースト開始"):
    multiplier = discord.ui.TextInput(
        label="配当倍率(例: 1.5 = 1.5倍デー)",
        placeholder="1.0で無効、1.5や2.0など",
        required=True, max_length=6,
    )
    hours = discord.ui.TextInput(
        label="期間(時間)",
        placeholder="例: 24",
        required=True, max_length=4,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            mult = float(str(self.multiplier.value))
            hours = int(str(self.hours.value))
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 倍率は小数、期間は整数で。", ephemeral=True
            )
            return
        if mult <= 0 or hours <= 0:
            await interaction.response.send_message(
                "⚠️ 倍率と期間は正の数で。", ephemeral=True
            )
            return
        import time as _t
        until_ts = int(_t.time()) + hours * 3600
        await self.cog.bot.db.set_setting("boost_multiplier", str(mult))
        await self.cog.bot.db.set_setting("boost_until_ts", str(until_ts))
        await self.cog.bot.db.log_admin(
            interaction.user.id, "boost_start", None,
            f"×{mult} for {hours}h",
        )
        # お喋りログに大々的に告知
        e = common.embed(
            "🚀 イベント発動！",
            f"いま開始！ **{hours}時間限定で 配当 ×{mult} デー** 🎉\n"
            "PVE全ゲーム(スロット/チンチロ/ハイロー/BJ)で配当が増えます。",
            color=common.COLOR_JACKPOT,
        )
        await common.post_casino_log(self.cog.bot, embed=e)
        await self.cog._post_audit_log(
            interaction, f"🚀 ブースト開始 ×{mult} for {hours}h"
        )
        await interaction.response.send_message(
            f"✅ ブースト開始: ×{mult} を {hours}時間。", ephemeral=True
        )


class EconomyDashboardView(discord.ui.View):
    """経済ダッシュボードの3タブ切替。管理者のみ操作可。"""

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not common.is_admin(self.cog.bot, interaction.user):
            await interaction.response.send_message(
                "🚫 管理者専用です。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="概要", emoji="🏠", style=discord.ButtonStyle.primary)
    async def overview(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=await self.cog.eco_embed_overview(), view=self
        )

    @discord.ui.button(label="詳細", emoji="🔬", style=discord.ButtonStyle.secondary)
    async def detail(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=await self.cog.eco_embed_detail(), view=self
        )

    @discord.ui.button(label="推移", emoji="📅", style=discord.ButtonStyle.secondary)
    async def trend(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=await self.cog.eco_embed_trend(), view=self
        )

    @discord.ui.button(label="今すぐスナップショット", emoji="📸",
                       style=discord.ButtonStyle.success)
    async def snap(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.bot.db.write_snapshot_today()
        await interaction.response.send_message(
            "📸 スナップショットを記録しました。", ephemeral=True
        )


class AddAdminModal(discord.ui.Modal, title="➕ 管理者を追加"):
    user_id = discord.ui.TextInput(
        label="追加するユーザーID(数字のみ)",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "⚠️ ユーザーIDは数字のみで入力してください。", ephemeral=True
            )
            return
        uid = int(raw)
        bot = self.cog.bot
        if uid in bot.admin_ids:
            await interaction.response.send_message(
                "ℹ️ 既に管理者です。", ephemeral=True
            )
            return
        ok = await bot.db.add_admin(uid, interaction.user.id)
        if not ok:
            await interaction.response.send_message(
                "⚠️ 追加に失敗しました(DB側で重複している可能性)。",
                ephemeral=True,
            )
            return
        await bot.refresh_admins()
        await bot.db.log_admin(
            interaction.user.id, "admin_add", uid, "via dashboard"
        )
        await self.cog._post_audit_log(
            interaction, f"➕ 管理者追加 <@{uid}>"
        )
        await interaction.response.send_message(
            f"✅ <@{uid}> を管理者に追加しました。", ephemeral=True
        )


class RemoveAdminModal(discord.ui.Modal, title="➖ 管理者を削除"):
    user_id = discord.ui.TextInput(
        label="削除するユーザーID(.env由来は削除不可)",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "⚠️ ユーザーIDは数字のみで入力してください。", ephemeral=True
            )
            return
        uid = int(raw)
        bot = self.cog.bot
        if bot.is_env_admin(uid):
            await interaction.response.send_message(
                "🚫 `.env` の `ADMIN_IDS` に登録されている管理者は"
                "このパネルからは削除できません。\n"
                "(削除するには `.env` を編集して再起動してください)",
                ephemeral=True,
            )
            return
        ok = await bot.db.remove_admin(uid)
        if not ok:
            await interaction.response.send_message(
                "ℹ️ そのユーザーはDB管理者として登録されていません。",
                ephemeral=True,
            )
            return
        await bot.refresh_admins()
        await bot.db.log_admin(
            interaction.user.id, "admin_remove", uid, "via dashboard"
        )
        await self.cog._post_audit_log(
            interaction, f"➖ 管理者削除 <@{uid}>"
        )
        await interaction.response.send_message(
            f"✅ <@{uid}> を管理者から外しました。", ephemeral=True
        )


class OwnerIdModal(discord.ui.Modal, title="お釈迦さま(焼却受取)を設定"):
    user_id = discord.ui.TextInput(
        label="Discord ユーザーID(数字のみ)",
        placeholder="例: 123456789012345678",
        required=True, max_length=20,
    )

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.user_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "⚠️ 数字のIDで入力してください。", ephemeral=True
            )
            return
        await self.cog.bot.db.set_setting("owner_id", raw)
        await self.cog.bot.db.log_admin(
            interaction.user.id, "config", None, f"owner_id={raw}"
        )
        await self.cog._post_audit_log(
            interaction, f"🔥 お釈迦さまを <@{raw}> に設定"
        )
        await interaction.response.send_message(
            f"✅ お釈迦さまを <@{raw}> に設定しました。", ephemeral=True
        )


# ───────────────────────── Cog 本体 ─────────────────────────
class AdminCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._cd = _Cooldowns()
        # admin_id -> 直近1件の操作情報(undo 用)
        self._last_op: dict[int, dict] = {}

    # ── 共通: 監査ログをチャンネル投稿 ──
    async def _post_audit_log(self, interaction: discord.Interaction, msg: str) -> None:
        """承認チャンネル(exchange_log_channel_id を流用)に管理操作を通知。

        専用 admin_log_channel_id を別途持つこともできるが、
        現状は同じ場所に流して管理者同士の見落としを減らす方針。
        """
        ch_id = int(self.bot.db.setting("exchange_log_channel_id", 0) or 0)
        if not ch_id:
            return
        ch = self.bot.get_channel(ch_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(ch_id)
            except (discord.NotFound, discord.Forbidden):
                return
        try:
            e = common.embed(
                "🛡️ 管理操作ログ",
                f"操作者: {interaction.user.mention}\n{msg}",
                color=common.COLOR_ADMIN,
            )
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    # ── 共通: クールダウン+権限チェック ──
    async def _gate(self, interaction: discord.Interaction) -> bool:
        err = _check_admin(self.bot, interaction.user)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return False
        remain = self._cd.check_and_set(interaction.user.id)
        if remain > 0:
            await interaction.response.send_message(
                f"⏳ 連打防止: あと {remain:.1f} 秒待ってください。",
                ephemeral=True,
            )
            return False
        return True

    # ── 残高操作(付与/没収/セット) ──
    async def handle_balance_op(
        self, interaction: discord.Interaction, op: str,
        target_id: int, amount: int, reason_txt: str,
    ) -> None:
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return

        # 安全装置1: 自分への付与禁止
        if op == "give" and target_id == interaction.user.id:
            await interaction.response.send_message(
                "🚫 自分自身に付与することはできません。", ephemeral=True
            )
            return

        # 安全装置2: お釈迦さま保護
        owner_id = int(self.bot.db.setting("owner_id", 0) or 0)
        if owner_id and target_id == owner_id and op in ("give", "take"):
            await interaction.response.send_message(
                "🚫 お釈迦さま口座への付与/没収はできません(焼却整合性のため)。\n"
                "残高セットは必要なら使えます。",
                ephemeral=True,
            )
            return

        # 安全装置3: 高額時は理由必須
        threshold = int(self.bot.db.setting(_HIGH_THRESHOLD_KEY, 100000))
        if amount >= threshold and not reason_txt:
            await interaction.response.send_message(
                f"⚠️ {threshold:,} 以上の操作は **理由(reason欄)** を入力してください。",
                ephemeral=True,
            )
            return

        # 安全装置4: クールダウン
        remain = self._cd.check_and_set(interaction.user.id)
        if remain > 0:
            await interaction.response.send_message(
                f"⏳ 連打防止: あと {remain:.1f} 秒待ってください。", ephemeral=True
            )
            return

        op_label = {"give": "付与", "take": "没収", "set": "セット"}[op]
        # 確認 Embed
        target_mention = f"<@{target_id}> (`{target_id}`)"
        current_bal = await self.bot.db.get_balance(target_id)
        if op == "give":
            after = current_bal + amount
        elif op == "take":
            after = current_bal - amount
        else:
            after = amount
        e = common.embed(
            f"🛡️ 確認: 残高{op_label}",
            f"対象: {target_mention}\n"
            f"現在残高: **{current_bal:,}**\n"
            f"操作後の残高(予定): **{after:,}**\n"
            f"理由: {reason_txt or '(未記入)'}",
            color=common.COLOR_ADMIN,
        )
        if amount >= threshold:
            e.set_footer(text="⚠️ 高額操作。実行内容を再確認してください。")

        async def _do(intr: discord.Interaction):
            db = self.bot.db
            async with db.user_lock(target_id):
                if op == "give":
                    new = await db.adjust_balance(
                        target_id, amount,
                        f"admin_give{':' + reason_txt if reason_txt else ''}"[:50],
                    )
                    delta = amount
                elif op == "take":
                    new = await db.adjust_balance(
                        target_id, -amount,
                        f"admin_take{':' + reason_txt if reason_txt else ''}"[:50],
                        allow_negative=True,
                    )
                    delta = -amount
                else:  # set
                    new = await db.set_balance(target_id, amount, "admin_set")
                    delta = amount - current_bal
            await db.log_admin(
                intr.user.id, op, target_id,
                f"amount={amount} delta={delta} reason={reason_txt or '-'}",
            )
            # Undo 用記録
            self._last_op[intr.user.id] = {
                "target_id": target_id,
                "delta": delta,
                "reason": op,
                "ts": time.time(),
            }
            await self._post_audit_log(
                intr, f"{op_label} {target_mention} `{delta:+,}` → 残高 `{new:,}`"
                      + (f"\n理由: {reason_txt}" if reason_txt else "")
            )
            await intr.followup.send(
                f"✅ {op_label}完了。{target_mention} 新残高 **{new:,}**",
                ephemeral=True,
            )

        view = ConfirmView(interaction.user.id, _do, label_yes=f"{op_label}を実行")
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)

    # ── Undo(直近1件) ──
    async def handle_undo(self, interaction: discord.Interaction) -> None:
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        op = self._last_op.get(interaction.user.id)
        if not op:
            await interaction.response.send_message(
                "↩️ 取消対象がありません(あなたの直近の付与/没収/セットのみ取消可能)。",
                ephemeral=True,
            )
            return
        delta = op["delta"]
        target_id = op["target_id"]
        e = common.embed(
            "🛡️ 確認: 直前操作の取消",
            f"対象: <@{target_id}>\n反対操作: 残高を **`{-delta:+,}`**\n"
            f"取消可能なのは1回限りです。",
            color=common.COLOR_ADMIN,
        )

        async def _do(intr: discord.Interaction):
            db = self.bot.db
            async with db.user_lock(target_id):
                new = await db.adjust_balance(
                    target_id, -delta, "admin_undo", allow_negative=True
                )
            await db.log_admin(
                intr.user.id, "undo", target_id, f"reverted delta={delta}"
            )
            self._last_op.pop(intr.user.id, None)
            await self._post_audit_log(
                intr, f"↩️ 取消: <@{target_id}> 残高 `{-delta:+,}` → `{new:,}`"
            )
            await intr.followup.send(
                f"✅ 取消完了。<@{target_id}> 新残高 **{new:,}**", ephemeral=True
            )

        view = ConfirmView(interaction.user.id, _do, label_yes="取消を実行")
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)

    # ── 凍結/解凍 ──
    async def handle_freeze(
        self, interaction: discord.Interaction, target_id: int, freeze: bool
    ) -> None:
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        remain = self._cd.check_and_set(interaction.user.id)
        if remain > 0:
            await interaction.response.send_message(
                f"⏳ あと {remain:.1f} 秒待ってください。", ephemeral=True
            )
            return
        label = "凍結" if freeze else "解凍"
        e = common.embed(
            f"🛡️ 確認: ユーザー{label}",
            f"対象: <@{target_id}> (`{target_id}`)\n"
            f"{'賭博・両替・送金が不可になります。' if freeze else '通常状態に戻します。'}",
            color=common.COLOR_ADMIN,
        )

        async def _do(intr: discord.Interaction):
            db = self.bot.db
            await db.set_frozen(target_id, freeze)
            await db.log_admin(
                intr.user.id, "freeze" if freeze else "unfreeze", target_id, ""
            )
            mark = "🧊" if freeze else "☀️"
            await self._post_audit_log(
                intr, f"{mark} {label} <@{target_id}>"
            )
            await intr.followup.send(
                f"{mark} <@{target_id}> を{label}しました。", ephemeral=True
            )

        view = ConfirmView(interaction.user.id, _do, label_yes=f"{label}を実行")
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)

    # ── 監査表示 ──
    async def show_audit(self, interaction: discord.Interaction, target_id: int) -> None:
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        rows = await self.bot.db.recent_tx(target_id, 15)
        bal = await self.bot.db.get_balance(target_id)
        lines = []
        for r in rows:
            sign = "+" if r["delta"] >= 0 else ""
            reason_jp = common.tx_reason_jp(r["reason"])
            lines.append(
                f"`{r['ts'][5:16]}` `{sign}{r['delta']:,}` "
                f"({reason_jp}) → **{r['balance_after']:,}**"
            )
        e = common.embed(
            f"🔍 監査: <@{target_id}>",
            "\n".join(lines) or "履歴なし",
            color=common.COLOR_ADMIN,
        )
        e.add_field(name="現在残高", value=common.money(self.bot.cfg, bal))
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── 経済ダッシュボード(3タブ) ──
    async def eco_embed_overview(self) -> discord.Embed:
        """🟢🟡🔴 で健康度を一目で判別 + 主要指標。"""
        db = self.bot.db
        cfg = self.bot.cfg
        m = await db.economy_dashboard()
        g_icon, g_msg = economy.classify_gini(m["gini"])
        i_icon, i_msg, i_rate = economy.classify_inflation(
            m["period_30d"]["net"], m["total_supply"]
        )
        a_icon, a_msg = economy.classify_activity(
            m["active_count_7d"], m["user_count"]
        )

        # 健康度サマリの総合判定(最悪寄せ)
        worst = max((g_icon, i_icon, a_icon), key=lambda x: "🔴🟡🟢".index(x))
        summary = {"🟢": "健康", "🟡": "警告", "🔴": "危険"}[worst]

        e = common.embed(
            f"📊 経済ダッシュボード — {worst} {summary}",
            f"**Gini**: {g_icon} {g_msg}\n"
            f"**インフレ**: {i_icon} {i_msg} ({i_rate:+.1f}%/月)\n"
            f"**アクティブ**: {a_icon} {a_msg}",
            color=common.COLOR_ADMIN,
        )
        e.add_field(name="総供給量", value=common.money(cfg, m["total_supply"]))
        e.add_field(name="ユーザー数", value=f"{m['user_count']:,}")
        e.add_field(name="アクティブ(7d)", value=f"{m['active_count_7d']:,}")
        e.add_field(name="Gini係数", value=f"`{m['gini']:.3f}`")
        e.add_field(name="上位10%集中度", value=f"{m['top10_share'] * 100:.1f}%")
        e.add_field(name="中央値残高", value=common.money(cfg, m["median_balance"]))
        e.add_field(name="JP残高", value=common.money(cfg, m["jackpot"]))
        e.add_field(name="24時間ベット量", value=common.money(cfg, m["bet_volume_24h"]))
        e.add_field(
            name="30日純発行(発行−消滅)",
            value=common.money(cfg, m["period_30d"]["net"]),
        )
        e.set_footer(text="他のタブで詳細/推移を確認 / 1日1回スナップショット自動保存")
        return e

    async def eco_embed_detail(self) -> discord.Embed:
        """ソース/シンクの内訳と上位プレイヤー。"""
        db = self.bot.db
        cfg = self.bot.cfg
        m = await db.economy_dashboard()
        e = common.embed("📊 経済ダッシュボード — 詳細", color=common.COLOR_ADMIN)

        for label, key in (("24時間", "period_1d"), ("7日間", "period_7d"),
                           ("30日間", "period_30d")):
            p = m[key]
            e.add_field(
                name=f"📈 ソース/シンク ({label})",
                value=(
                    f"発行: **{p['source']:,}**\n"
                    f"消滅: **{p['sink']:,}**\n"
                    f"純: **{p['net']:+,}**"
                ),
                inline=True,
            )

        # 7日間の reason 内訳(上位5)
        srcs = m["top_sources_7d"]
        sinks = m["top_sinks_7d"]
        e.add_field(
            name="🟢 主なソース(7d, reason別)",
            value="\n".join(
                f"`{common.tx_reason_jp(r['reason'])}` +{int(r['s']):,}"
                for r in srcs
            ) or "—",
            inline=False,
        )
        e.add_field(
            name="🔴 主なシンク(7d, reason別)",
            value="\n".join(
                f"`{common.tx_reason_jp(r['reason'])}` -{int(r['s']):,}"
                for r in sinks
            ) or "—",
            inline=False,
        )

        rows = await db.leaderboard(5)
        rich = "\n".join(
            f"{i+1}. <@{r['user_id']}> — {common.money(cfg, int(r['balance']))}"
            for i, r in enumerate(rows)
        ) or "—"
        e.add_field(name="🏆 資産上位5(お釈迦さま除外)", value=rich, inline=False)
        return e

    async def eco_embed_trend(self) -> discord.Embed:
        """過去のスナップショット推移と当日比較。"""
        db = self.bot.db
        cfg = self.bot.cfg
        snaps = await db.recent_snapshots(14)
        if not snaps:
            e = common.embed(
                "📊 経済ダッシュボード — 推移",
                "まだスナップショットが記録されていません。\n"
                "次の日次更新(UTC 00時頃)以降に履歴が見え始めます。",
                color=common.COLOR_ADMIN,
            )
            return e
        # 当日と前回(あれば前日)を比較
        cur_metrics = await db.economy_dashboard()
        latest = snaps[0]

        def _delta(now: int, prev: int) -> str:
            d = now - prev
            sign = "📈 +" if d >= 0 else "📉 "
            return f"{sign}{d:,}"

        e = common.embed(
            "📊 経済ダッシュボード — 推移",
            f"最新スナップショット: `{latest['date']}`",
            color=common.COLOR_ADMIN,
        )
        e.add_field(
            name="総供給量(現在 vs 直近)",
            value=(
                f"{common.money(cfg, cur_metrics['total_supply'])} / "
                f"{_delta(cur_metrics['total_supply'], int(latest['total_supply']))}"
            ),
            inline=False,
        )
        e.add_field(
            name="Gini(現在 vs 直近)",
            value=f"`{cur_metrics['gini']:.3f}` (Δ {cur_metrics['gini'] - float(latest['gini']):+.3f})",
        )
        e.add_field(
            name="アクティブ(7d)",
            value=f"{cur_metrics['active_count_7d']:,} "
                  f"({_delta(cur_metrics['active_count_7d'], int(latest['active_count']))})",
        )

        # 過去14日テーブル(コードブロックで等幅)
        lines = ["日付        供給        Gini  Active   30d純"]
        for s in snaps:
            lines.append(
                f"{s['date']}  {int(s['total_supply']):>9,}  "
                f"{float(s['gini']):.3f}  {int(s['active_count']):>5}  "
                f"{int(s['monthly_net']):>+9,}"
            )
        e.add_field(
            name="📅 直近14日スナップショット",
            value="```\n" + "\n".join(lines) + "\n```",
            inline=False,
        )
        return e

    # ── スナップショット日次ループ ──
    @tasks.loop(hours=24)
    async def _snapshot_loop(self) -> None:
        try:
            await self.bot.db.write_snapshot_today()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("casino.admin").exception("snapshot 失敗")

    @_snapshot_loop.before_loop
    async def _before_snapshot(self) -> None:
        await self.bot.wait_until_ready()
        # 起動時に1回流して、初日のデータを早めに作る
        try:
            await self.bot.db.write_snapshot_today()
        except Exception:  # noqa: BLE001
            pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._snapshot_loop.is_running():
            self._snapshot_loop.start()

    # ── 管理者一覧 ──
    async def admins_embed(self) -> discord.Embed:
        env_ids = sorted(self.bot.cfg.admin_ids)
        db_records = await self.bot.db.list_admin_records()
        e = common.embed(
            "👥 管理者一覧",
            f"合計 **{len(self.bot.admin_ids)}** 人",
            color=common.COLOR_ADMIN,
        )
        e.add_field(
            name=f"🔒 .env由来 ({len(env_ids)}人) — 削除不可",
            value="\n".join(f"<@{uid}> `{uid}`" for uid in env_ids) or "—",
            inline=False,
        )
        if db_records:
            lines = []
            for r in db_records:
                uid = int(r["user_id"])
                by = int(r["added_by"])
                lines.append(
                    f"<@{uid}> `{uid}`  ← <@{by}> が `{r['added_at'][:16]}` に追加"
                )
            e.add_field(
                name=f"⚙️ DB管理(運用追加, {len(db_records)}人) — 削除可",
                value="\n".join(lines),
                inline=False,
            )
        else:
            e.add_field(
                name="⚙️ DB管理(運用追加)",
                value="(なし)",
                inline=False,
            )
        return e

    # ── 設定一覧 ──
    async def settings_embed(self) -> discord.Embed:
        rows = await self.bot.db.settings_meta()
        e = common.embed("📋 設定一覧", color=common.COLOR_ADMIN)
        for r in rows:
            e.add_field(
                name=f"{r['key']} = {r['value']}",
                value=r["label"] or "​",
                inline=False,
            )
        return e

    # ── 両替保留一覧 ──
    async def pending_exchange_embed(self) -> discord.Embed:
        cur = await self.bot.db.conn.execute(
            "SELECT id, user_id, direction, send_amount, receive_amount, created_at "
            "FROM exchange_requests WHERE status='pending' "
            "ORDER BY created_at DESC LIMIT 20"
        )
        rows = list(await cur.fetchall())
        e = common.embed("💱 両替 保留中申請", color=common.COLOR_ADMIN)
        if not rows:
            e.description = "保留中の申請はありません。"
            return e
        for r in rows:
            dir_label = (
                "ゼニー→カジノ" if r["direction"] == "zeny_to_coin"
                else "カジノ→ゼニー"
            )
            e.add_field(
                name=f"#{r['id']}  {dir_label}",
                value=(
                    f"<@{r['user_id']}> 送 **{r['send_amount']:,}** → "
                    f"受 **{r['receive_amount']:,}**\n"
                    f"申請: `{r['created_at'][:16]}`"
                ),
                inline=False,
            )
        return e

    # ── ダッシュボード ──
    def dashboard_embed(self) -> discord.Embed:
        e = common.embed(
            "🛠️ 管理ダッシュボード",
            "操作はボタン経由で行えます。残高変更・凍結は確認ステップ付き。\n"
            "高額操作は理由必須、自分への付与は禁止、5秒の連打防止あり。",
            color=common.COLOR_ADMIN,
        )
        e.add_field(
            name="ユーザー操作", value="付与 / 没収 / セット / 取消 / 凍結 / 解凍 / 監査",
            inline=False,
        )
        e.add_field(
            name="経済・設定", value="統計 / 設定一覧 / 設定変更", inline=False,
        )
        e.add_field(
            name="両替", value="承認CH設定 / お釈迦さま設定 / 申請一覧", inline=False,
        )
        e.add_field(
            name="システム", value="Cog リロード", inline=False,
        )
        e.set_footer(text="操作は監査ログに残り、承認チャンネルへ自動投稿されます。")
        return e

    @app_commands.command(name="管理", description="管理ダッシュボードを開く")
    async def panel(self, interaction: discord.Interaction) -> None:
        err = _check_admin(self.bot, interaction.user)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_message(
            embed=self.dashboard_embed(),
            view=AdminDashboard(self),
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(AdminCog(bot))
