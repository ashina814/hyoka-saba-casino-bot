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
# (app_commands removed: no slash commands here anymore)
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

    # ハブ/コマンド共通の入口: ベットプリセット画面を出す
    async def entry(self, interaction: discord.Interaction) -> None:
        await common.send_bet_panel(
            interaction, self.bot, self._run,
            title="🎰 スロット — ベット", game_key="slot",
        )

    async def _run(self, interaction: discord.Interaction, bet: int) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        user = interaction.user

        # 自己制限チェック(凍結よりも先に伝える)
        if await common.self_limit_guard(interaction, bet):
            return
        async with db.user_lock(user.id):
            if await db.is_frozen(user.id):
                await common.respond_with(
                    interaction, content="🧊 あなたは凍結中です。", ephemeral=True
                )
                return
            try:
                await db.adjust_balance(user.id, -bet, "slot_bet")
            except InsufficientFunds:
                await common.respond_with(
                    interaction, content="残高が足りません。", ephemeral=True
                )
                return
            # ベットの一部をジャックポットへ積む(再分配)
            contrib = economy.jackpot_contribution(db, bet)
            if contrib:
                await db.jackpot_add(contrib)
        # 前回ベット記憶(連戦の default にする)
        try:
            await db.set_last_bet(user.id, "slot", bet)
        except Exception:  # noqa: BLE001
            pass
        # 全体JP: スロット以外の場所でもフックされる横串。当選時は即配布。
        from core import global_jackpot as _gjp
        await _gjp.hook_pve_bet(self.bot, user.id, bet)

        # 抽選(結果は先に確定。演出だけ後で順次表示)
        reels = [_spin_reel() for _ in range(3)]

        # ── 段階表示(脳汁演出) ──
        def render(shown: int) -> discord.Embed:
            cells = [reels[i] if i < shown else "❓" for i in range(3)]
            e = common.embed("🎰 スロット", color=common.COLOR_MAIN)
            e.description = f"# ｜ {' ｜ '.join(cells)} ｜"
            e.add_field(name="ベット", value=common.money(cfg, bet))
            return e

        # 初回(モーダル経由)は response.send_message、
        # 「もう一回」(既に response 済み)は followup.send で送る。
        # 送信したメッセージを以降 msg.edit で段階表示する。
        msg = await common.respond_with(interaction, embed=render(0))
        if msg is None:
            msg = await interaction.original_response()
        for shown in range(1, 4):
            await asyncio.sleep(0.7)
            emb = render(shown)
            if shown == 2 and reels[0] == reels[1] and reels[0] in (
                "7️⃣", JACKPOT_SYMBOL, "⭐"
            ):
                emb.set_footer(text="🔥 リーチ！あと1つ…！")
            try:
                await msg.edit(embed=emb)
            except discord.HTTPException:
                pass

        # ── 精算 ──
        await self._settle(msg, user, bet, reels)

    async def _settle(self, msg, user, bet: int, reels: list[str]) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        target_rtp = max(0.0, 1.0 - float(db.setting("slot_house_edge", 0.05)))
        scale = (target_rtp / _BASE_RTP) if _BASE_RTP > 0 else 1.0
        # 運営ブースト(配当のみに適用、JP獲得額はプール元本なので非適用)
        boost = common.boost_multiplier(self.bot)
        scale *= boost

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
        # JP当選やストリーク達成をお喋りログ&DM通知
        await self._announce(user, bet, total_credit, jackpot_won, streak)
        # 称号判定
        from core import badges as _badges
        if jackpot_won:
            await _badges.on_jackpot_won(self.bot, user.id)
        if streak > 0:
            await _badges.on_streak(self.bot, user.id, streak)
        await _badges.on_bet(self.bot, user.id)

        view = common.PlayAgainView(self.bot, user.id, bet, self._run)
        try:
            await msg.edit(embed=e, view=view)
        except discord.HTTPException:
            pass


    async def _announce(self, user, bet: int, total_credit: int,
                        jackpot_won: int, streak: int) -> None:
        """JP当選とストリーク節目だけ お喋りログ&DMで派手に通知。"""
        cfg = self.bot.cfg
        if jackpot_won:
            e = common.embed(
                "💎 JACKPOT 💎",
                f"{user.mention} がスロットで **ジャックポット {jackpot_won:,} 獲得**！",
                color=common.COLOR_JACKPOT,
            )
            await common.post_casino_log(self.bot, embed=e)
            dm = common.embed(
                "💎 ジャックポット獲得！",
                f"スロットで **{jackpot_won:,}** のジャックポットを獲得しました！\n"
                f"おめでとうございます🎉",
                color=common.COLOR_JACKPOT,
            )
            await common.dm_user(self.bot, user.id, dm)
        elif streak in (5, 10, 20, 50):
            e = common.embed(
                "🔥 連勝記録",
                f"{user.mention} がスロットで **{streak}連勝** を達成！",
                color=common.COLOR_WIN,
            )
            await common.post_casino_log(self.bot, embed=e)


async def setup(bot) -> None:
    await bot.add_cog(SlotCog(bot))
