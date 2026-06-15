"""称号の定義と達成判定。

設計:
- 達成判定はゲーム精算後にフック関数で呼ぶ。tx_logs を都度集計せず、
  「いま発生したイベント」(JP当選/連勝N達成 等)から判定するシンプル方式。
- 称号は完全に表示用(自慢用)。経済影響なし。プロフィールに並べる。
- 新称号を追加する時は BADGES に1行足すだけ。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Badge:
    id: str
    label: str
    emoji: str
    description: str


# 全称号定義(中央化)。順序は表示順。
BADGES: list[Badge] = [
    Badge("first_jp",         "ジャックポット初獲得", "💎", "スロットJPを1回でも獲得"),
    Badge("first_global_jp",  "全体JPの主",           "🌟", "全体JPを獲得した"),
    Badge("streak_10",        "10連勝",               "🔥", "10連勝達成"),
    Badge("streak_50",        "炎の50連勝",           "🔥🔥", "50連勝達成"),
    Badge("streak_100",       "伝説の100連勝",         "🔥🔥🔥", "100連勝達成"),
    Badge("bj_natural",       "ナチュラルブラックジャック", "🃏", "BJでナチュラル1.5倍を出した"),
    Badge("tournament_winner","大会優勝者",            "🏆", "大会で1位を獲得"),
    Badge("daily_streak_30",  "皆勤30日",             "📅", "ログイン30日連続達成"),
    Badge("mega_better",      "メガベッター",          "💰", "累計100万以上をベット"),
    Badge("omikuji_oo",       "大吉引いた",            "🎴", "おみくじで大吉を引いた"),
]

BADGE_BY_ID = {b.id: b for b in BADGES}


def badge_label(badge_id: str) -> str:
    b = BADGE_BY_ID.get(badge_id)
    if not b:
        return badge_id
    return f"{b.emoji} {b.label}"


async def _award_and_notify(bot, user_id: int, badge_id: str) -> None:
    """付与に成功(=新規取得)したら、お喋りログに通知。"""
    if await bot.db.award_badge(user_id, badge_id):
        from ui import common as _common
        b = BADGE_BY_ID.get(badge_id)
        if not b:
            return
        user = bot.get_user(user_id)
        mention = user.mention if user else f"<@{user_id}>"
        e = _common.embed(
            f"🏅 称号獲得: {b.emoji} {b.label}",
            f"{mention} が「{b.label}」を獲得！\n_{b.description}_",
            color=_common.COLOR_INFO,
        )
        await _common.post_casino_log(bot, embed=e)


async def on_jackpot_won(bot, user_id: int) -> None:
    await _award_and_notify(bot, user_id, "first_jp")


async def on_global_jp_won(bot, user_id: int) -> None:
    await _award_and_notify(bot, user_id, "first_global_jp")


async def on_streak(bot, user_id: int, streak: int) -> None:
    if streak >= 100:
        await _award_and_notify(bot, user_id, "streak_100")
    if streak >= 50:
        await _award_and_notify(bot, user_id, "streak_50")
    if streak >= 10:
        await _award_and_notify(bot, user_id, "streak_10")


async def on_bj_natural(bot, user_id: int) -> None:
    await _award_and_notify(bot, user_id, "bj_natural")


async def on_tournament_winner(bot, user_id: int) -> None:
    await _award_and_notify(bot, user_id, "tournament_winner")


async def on_daily_streak(bot, user_id: int, streak: int) -> None:
    if streak >= 30:
        await _award_and_notify(bot, user_id, "daily_streak_30")


async def on_omikuji_oo(bot, user_id: int) -> None:
    await _award_and_notify(bot, user_id, "omikuji_oo")


async def on_bet(bot, user_id: int) -> None:
    """累計ベット額の閾値判定。tx_logs を引いて判断。"""
    db = bot.db
    cur = await db.conn.execute(
        "SELECT COALESCE(-SUM(delta),0) v FROM tx_logs WHERE user_id=? "
        "AND delta < 0 AND reason IN "
        "('slot_bet','chinchiro_bet','hilo_bet','blackjack_bet',"
        "'blackjack_double','blackjack_split','pvp_escrow')",
        (user_id,),
    )
    total = int((await cur.fetchone())["v"])
    if total >= 1_000_000:
        await _award_and_notify(bot, user_id, "mega_better")
