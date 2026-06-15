"""自分用の取引履歴と統計表示。

`/履歴` … ページング付き Embed で自分の取引履歴を見る
`/統計` … 自分のプレイ統計(ゲーム別収支/勝率/JP獲得/最高連勝など)

集計は DAO の user_stats() に集約。Cog 側は Embed の整形だけ。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common

_PAGE_SIZE = 10


def _format_history_page(rows, cfg) -> str:
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


GAME_LABEL = {
    "slot": "🎰 スロット",
    "chinchiro": "🎲 チンチロ",
    "hilo": "📈 ハイロー",
    "blackjack": "🃏 BJ",
    "pvp": "⚔️ PVP",
}


class HistoryView(discord.ui.View):
    """履歴のページ送り。"""

    def __init__(self, cog: "StatsCog", user_id: int, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.page = page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人の履歴は操作できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ 前", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(
            embed=await self.cog.history_embed(self.user_id, self.page),
            view=self,
        )

    @discord.ui.button(label="次 ▶", style=discord.ButtonStyle.secondary)
    async def next_(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(
            embed=await self.cog.history_embed(self.user_id, self.page),
            view=self,
        )


class StatsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # ── 履歴 ──
    async def history_embed(self, user_id: int, page: int) -> discord.Embed:
        db = self.bot.db
        offset = page * _PAGE_SIZE
        cur = await db.conn.execute(
            "SELECT delta, balance_after, reason, ref, ts FROM tx_logs "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, _PAGE_SIZE, offset),
        )
        rows = list(await cur.fetchall())
        bal = await db.get_balance(user_id)
        e = common.embed(
            f"📜 取引履歴 (p{page + 1})",
            _format_history_page(rows, self.bot.cfg),
            color=common.COLOR_INFO,
        )
        e.add_field(name="現在残高", value=common.money(self.bot.cfg, bal))
        if not rows and page > 0:
            e.set_footer(text="これ以上履歴はありません。")
        return e

    @app_commands.command(name="履歴", description="自分の取引履歴を表示")
    async def history(self, interaction: discord.Interaction) -> None:
        view = HistoryView(self, interaction.user.id, page=0)
        await interaction.response.send_message(
            embed=await self.history_embed(interaction.user.id, 0),
            view=view,
            ephemeral=True,
        )

    # ── 統計 ──
    @app_commands.command(name="統計", description="自分のプレイ統計を表示")
    async def stats(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        s = await db.user_stats(interaction.user.id)
        cfg = self.bot.cfg
        e = common.embed(
            f"📊 {interaction.user.display_name} の統計",
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
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(StatsCog(bot))
