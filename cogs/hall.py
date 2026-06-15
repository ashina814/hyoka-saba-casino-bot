"""🏛️ 殿堂 — 歴代記録の掲示。

3タブ:
- 💎 歴代JP獲得 (tx_logs の slot_jackpot/global_jp_win から)
- 🔥 最高連勝   (users.max_win_streak)
- 🏆 大会優勝者 (tournaments.winners JSON)
"""
from __future__ import annotations

import json

import discord
from discord.ext import commands

from ui import common


class HallView(discord.ui.View):
    def __init__(self, cog: "HallCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.tab = "jp"
        self._sync()

    def _sync(self) -> None:
        for item in self.children:
            cid = getattr(item, "custom_id", "")
            if cid == f"hall:{self.tab}":
                item.style = discord.ButtonStyle.primary
            elif cid and cid.startswith("hall:"):
                item.style = discord.ButtonStyle.secondary

    async def _switch(self, interaction: discord.Interaction, tab: str) -> None:
        self.tab = tab
        self._sync()
        await interaction.response.edit_message(
            embed=await self.cog.build_embed(tab), view=self
        )

    @discord.ui.button(label="💎 歴代JP", custom_id="hall:jp",
                       style=discord.ButtonStyle.primary)
    async def jp(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "jp")

    @discord.ui.button(label="🔥 最高連勝", custom_id="hall:streak",
                       style=discord.ButtonStyle.secondary)
    async def streak(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "streak")

    @discord.ui.button(label="🏆 大会優勝者", custom_id="hall:tour",
                       style=discord.ButtonStyle.secondary)
    async def tour(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._switch(interaction, "tour")


class HallCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def build_embed(self, tab: str) -> discord.Embed:
        if tab == "streak":
            return await self._streak_embed()
        if tab == "tour":
            return await self._tour_embed()
        return await self._jp_embed()

    async def _jp_embed(self) -> discord.Embed:
        db = self.bot.db
        cur = await db.conn.execute(
            "SELECT user_id, delta, reason, ts FROM tx_logs "
            "WHERE reason IN ('slot_jackpot','global_jp_win') "
            "ORDER BY delta DESC LIMIT 10"
        )
        rows = list(await cur.fetchall())
        e = common.embed("🏛️ 殿堂 — 💎 歴代JP獲得 TOP10",
                         color=common.COLOR_JACKPOT)
        if not rows:
            e.description = "まだ誰もJPを獲得していません。"
            return e
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        for i, r in enumerate(rows):
            mark = "🌟" if r["reason"] == "global_jp_win" else "💎"
            e.add_field(
                name=f"{medals[i]} <@{r['user_id']}>",
                value=f"{mark} **{int(r['delta']):,}**  `{r['ts'][:16]}`",
                inline=False,
            )
        return e

    async def _streak_embed(self) -> discord.Embed:
        db = self.bot.db
        cur = await db.conn.execute(
            "SELECT user_id, max_win_streak FROM users "
            "WHERE max_win_streak > 0 ORDER BY max_win_streak DESC LIMIT 10"
        )
        rows = list(await cur.fetchall())
        e = common.embed("🏛️ 殿堂 — 🔥 最高連勝 TOP10",
                         color=common.COLOR_WIN)
        if not rows:
            e.description = "まだ連勝記録がありません。"
            return e
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        for i, r in enumerate(rows):
            e.add_field(
                name=f"{medals[i]} <@{r['user_id']}>",
                value=f"🔥 **{int(r['max_win_streak'])} 連勝**",
                inline=False,
            )
        return e

    async def _tour_embed(self) -> discord.Embed:
        db = self.bot.db
        cur = await db.conn.execute(
            "SELECT id, name, kind, prize_pool, winners, ended_at, created_at "
            "FROM tournaments WHERE status='finished' AND winners IS NOT NULL "
            "ORDER BY id DESC LIMIT 8"
        )
        rows = list(await cur.fetchall())
        e = common.embed("🏛️ 殿堂 — 🏆 大会優勝者",
                         color=common.COLOR_JACKPOT)
        if not rows:
            e.description = "まだ大会の優勝者がいません。"
            return e
        from cogs.tournament import KIND_LABEL
        for r in rows:
            try:
                winners = json.loads(r["winners"]) or []
            except (TypeError, ValueError):
                winners = []
            if not winners:
                continue
            top = winners[0]
            e.add_field(
                name=f"{r['name']}  ({KIND_LABEL.get(r['kind'], r['kind'])})",
                value=(
                    f"🥇 <@{top['user_id']}>  賞金 +{int(top['prize']):,}\n"
                    f"スコア {int(top['score']):,}"
                ),
                inline=False,
            )
        return e

    async def entry(self, interaction: discord.Interaction) -> None:
        view = HallView(self)
        await interaction.response.send_message(
            embed=await self.build_embed("jp"), view=view, ephemeral=True
        )


async def setup(bot) -> None:
    await bot.add_cog(HallCog(bot))
