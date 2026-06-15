"""大会スコアリング、全体JP積立/当選、称号、メンテガードのテスト。"""
import asyncio
import os
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.global_jackpot import contribution, win_probability
from cogs.tournament import score_profit, score_jackpot, score_streak
from db.dao import Database


async def test_global_jp():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_gjp_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    class Bot:
        pass
    b = Bot(); b.db = db
    # 既定 0.5%
    assert contribution(b, 1000) == 5
    assert contribution(b, 100) == 0  # 0.5切り捨て
    # 100% rate
    await db.set_setting("global_jp_contrib", "1.0")
    assert contribution(b, 1000) == 1000
    # OFF
    await db.set_setting("global_jp_enabled", "0")
    assert contribution(b, 1000) == 0

    # win_probability
    await db.set_setting("global_jp_enabled", "1")
    await db.set_setting("global_jp_full_speed", "1000000")
    assert win_probability(b, 0) == 0.0
    assert win_probability(b, 500000) == 0.5
    assert win_probability(b, 1000000) == 1.0
    assert win_probability(b, 2000000) == 1.0   # 上限クランプ

    # 積立 → win
    await db.global_jp_add(5000)
    assert await db.global_jp_amount() == 5000
    won = await db.global_jp_win(999)
    assert won == 5000
    assert await db.global_jp_amount() == 0
    await db.close()
    print("global jp OK")


async def test_tournament_scoring():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_t_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid1, uid2 = 1001, 1002
    await db.ensure_user(uid1); await db.ensure_user(uid2)
    # uid1: bet -100, win +300 -> profit +200。連勝 = 1
    await db.adjust_balance(uid1, -100, "slot_bet")
    await db.adjust_balance(uid1, 300, "slot_win")
    # uid2: bet -50, win +120, bet -50, win +200。連勝 = 2、profit = 220
    await db.adjust_balance(uid2, -50, "slot_bet")
    await db.adjust_balance(uid2, 120, "slot_win")
    await db.adjust_balance(uid2, -50, "slot_bet")
    await db.adjust_balance(uid2, 200, "slot_win")
    # uid2: JP 1000 獲得
    await db.adjust_balance(uid2, 1000, "slot_jackpot")

    now = int(time.time())
    start = now - 3600
    end = now + 3600
    p = await score_profit(db, start, end)
    # uid2: 220+1000=1220 > uid1: 200
    assert p[0][0] == uid2 and p[0][1] == 1220, p
    j = await score_jackpot(db, start, end)
    assert j[0][0] == uid2 and j[0][1] == 1000
    s = await score_streak(db, start, end)
    by_uid = dict(s)
    # uid2 は win→win→jackpot で3連勝、uid1 は1勝
    assert by_uid[uid2] == 3, by_uid
    assert by_uid[uid1] == 1, by_uid
    await db.close()
    print("tournament scoring OK")


async def test_badges():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_bg_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 7777
    ok1 = await db.award_badge(uid, "first_jp")
    assert ok1
    ok2 = await db.award_badge(uid, "first_jp")  # 同じものは False
    assert not ok2
    assert await db.has_badge(uid, "first_jp")
    badges = await db.user_badges(uid)
    assert "first_jp" in badges
    await db.close()
    print("badges OK")


if __name__ == "__main__":
    asyncio.run(test_global_jp())
    asyncio.run(test_tournament_scoring())
    asyncio.run(test_badges())
    print("=== EVENT PACK TESTS PASSED ===")
