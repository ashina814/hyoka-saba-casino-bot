"""チンチロ(PVE)。親=Bot、子=プレイヤー。

進行:
- 両者それぞれ最大3回まで振り、役が出たら止める(目なしなら振り直し)。
- 役の強さ→同役は出目で比較。勝者の役の倍率で精算する。
  ピンゾロ×5 / ゾロ目×3 / シゴロ×2 / 目×1 / ヒフミは2倍払い。
- ハウスエッジ(chinchiro_house_edge)はプレイヤーの勝ち額にのみ掛けて実現。

演出: 親→子の順に各投を順次表示して引っ張る。
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from core import dice
from core.dice import ChinchiroRank
from db.dao import InsufficientFunds
from ui import common


def _throw_until_role(max_throws: int = 3) -> list[list[int]]:
    """役が出るまで(最大 max_throws 回)振り、各投の出目を返す。"""
    throws: list[list[int]] = []
    for _ in range(max_throws):
        vals = dice.roll(3)
        throws.append(vals)
        if dice.evaluate_chinchiro(vals).rank != ChinchiroRank.NO_ROLE:
            break
    return throws


class ChinchiroCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            common.BetModal(self.bot, "🎲 チンチロ — ベット", self._run)
        )

    @app_commands.command(name="チンチロ", description="親(Bot)とチンチロ勝負")
    @app_commands.describe(ベット="賭け額")
    async def cmd(self, interaction: discord.Interaction, ベット: int) -> None:
        err = common.validate_bet(self.bot, ベット)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        await self._run(interaction, ベット)

    async def _run(self, interaction: discord.Interaction, bet: int) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        user = interaction.user

        async with db.user_lock(user.id):
            if await db.is_frozen(user.id):
                await interaction.response.send_message(
                    "🧊 あなたは凍結中です。", ephemeral=True
                )
                return
            try:
                await db.adjust_balance(user.id, -bet, "chinchiro_bet")
            except InsufficientFunds:
                await interaction.response.send_message(
                    "残高が足りません。", ephemeral=True
                )
                return

        parent_throws = _throw_until_role()
        child_throws = _throw_until_role()
        parent_res = dice.evaluate_chinchiro(parent_throws[-1])
        child_res = dice.evaluate_chinchiro(child_throws[-1])

        def render(p_show: int, c_show: int, footer: str = "") -> discord.Embed:
            e = common.embed("🎲 チンチロ", color=common.COLOR_MAIN)
            p_lines = [dice.faces(t) for t in parent_throws[:p_show]]
            c_lines = [dice.faces(t) for t in child_throws[:c_show]]
            e.add_field(
                name="🤖 親(Bot)",
                value="\n".join(p_lines) or "…",
                inline=False,
            )
            e.add_field(
                name=f"🧑 子({user.display_name})",
                value="\n".join(c_lines) or "…",
                inline=False,
            )
            e.add_field(name="ベット", value=common.money(cfg, bet))
            if footer:
                e.set_footer(text=footer)
            return e

        # 親の投を順次表示
        await interaction.response.send_message(embed=render(0, 0, "親が振ります…"))
        for i in range(1, len(parent_throws) + 1):
            await asyncio.sleep(0.6)
            await interaction.edit_original_response(
                embed=render(i, 0, f"親の役: {parent_res.label if i == len(parent_throws) else '振り直し…'}")
            )
        # 子の投を順次表示
        for i in range(1, len(child_throws) + 1):
            await asyncio.sleep(0.6)
            await interaction.edit_original_response(
                embed=render(len(parent_throws), i, "あなたが振ります…")
            )

        await self._settle(interaction, user, bet, parent_res, child_res, render)

    async def _settle(self, interaction, user, bet, parent_res, child_res, render):
        db = self.bot.db
        cfg = self.bot.cfg
        edge = float(db.setting("chinchiro_house_edge", 0.05))
        cmp = dice.chinchiro_compare(child_res, parent_res)

        # 役倍率(勝者側)。子のヒフミは自滅で2倍払い。
        credit = 0          # ユーザーへ戻す総額(ベット返却含む)
        extra_loss = 0      # 追加で引かれる額(倍付け負け)
        if child_res.rank == ChinchiroRank.HIFUMI and cmp <= 0:
            extra_loss = bet  # 計2倍払い(既に1倍は徴収済み)
            outcome, color = "ヒフミ… 2倍払い", common.COLOR_LOSE
        elif cmp > 0:
            mult = max(1, child_res.payout_mult)
            win = int(bet * mult * (1 - edge))
            credit = bet + win
            outcome, color = f"勝ち！ ×{mult}", common.COLOR_WIN
        elif cmp < 0:
            mult = max(1, parent_res.payout_mult)
            if mult > 1:
                extra_loss = bet * (mult - 1)
            outcome, color = f"負け… ×{mult}", common.COLOR_LOSE
        else:
            credit = bet  # 引き分け→ベット返却
            outcome, color = "引き分け(ベット返却)", common.COLOR_INFO

        async with db.user_lock(user.id):
            if extra_loss:
                await db.adjust_balance(
                    user.id, -extra_loss, "chinchiro_bet", allow_negative=True
                )
            if credit:
                await db.adjust_balance(user.id, credit, "chinchiro_win")
            row = await db.ensure_user(user.id)
            streak = int(row["win_streak"])
            streak = streak + 1 if cmp > 0 else 0
            await db.set_win_streak(user.id, streak)
            new_balance = int((await db.ensure_user(user.id))["balance"])

        e = render(99, 99)
        e.color = color
        e.title = "🎲 チンチロ — 結果"
        e.clear_fields()
        e.add_field(name="🤖 親", value=parent_res.label, inline=True)
        e.add_field(name="🧑 子", value=child_res.label, inline=True)
        e.add_field(name="判定", value=outcome, inline=False)
        net = credit - bet - extra_loss
        e.add_field(name="収支", value=("📈 +" if net >= 0 else "📉 ") + f"{net:,}")
        e.add_field(name="残高", value=common.money(cfg, new_balance))
        if streak >= 2 and cmp > 0:
            e.set_footer(text=f"🔥 {streak}連勝中！")
        await interaction.edit_original_response(embed=e)


async def setup(bot) -> None:
    await bot.add_cog(ChinchiroCog(bot))
