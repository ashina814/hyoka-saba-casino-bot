"""自己制限/称号進捗/殿堂集計のテスト。"""
import asyncio
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.badges import progress_for, _bar
from db.dao import Database


async def test_self_limit():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_q1_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 555
    await db.ensure_user(uid)
    # 既定: 上限0(未設定)
    lim = await db.get_user_limit(uid)
    assert lim["daily_bet_cap"] == 0
    # 設定
    await db.set_user_limit(uid, 5000)
    lim = await db.get_user_limit(uid)
    assert lim["daily_bet_cap"] == 5000 and lim["set_at"]
    # 日次累計の計測
    await db.adjust_balance(uid, -1000, "slot_bet", allow_negative=True)
    await db.adjust_balance(uid, -500, "blackjack_bet", allow_negative=True)
    total = await db.daily_bet_total(uid)
    assert total == 1500, total
    await db.close()
    print("self limit OK")


async def test_badge_progress():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_q2_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 666
    await db.ensure_user(uid)
    # first_jp: 0/1
    p = await progress_for(db, uid, "first_jp")
    assert p == (0, 1)
    # JP獲得して 1/1
    await db.adjust_balance(uid, 5000, "slot_jackpot")
    p = await progress_for(db, uid, "first_jp")
    assert p == (1, 1)
    # mega_better
    await db.adjust_balance(uid, -300000, "slot_bet", allow_negative=True)
    p = await progress_for(db, uid, "mega_better")
    assert p[0] == 300000 and p[1] == 1_000_000
    # streak
    await db.set_win_streak(uid, 25)
    p = await progress_for(db, uid, "streak_50")
    assert p == (25, 50)
    # 評価不能タイプ
    assert await progress_for(db, uid, "bj_natural") is None
    await db.close()
    print("badge progress OK")


def test_bar():
    assert _bar(0, 10, 10) == "░" * 10
    assert _bar(10, 10, 10) == "█" * 10
    assert _bar(5, 10, 10) == "█" * 5 + "░" * 5
    assert _bar(20, 10, 10) == "█" * 10  # 上振れクランプ
    print("bar OK")


if __name__ == "__main__":
    asyncio.run(test_self_limit())
    asyncio.run(test_badge_progress())
    test_bar()
    print("=== QUALITY TESTS PASSED ===")
