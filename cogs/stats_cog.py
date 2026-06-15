"""自分用プロフィール: 残高 / 履歴 / 統計 を1パネル + タブUI で切替。

スラッシュコマンドは `/プロフィール` 1本のみ。EconomyCog/HelpCog の旧コマンド
を撤廃した代わりに、自分の情報を見たい全ての需要はここで完結する。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common

_PAGE_SIZE = 10


GAME_LABEL = {
    "slot": "🎰 スロット",
    "chinchiro": "🎲 チンチロ",
    "hilo": "📈 ハイロー",
    "blackjack": "🃏 BJ",
    "pvp": "⚔️ PVP",
}


def _format_history_page(rows) -> str:
    if not rows:
        return "履歴なし"
    lines = []
    for r in rows:
        sign = "+" if r["delta"] >= 0 else ""
        reason_jp = common.tx_reason_jp(r["reason"])
        lines.append(
            f"`{r['ts'][5:16]}` `{sign}{r['delta']:>+8,}` "
            f"({reason_jp}) → **{r['balance_after']:,}**"
        )
    return "\n".join(lines)


class ProfileView(discord.ui.View):
    """残高/履歴/統計 をタブで切替。履歴は内側でページング。"""

    def __init__(self, cog: "StatsCog", user: discord.abc.User) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user = user
        self.tab = "balance"  # 'balance' | 'history' | 'stats'
        self.page = 0
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "他人のプロフィールは操作できません。", ephemeral=True
            )
            return False
        return True

    def _sync_buttons(self) -> None:
        """現在のタブに応じてボタンの enable / disable とラベル色を整える。"""
        # 履歴専用のページボタンはタブが history のときだけ enable
        for item in self.children:
            cid = getattr(item, "custom_id", "")
            if cid in ("prof:prev", "prof:next"):
                item.disabled = (self.tab != "history")
            # アクティブなタブはハイライト(primary)
            if cid == f"prof:tab:{self.tab}":
                item.style = discord.ButtonStyle.primary
            elif cid and cid.startswith("prof:tab:"):
                item.style = discord.ButtonStyle.secondary

    async def _switch(self, interaction: discord.Interaction, tab: str) -> None:
        self.tab = tab
        if tab != "history":
            self.page = 0
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=await self.cog.build_embed(self.user, self.tab, self.page),
            view=self,
        )

    # row 0: タブ
    @discord.ui.button(label="💰 残高", row=0, custom_id="prof:tab:balance",
                       style=discord.ButtonStyle.primary)
    async def tab_balance(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "balance")

    @discord.ui.button(label="📜 履歴", row=0, custom_id="prof:tab:history",
                       style=discord.ButtonStyle.secondary)
    async def tab_history(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "history")

    @discord.ui.button(label="📊 統計", row=0, custom_id="prof:tab:stats",
                       style=discord.ButtonStyle.secondary)
    async def tab_stats(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "stats")

    @discord.ui.button(label="🏅 称号", row=0, custom_id="prof:tab:badges",
                       style=discord.ButtonStyle.secondary)
    async def tab_badges(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "badges")

    @discord.ui.button(label="🛡️ 制限", row=0, custom_id="prof:tab:limit",
                       style=discord.ButtonStyle.secondary)
    async def tab_limit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "limit")

    @discord.ui.button(label="上限を変更", row=1, custom_id="prof:edit_limit",
                       emoji="🛡️", style=discord.ButtonStyle.primary)
    async def edit_limit(self, interaction: discord.Interaction,
                         _: discord.ui.Button):
        await interaction.response.send_modal(LimitModal(self.cog, self))

    # row 1: 履歴ページング(他タブでは disabled)
    @discord.ui.button(label="◀ 前", row=1, custom_id="prof:prev",
                       style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(
            embed=await self.cog.build_embed(self.user, self.tab, self.page),
            view=self,
        )

    @discord.ui.button(label="次 ▶", row=1, custom_id="prof:next",
                       style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(
            embed=await self.cog.build_embed(self.user, self.tab, self.page),
            view=self,
        )


class LimitModal(discord.ui.Modal, title="🛡️ 1日のベット上限を設定"):
    cap_input = discord.ui.TextInput(
        label="1日の上限(0=無制限)",
        placeholder="例: 5000",
        required=True, max_length=12,
    )

    def __init__(self, cog: "StatsCog", parent: "ProfileView") -> None:
        super().__init__()
        self.cog = cog
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from datetime import datetime, timezone
        try:
            new_cap = common.parse_bet(str(self.cap_input.value)) \
                if str(self.cap_input.value).strip() != "0" else 0
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        db = self.cog.bot.db
        cur = await db.get_user_limit(interaction.user.id)
        old_cap = int(cur.get("daily_bet_cap", 0) or 0)
        set_at = cur.get("set_at")
        # 引き上げ/解除はクールダウン(24h)
        loosening = (old_cap > 0) and (new_cap == 0 or new_cap > old_cap)
        if loosening and set_at:
            try:
                last = datetime.strptime(set_at[:19], "%Y-%m-%dT%H:%M:%S")
                last = last.replace(tzinfo=timezone.utc)
                hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if hours < 24:
                    remain = int(24 - hours)
                    await interaction.response.send_message(
                        f"⚠️ 上限を緩める操作は **24時間のクールダウン** があります。"
                        f"あと約 {remain} 時間お待ちください。",
                        ephemeral=True,
                    )
                    return
            except ValueError:
                pass
        await db.set_user_limit(interaction.user.id, new_cap)
        msg = "🛡️ 上限を解除しました。" if new_cap == 0 \
            else f"🛡️ 1日の上限を **{new_cap:,}** に設定しました。"
        # パネル更新
        await interaction.response.edit_message(
            embed=await self.cog.build_embed(interaction.user, "limit"),
            view=self.parent,
        )
        await interaction.followup.send(msg, ephemeral=True)


class StatsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # ── Embed ビルダー(タブ別) ──
    async def build_embed(
        self, user: discord.abc.User, tab: str, page: int = 0
    ) -> discord.Embed:
        if tab == "history":
            return await self._embed_history(user, page)
        if tab == "stats":
            return await self._embed_stats(user)
        if tab == "badges":
            return await self._embed_badges(user)
        if tab == "limit":
            return await self._embed_limit(user)
        return await self._embed_balance(user)

    async def _embed_limit(self, user: discord.abc.User) -> discord.Embed:
        db = self.bot.db
        lim = await db.get_user_limit(user.id)
        cap = int(lim.get("daily_bet_cap", 0) or 0)
        today = await db.daily_bet_total(user.id)
        e = common.embed(
            f"👤 {user.display_name} — 🛡️ 自己制限",
            "自分自身に1日のベット上限を設定できます。\n"
            "ギャンブルとの健全な距離をとるための機能です。",
            color=common.COLOR_INFO,
        )
        if cap <= 0:
            e.add_field(name="現在の上限", value="未設定(無制限)", inline=False)
        else:
            from core.badges import _bar
            e.add_field(
                name="1日の上限",
                value=f"`{_bar(today, cap)}` **{today:,} / {cap:,}**",
                inline=False,
            )
        e.add_field(
            name="ヒント",
            value=(
                "・上限変更は **「上限を変更」** ボタンから\n"
                "・新規設定は即時反映\n"
                "・**上限の引き上げ/解除には24時間のクールダウン**\n"
                "・引き下げ(より厳しく)はいつでも可能"
            ),
            inline=False,
        )
        return e

    async def _embed_badges(self, user: discord.abc.User) -> discord.Embed:
        from core.badges import BADGES, progress_for, _bar
        db = self.bot.db
        earned = set(await db.user_badges(user.id))
        e = common.embed(
            f"👤 {user.display_name} のプロフィール — 🏅 称号",
            f"獲得: **{len(earned)} / {len(BADGES)}**",
            color=common.COLOR_INFO,
        )
        lines = []
        for b in BADGES:
            has = b.id in earned
            mark = "✅" if has else "▫️"
            prog = await progress_for(db, user.id, b.id)
            if has:
                lines.append(f"{mark} {b.emoji} **{b.label}**  _{b.description}_")
            elif prog:
                cur, target = prog
                pct = min(100, int(cur * 100 / target)) if target else 0
                lines.append(
                    f"{mark} {b.emoji} **{b.label}**  `{_bar(cur, target)}` "
                    f"{cur:,}/{target:,} ({pct}%)\n"
                    f"　_{b.description}_"
                )
            else:
                lines.append(f"{mark} {b.emoji} **{b.label}**  _{b.description}_")
        e.add_field(name="一覧", value="\n".join(lines), inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        return e

    async def _embed_balance(self, user: discord.abc.User) -> discord.Embed:
        db = self.bot.db
        row = await db.ensure_user(user.id)
        cfg = self.bot.cfg
        e = common.embed(
            f"👤 {user.display_name} のプロフィール — 残高",
            color=common.COLOR_MAIN,
        )
        e.add_field(name="残高", value=common.money(cfg, int(row["balance"])))
        if row["win_streak"]:
            e.add_field(name="現在の連勝", value=f"🔥 {row['win_streak']}")
        if row["daily_streak"]:
            e.add_field(name="ログイン連続", value=f"📅 {row['daily_streak']} 日")
        if row["frozen"]:
            e.add_field(name="状態", value="🧊 凍結中", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        e.set_footer(text="タブ: 💰残高 / 📜履歴 / 📊統計")
        return e

    async def _embed_history(self, user: discord.abc.User, page: int) -> discord.Embed:
        db = self.bot.db
        offset = page * _PAGE_SIZE
        cur = await db.conn.execute(
            "SELECT delta, balance_after, reason, ref, ts FROM tx_logs "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user.id, _PAGE_SIZE, offset),
        )
        rows = list(await cur.fetchall())
        bal = await db.get_balance(user.id)
        e = common.embed(
            f"👤 {user.display_name} のプロフィール — 履歴 (p{page + 1})",
            _format_history_page(rows),
            color=common.COLOR_INFO,
        )
        e.add_field(name="現在残高", value=common.money(self.bot.cfg, bal))
        if not rows and page > 0:
            e.set_footer(text="これ以上履歴はありません。 ◀前 で戻れます")
        else:
            e.set_footer(text="◀前 / 次▶ でページを移動")
        return e

    async def _embed_stats(self, user: discord.abc.User) -> discord.Embed:
        from core.badges import badge_label
        db = self.bot.db
        cfg = self.bot.cfg
        s = await db.user_stats(user.id)
        badges = await db.user_badges(user.id)
        e = common.embed(
            f"👤 {user.display_name} のプロフィール — 統計",
            color=common.COLOR_INFO,
        )
        e.add_field(name="残高", value=common.money(cfg, s["balance"]))
        e.add_field(name="累計ベット", value=common.money(cfg, s["total_bet_volume"]))
        e.add_field(name="JP獲得", value=f"💎 {s['jackpots_won']} 回")
        e.add_field(name="現在の連勝", value=f"🔥 {s['win_streak']}")
        e.add_field(name="自己最高連勝", value=f"🏆 {s['max_win_streak']}")
        e.add_field(name="ログイン連続", value=f"📅 {s['daily_streak']} 日")

        per = s["per_game"]
        lines = []
        for key, label in GAME_LABEL.items():
            d = per.get(key)
            if not d or d["plays"] == 0:
                continue
            sign = "📈 +" if d["net"] >= 0 else "📉 "
            lines.append(
                f"{label}  プレイ {d['plays']:>3}  /  収支 {sign}{d['net']:,}"
            )
        e.add_field(
            name="ゲーム別",
            value="\n".join(lines) or "(まだプレイ履歴がありません)",
            inline=False,
        )
        if badges:
            e.add_field(
                name=f"🏅 称号 ({len(badges)})",
                value="\n".join(badge_label(b) for b in badges),
                inline=False,
            )
        e.set_thumbnail(url=user.display_avatar.url)
        return e

    @app_commands.command(name="プロフィール",
                          description="自分の残高/履歴/統計を1パネルで表示")
    async def profile(self, interaction: discord.Interaction) -> None:
        view = ProfileView(self, interaction.user)
        await interaction.response.send_message(
            embed=await self.build_embed(interaction.user, "balance"),
            view=view,
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(StatsCog(bot))
