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
        return await self._embed_balance(user)

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
        db = self.bot.db
        cfg = self.bot.cfg
        s = await db.user_stats(user.id)
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
