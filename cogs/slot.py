"""スロット(PVE)。

射幸性(脳汁)設計:
- リールを1つずつ開いて見せる(順次 edit)。
- ニアミス(高配当シンボルが2つ揃い)を明示して「惜しい!」を演出。
- プログレッシブ・ジャックポット(💎×3)。原資はベットからの積立=インフレ中立。
- 連勝ストリークを煽る。

公平性/経済設計:
- ハウスエッジは settings(slot_house_edge)で可変。配当はペイテーブルの
  素のRTPを target=(1-house_edge) に合わせて毎スピン動的にスケールするので、
  管理者がエッジをいじれば実効RTPもそのまま追従する。
- 乱数は secrets.SystemRandom。
"""
from __future__ import annotations

import asyncio
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from core import economy
from db.dao import InsufficientFunds
from ui import common

_RNG = secrets.SystemRandom()

# (シンボル, リール内の重み)。3リール共通。
SYMBOLS: list[tuple[str, int]] = [
    ("🍒", 30),
    ("🍋", 25),
    ("🔔", 20),
    ("⭐", 12),
    ("7️⃣", 8),
    ("💎", 5),
]
_TOTAL_W = sum(w for _, w in SYMBOLS)

# 3つ揃いの基本配当倍率(ベットに対する払い戻し倍率)。💎×3 はJP扱いで別。
PAYOUT3 = {"🍒": 5, "🍋": 8, "🔔": 12, "⭐": 25, "7️⃣": 75}
CHERRY2_MULT = 2          # 🍒 がちょうど2つ
JACKPOT_SYMBOL = "💎"     # 3つ揃いでジャックポット


def _spin_reel() -> str:
    r = _RNG.randint(1, _TOTAL_W)
    acc = 0
    for sym, w in SYMBOLS:
        acc += w
        if r <= acc:
            return sym
    return SYMBOLS[-1][0]


def _base_rtp() -> float:
    """ジャックポットを除いた素のRTP(ペイテーブル期待値)。スケール基準に使う。"""
    rtp = 0.0
    for sym, w in SYMBOLS:
        p = w / _TOTAL_W
        if sym in PAYOUT3:
            rtp += (p ** 3) * PAYOUT3[sym]
    # 🍒 がちょうど2つ
    pc = next(w for s, w in SYMBOLS if s == "🍒") / _TOTAL_W
    p_exactly2 = 3 * (pc ** 2) * (1 - pc)
    rtp += p_exactly2 * CHERRY2_MULT
    return rtp


_BASE_RTP = _base_rtp()


class SlotCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # ハブ/コマンド共通の入口: ベットモーダルを開く
    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            common.BetModal(self.bot, "🎰 スロット — ベット", self._run)
        )

    @app_commands.command(name="スロット", description="スロットを回す")
    @app_commands.describe(ベット="賭け額")
    async def slot_cmd(self, interaction: discord.Interaction, ベット: int) -> None:
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
                await db.adjust_balance(user.id, -bet, "slot_bet")
            except InsufficientFunds:
                await interaction.response.send_message(
                    "残高が足りません。", ephemeral=True
                )
                return
            # ベットの一部をジャックポットへ積む(再分配)
            contrib = economy.jackpot_contribution(db, bet)
            if contrib:
                await db.jackpot_add(contrib)

        # 抽選(結果は先に確定。演出だけ後で順次表示)
        reels = [_spin_reel() for _ in range(3)]

        # ── 段階表示(脳汁演出) ──
        def render(shown: int) -> discord.Embed:
            cells = [reels[i] if i < shown else "❓" for i in range(3)]
            e = common.embed("🎰 スロット", color=common.COLOR_MAIN)
            e.description = f"# ｜ {' ｜ '.join(cells)} ｜"
            e.add_field(name="ベット", value=common.money(cfg, bet))
            return e

        await interaction.response.send_message(embed=render(0))
        for shown in range(1, 4):
            await asyncio.sleep(0.7)
            emb = render(shown)
            if shown == 2 and reels[0] == reels[1] and reels[0] in (
                "7️⃣", JACKPOT_SYMBOL, "⭐"
            ):
                emb.set_footer(text="🔥 リーチ！あと1つ…！")
            await interaction.edit_original_response(embed=emb)

        # ── 精算 ──
        await self._settle(interaction, user, bet, reels)

    async def _settle(self, interaction, user, bet: int, reels: list[str]) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        target_rtp = max(0.0, 1.0 - float(db.setting("slot_house_edge", 0.05)))
        scale = (target_rtp / _BASE_RTP) if _BASE_RTP > 0 else 1.0

        payout = 0
        jackpot_won = 0
        title = "🎰 スロット — 結果"
        color = common.COLOR_LOSE
        note = ""

        if reels[0] == reels[1] == reels[2]:
            sym = reels[0]
            if sym == JACKPOT_SYMBOL and db.setting("jackpot_enabled", True):
                jackpot_won = await db.jackpot_amount("slot")
                seed = int(db.setting("jackpot_seed", 10000))
                await db.jackpot_reset(seed)
                color = common.COLOR_JACKPOT
                title = "💎 JACKPOT 💎"
                note = "**大当たり！ジャックポット獲得！！**"
            elif sym in PAYOUT3:
                payout = int(bet * PAYOUT3[sym] * scale)
                color = common.COLOR_WIN
                note = f"**{sym}×3！**"
        elif reels.count("🍒") == 2:
            payout = int(bet * CHERRY2_MULT * scale)
            color = common.COLOR_WIN
            note = "🍒 が2つ！"
        else:
            # ニアミス検出(高配当が2つ)
            for hot in ("💎", "7️⃣", "⭐"):
                if reels.count(hot) == 2:
                    note = f"惜しい！ {hot} があと1つで大当たり…！"
                    break

        total_credit = payout + jackpot_won
        async with db.user_lock(user.id):
            if total_credit:
                reason = "slot_jackpot" if jackpot_won else "slot_win"
                new_balance = await db.adjust_balance(user.id, total_credit, reason)
            else:
                new_balance = await db.get_balance(user.id)
            # 連勝ストリーク更新
            row = await db.ensure_user(user.id)
            streak = int(row["win_streak"])
            streak = streak + 1 if total_credit > 0 else 0
            await db.set_win_streak(user.id, streak)

        e = common.embed(title, color=color)
        e.description = f"# ｜ {' ｜ '.join(reels)} ｜"
        if note:
            e.add_field(name="​", value=note, inline=False)
        net = total_credit - bet
        e.add_field(name="ベット", value=common.money(cfg, bet))
        e.add_field(
            name="払い戻し",
            value=common.money(cfg, total_credit) if total_credit else "—",
        )
        e.add_field(
            name="収支",
            value=("📈 +" if net >= 0 else "📉 ") + f"{net:,}",
        )
        e.add_field(name="残高", value=common.money(cfg, new_balance))
        if streak >= 2 and total_credit > 0:
            e.set_footer(text=f"🔥 {streak}連勝中！")
        elif not jackpot_won and db.setting("jackpot_enabled", True):
            jp = await db.jackpot_amount("slot")
            e.set_footer(text=f"💎 現在のジャックポット: {jp:,}")
        await interaction.edit_original_response(embed=e)


async def setup(bot) -> None:
    await bot.add_cog(SlotCog(bot))
