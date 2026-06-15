"""残高・デイリー・送金・ランキング。

スラッシュコマンドに加え、ハブパネルのボタンからも同じ処理を呼べるよう、
ロジックは Cog のメソッドに切り出してある。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import economy
from db.dao import InsufficientFunds
from ui import common


class EconomyCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # ───────────────────────── 共有ロジック ─────────────────────────
    async def build_balance_embed(self, user: discord.abc.User) -> discord.Embed:
        db = self.bot.db
        row = await db.ensure_user(user.id)
        e = common.embed(
            f"{user.display_name} の財布",
            color=common.COLOR_MAIN,
        )
        e.add_field(name="残高", value=common.money(self.bot.cfg, int(row["balance"])))
        if row["win_streak"]:
            e.add_field(name="連勝", value=f"🔥 {row['win_streak']}")
        if row["frozen"]:
            e.add_field(name="状態", value="🧊 凍結中", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        return e

    async def claim_daily(self, user: discord.abc.User) -> discord.Embed:
        db = self.bot.db
        async with db.user_lock(user.id):
            row = await db.ensure_user(user.id)
            amount, new_streak, msg = economy.compute_daily(
                db, int(row["balance"]), int(row["daily_streak"]), row["last_daily"]
            )
            if amount <= 0:
                return common.embed(
                    "デイリー", f"⏳ {msg}", color=common.COLOR_INFO
                )
            ts = economy.now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            # last_daily/streak/balance/tx_logs を 1トランザクションで更新。
            # 以前は update_daily と adjust_balance を別 commit していて、
            # 前者だけ通った場合に「もう受け取った扱いだが残高は据え置き」の
            # 不整合が起きていた。アトミック化で防ぐ。
            new_balance = await db.pay_daily(user.id, amount, new_streak, ts)
        e = common.embed(
            "デイリー受け取り", f"🎁 {msg}", color=common.COLOR_WIN
        )
        e.add_field(name="受給", value=common.money(self.bot.cfg, amount))
        e.add_field(name="残高", value=common.money(self.bot.cfg, new_balance))
        e.set_footer(text=f"連続ログイン {new_streak} 日目")
        return e

    async def build_leaderboard_embed(self) -> discord.Embed:
        rows = await self.bot.db.leaderboard(10)
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = []
        for i, r in enumerate(rows):
            user = self.bot.get_user(int(r["user_id"]))
            name = user.display_name if user else f"ユーザー{r['user_id']}"
            lines.append(
                f"{medals[i]} **{name}** — {common.money(self.bot.cfg, int(r['balance']))}"
            )
        return common.embed(
            "💰 長者番付",
            "\n".join(lines) or "まだ誰もいません。",
            color=common.COLOR_MAIN,
        )

    # ───────────────────────── スラッシュコマンド ─────────────────────────
    # 残高/デイリー/ランキングは /カジノ パネル経由に集約。
    # /送金 だけはメンション指定が必須なのでテキストコマンドを残す。
    @app_commands.command(name="送金", description="他のプレイヤーにチップを送る")
    @app_commands.describe(相手="送り先", 金額="送る額")
    async def transfer(
        self, interaction: discord.Interaction, 相手: discord.User, 金額: int
    ) -> None:
        if 相手.bot or 相手.id == interaction.user.id:
            await interaction.response.send_message(
                "そのユーザーには送金できません。", ephemeral=True
            )
            return
        if 金額 <= 0:
            await interaction.response.send_message(
                "金額は正の数で指定してください。", ephemeral=True
            )
            return
        db = self.bot.db
        # デッドロック回避のため id 昇順でロックを取得
        a, b = sorted((interaction.user.id, 相手.id))
        async with db.user_lock(a), db.user_lock(b):
            if await db.is_frozen(interaction.user.id):
                await interaction.response.send_message(
                    "あなたは凍結中のため送金できません。", ephemeral=True
                )
                return
            try:
                await db.adjust_balance(
                    interaction.user.id, -金額, "transfer_out", str(相手.id)
                )
            except InsufficientFunds:
                await interaction.response.send_message(
                    "残高が足りません。", ephemeral=True
                )
                return
            await db.adjust_balance(
                相手.id, 金額, "transfer_in", str(interaction.user.id)
            )
        e = common.embed("送金完了", color=common.COLOR_WIN)
        e.description = (
            f"{interaction.user.mention} → {相手.mention}\n"
            f"{common.money(self.bot.cfg, 金額)}"
        )
        await interaction.response.send_message(embed=e)


async def setup(bot) -> None:
    await bot.add_cog(EconomyCog(bot))
