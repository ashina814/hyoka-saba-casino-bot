"""PVP 用の共通ロビー View(Cog ではなく部品)。

参加/退出でエスクローを管理し、主催者の開始で on_start コールバックへ委譲する。
DrawState / HoldemState など、players(list)・host_id・bet・match_id・pot を
備えた状態オブジェクトと組み合わせて使う。
"""
from __future__ import annotations

import discord

from core import match
from ui import common


class LobbyView(discord.ui.View):
    def __init__(self, cog, state, min_players: int, max_players: int, on_start) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.st = state
        self.min_players = min_players
        self.max_players = max_players
        self.on_start = on_start
        self._title = "PVP"

    def embed(self, title: str | None = None) -> discord.Embed:
        if title:
            self._title = title
        cfg = self.cog.bot.cfg
        st = self.st
        names = "\n".join(f"・<@{u}>" for u in st.players) or "—"
        e = common.embed(
            self._title,
            f"参加費 **{common.money(cfg, st.bet)}**。"
            f"{self.min_players}〜{self.max_players}人。主催者が『開始』で始めます。",
            color=common.COLOR_INFO,
        )
        e.add_field(
            name=f"参加者 ({len(st.players)}/{self.max_players})", value=names, inline=False
        )
        e.add_field(name="ポット", value=common.money(cfg, st.pot))
        return e

    @discord.ui.button(label="参加", emoji="✅", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        db = self.cog.bot.db
        uid = interaction.user.id
        if st.started:
            await interaction.response.send_message("開始済みです。", ephemeral=True)
            return
        if uid in st.players:
            await interaction.response.send_message("参加済みです。", ephemeral=True)
            return
        if len(st.players) >= self.max_players:
            await interaction.response.send_message("満員です。", ephemeral=True)
            return
        async with db.user_lock(uid):
            ok = await match.escrow_take(db, uid, st.bet, st.match_id)
        if not ok:
            await interaction.response.send_message(
                "残高不足または凍結中で参加できません。", ephemeral=True
            )
            return
        st.players.append(uid)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="退出", emoji="🚪", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        uid = interaction.user.id
        if st.started or uid not in st.players:
            await interaction.response.send_message("退出できません。", ephemeral=True)
            return
        st.players.remove(uid)
        await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="開始", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        if interaction.user.id != st.host_id:
            await interaction.response.send_message(
                "主催者のみ開始できます。", ephemeral=True
            )
            return
        if len(st.players) < self.min_players:
            await interaction.response.send_message(
                f"あと {self.min_players - len(st.players)} 人必要です。", ephemeral=True
            )
            return
        for item in self.children:
            item.disabled = True
        await self.on_start(interaction, st)
        self.stop()

    @discord.ui.button(label="解散", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        if interaction.user.id != st.host_id:
            await interaction.response.send_message(
                "主催者のみ操作できます。", ephemeral=True
            )
            return
        if st.started:
            await interaction.response.send_message(
                "開始後は解散できません。", ephemeral=True
            )
            return
        for uid in list(st.players):
            await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
        self.cog.matches.pop(st.match_id, None)
        e = common.embed("解散", "全額返金しました。", color=common.COLOR_INFO)
        await interaction.response.edit_message(embed=e, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        st = self.st
        if not st.started:
            for uid in list(st.players):
                await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
            self.cog.matches.pop(st.match_id, None)
