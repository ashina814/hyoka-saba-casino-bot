"""全体JP(インフレ中立の再分配プール)。

設計のキモ:
- プレイヤーが「賭けた一部 (contrib%)」だけが原資になるので
  Bot側からの新規発行はゼロ → **インフレに対して完全中立**。
- 当選確率を `pool / full_speed` で自動的に上げていく。
  → プールが膨らみすぎる前に確率的に必ず吐ける = 自己調整。
- 当選時はプール全額を当選者に渡し、`seed` 額にリセット(通常0)。

呼び出し側(各PVEゲームの精算):
  contrib = global_jackpot.contribution(bot, bet)
  if contrib: await db.global_jp_add(contrib)
  if await global_jackpot.try_win(bot, user_id) のように使う
"""
from __future__ import annotations

import secrets

_RNG = secrets.SystemRandom()


def contribution(bot, bet: int) -> int:
    """このベットからプールへ積む額(切り捨て)。"""
    db = bot.db
    if not db.setting("global_jp_enabled", True):
        return 0
    rate = float(db.setting("global_jp_contrib", 0.005) or 0.005)
    return max(0, int(bet * rate))


def win_probability(bot, pool: int) -> float:
    """現プールに対する 1ベットあたり当選確率(0.0〜1.0)。"""
    full = int(bot.db.setting("global_jp_full_speed", 5_000_000) or 5_000_000)
    if full <= 0:
        return 0.0
    return min(1.0, pool / full)


async def try_win(bot, pool: int) -> bool:
    """このベットで当選するかをロール。
    secrets.SystemRandom を使い、確率は pool 依存(プールが大きいほど当たりやすい)。"""
    if not bot.db.setting("global_jp_enabled", True):
        return False
    p = win_probability(bot, pool)
    return _RNG.random() < p


async def hook_pve_bet(bot, user_id: int, bet: int) -> int:
    """PVEゲームの精算時に呼ぶ統一ヘルパー。

    1) ベットからプール積立を行う
    2) このベットで当選するかを判定し、当選なら **当選額をユーザーに付与** + プールリセット
    3) 当選時はお喋りログにも告知し、当選額を返す(0なら未当選)
    """
    db = bot.db
    if not db.setting("global_jp_enabled", True):
        return 0
    contrib = contribution(bot, bet)
    if contrib:
        await db.global_jp_add(contrib)
    pool = await db.global_jp_amount()
    if not await try_win(bot, pool):
        return 0

    # 当選! 加算 → リセット
    won = await db.global_jp_win(user_id)
    if won <= 0:
        return 0
    async with db.user_lock(user_id):
        await db.adjust_balance(user_id, won, "global_jp_win")

    # 通知
    from ui import common as _common
    user = bot.get_user(user_id)
    mention = user.mention if user else f"<@{user_id}>"
    e = _common.embed(
        "🌟 全体ジャックポット獲得 🌟",
        f"{mention} が **全体JP {won:,} 獲得** !!!\n"
        "次のプール積立はまた0から始まります。",
        color=_common.COLOR_JACKPOT,
    )
    await _common.post_casino_log(bot, embed=e)
    dm = _common.embed(
        "🌟 全体ジャックポット獲得！",
        f"全体JPを **{won:,}** 獲得しました！おめでとうございます🎉",
        color=_common.COLOR_JACKPOT,
    )
    await _common.dm_user(bot, user_id, dm)
    # 称号付与
    from core import badges as _badges
    await _badges.on_global_jp_won(bot, user_id)
    return won
