"""丁半(PVP)。プレイヤーが「丁」か「半」に同額ベットして対戦する。

進行:
- 主催者が場(ベット額)を立てる → パネルに丁/半ボタン。
- 各プレイヤーがどちらかにベット(参加時にエスクロー)。
- 主催者が締切 → サイコロ2個。偶数=丁、奇数=半。
- 勝った側が負けた側の賭け金を山分け(レーキを差し引いてシンク化)。
  片側が空なら不成立で全額返金。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import dice, economy, match
from ui import common


class ChohanState:
    def __init__(self, match_id: str, host_id: int, bet: int) -> None:
        self.match_id = match_id
        self.host_id = host_id
        self.bet = bet
        self.cho: set[int] = set()
        self.han: set[int] = set()
        self.closed = False

    def has(self, uid: int) -> bool:
        return uid in self.cho or uid in self.han


class ChohanView(discord.ui.View):
    def __init__(self, cog: "ChohanCog", state: ChohanState) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.state = state

    async def _join(self, interaction: discord.Interaction, side: str) -> None:
        st = self.state
        db = self.cog.bot.db
        uid = interaction.user.id
        if st.closed:
            await interaction.response.send_message("締切済みです。", ephemeral=True)
            return
        if st.has(uid):
            await interaction.response.send_message(
                "すでに参加しています。", ephemeral=True
            )
            return
        async with db.user_lock(uid):
            ok = await match.escrow_take(db, uid, st.bet, st.match_id)
        if not ok:
            await interaction.response.send_message(
                "残高不足または凍結中で参加できません。", ephemeral=True
            )
            return
        (st.cho if side == "cho" else st.han).add(uid)
        await interaction.response.edit_message(embed=self.cog.lobby_embed(st), view=self)

    @discord.ui.button(label="丁 (偶数)", emoji="🔵", style=discord.ButtonStyle.primary)
    async def cho(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._join(interaction, "cho")

    @discord.ui.button(label="半 (奇数)", emoji="🔴", style=discord.ButtonStyle.primary)
    async def han(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._join(interaction, "han")

    @discord.ui.button(label="抜ける", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.state
        uid = interaction.user.id
        if st.closed or not st.has(uid):
            await interaction.response.send_message(
                "退出できません。", ephemeral=True
            )
            return
        st.cho.discard(uid)
        st.han.discard(uid)
        await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
        await interaction.response.edit_message(embed=self.cog.lobby_embed(st), view=self)

    @discord.ui.button(label="締切＆勝負", emoji="🎲", style=discord.ButtonStyle.success)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.state
        if interaction.user.id != st.host_id:
            await interaction.response.send_message(
                "主催者のみ操作できます。", ephemeral=True
            )
            return
        if st.closed:
            return
        st.closed = True
        for item in self.children:
            item.disabled = True
        await self.cog.settle(interaction, st)
        self.stop()

    @discord.ui.button(label="解散", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.state
        if interaction.user.id != st.host_id:
            await interaction.response.send_message(
                "主催者のみ操作できます。", ephemeral=True
            )
            return
        st.closed = True
        for uid in list(st.cho | st.han):
            await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
        self.cog.matches.pop(st.match_id, None)
        e = common.embed("丁半 — 解散", "全額返金しました。", color=common.COLOR_INFO)
        await interaction.response.edit_message(embed=e, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        st = self.state
        if not st.closed:
            for uid in list(st.cho | st.han):
                await match.escrow_refund(self.cog.bot.db, uid, st.bet, st.match_id)
            self.cog.matches.pop(st.match_id, None)


class ChohanCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.matches: dict[str, ChohanState] = {}

    def lobby_embed(self, st: ChohanState) -> discord.Embed:
        cfg = self.bot.cfg

        def names(ids: set[int]) -> str:
            if not ids:
                return "—"
            return "\n".join(
                f"<@{u}>" for u in ids
            )

        e = common.embed(
            "⚂ 丁半",
            f"1口 **{common.money(cfg, st.bet)}** で参加。丁か半を選べ！",
            color=common.COLOR_INFO,
        )
        e.add_field(name=f"丁 ({len(st.cho)})", value=names(st.cho))
        e.add_field(name=f"半 ({len(st.han)})", value=names(st.han))
        e.set_footer(text="主催者が『締切＆勝負』で勝負開始")
        return e

    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            common.BetModal(self.bot, "⚂ 丁半 — 場を立てる", self._create)
        )

    @app_commands.command(name="丁半", description="丁半の場を立てる(PVP)")
    @app_commands.describe(ベット="1口の賭け額")
    async def cmd(self, interaction: discord.Interaction, ベット: int) -> None:
        err = common.validate_bet(self.bot, ベット)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        await self._create(interaction, ベット)

    async def _create(self, interaction: discord.Interaction, bet: int) -> None:
        mid = match.new_match_id("chohan")
        st = ChohanState(mid, interaction.user.id, bet)
        self.matches[mid] = st
        view = ChohanView(self, st)
        await interaction.response.send_message(embed=self.lobby_embed(st), view=view)

    async def settle(self, interaction: discord.Interaction, st: ChohanState) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        vals, is_cho = dice.chohan_roll()
        result = "丁 (偶数)" if is_cho else "半 (奇数)"
        winners = st.cho if is_cho else st.han
        losers = st.han if is_cho else st.cho

        e = common.embed("⚂ 丁半 — 結果", color=common.COLOR_MAIN)
        e.add_field(name="出目", value=f"{dice.faces(vals)}  合計 {sum(vals)}", inline=False)
        e.add_field(name="結果", value=f"**{result}**", inline=False)

        # 不成立(片側空)→ 全返金
        if not winners or not losers:
            for uid in list(st.cho | st.han):
                await match.escrow_refund(db, uid, st.bet, st.match_id)
            e.description = "片側に賭け手がいないため**不成立**。全額返金しました。"
            e.color = common.COLOR_INFO
            self.matches.pop(st.match_id, None)
            await interaction.response.edit_message(embed=e, view=None)
            return

        pot_lose = st.bet * len(losers)
        rake = economy.rake(db, pot_lose)         # シンク(消滅)
        distributable = pot_lose - rake
        share = distributable // len(winners)

        for uid in winners:
            async with db.user_lock(uid):
                await db.adjust_balance(uid, st.bet + share, "pvp_win", st.match_id)
                await match.clear_active(db, uid)
            row = await db.ensure_user(uid)
            await db.set_win_streak(uid, int(row["win_streak"]) + 1)
        for uid in losers:
            async with db.user_lock(uid):
                await match.clear_active(db, uid)
            await db.set_win_streak(uid, 0)

        win_lines = "\n".join(
            f"<@{u}> +{common.money(cfg, st.bet + share)}" for u in winners
        )
        e.color = common.COLOR_WIN
        e.add_field(name="勝者", value=win_lines, inline=False)
        e.set_footer(text=f"1人あたり配当 +{share:,}(レーキ {rake:,} を徴収)")
        self.matches.pop(st.match_id, None)
        await interaction.response.edit_message(embed=e, view=None)


async def setup(bot) -> None:
    await bot.add_cog(ChohanCog(bot))
