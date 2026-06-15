"""新規UX機能の単体テスト: 統計集計、チャレンジpick・進捗算出。"""
import asyncio
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.dao import Database
from cogs.challenges import pick_today, _progress_for, CHALLENGES


async def test_stats():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_feat_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 100
    await db.ensure_user(uid)
    # スロット 3回賭けて 2回勝つ
    for _ in range(3):
        await db.adjust_balance(uid, -100, "slot_bet")
    await db.adjust_balance(uid, 250, "slot_win")
    await db.adjust_balance(uid, 250, "slot_win")
    # JP 1回
    await db.adjust_balance(uid, 5000, "slot_jackpot")
    # BJ 1回
    await db.adjust_balance(uid, -200, "blackjack_bet")
    # max_win_streak
    await db.set_win_streak(uid, 5)
    await db.set_win_streak(uid, 0)
    await db.set_win_streak(uid, 3)

    s = await db.user_stats(uid)
    assert s["jackpots_won"] == 1, s
    assert s["max_win_streak"] == 5, s
    assert s["win_streak"] == 3, s
    assert s["per_game"]["slot"]["plays"] == 3, s
    assert s["per_game"]["blackjack"]["plays"] == 1, s
    # 累計ベット = 100*3 + 200 = 500
    assert s["total_bet_volume"] == 500, s
    await db.close()
    print("stats OK")


def test_pick_deterministic():
    a = pick_today(123)
    b = pick_today(123)
    c = pick_today(124)
    assert [x.id for x in a] == [x.id for x in b], "同ユーザー同日は同じになる"
    assert [x.id for x in a] != [x.id for x in c] or a != c, "違うユーザーで違うはず(衝突は確率的にあるので緩く)"
    assert len(a) == 3 and len(set(x.id for x in a)) == 3, "重複なし3個"
    print("pick deterministic OK")


async def test_progress():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_chal_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 200
    await db.ensure_user(uid)
    # スロット 4回
    for _ in range(4):
        await db.adjust_balance(uid, -100, "slot_bet")
    slot_play_5 = next(c for c in CHALLENGES if c.id == "slot_play_5")
    bet_5k = next(c for c in CHALLENGES if c.id == "bet_volume_5k")
    p = await _progress_for(db, uid, slot_play_5)
    assert p == 4, p
    # 賭け合計 400 < 5000
    p2 = await _progress_for(db, uid, bet_5k)
    assert p2 == 400, p2
    # さらに賭けて 5000 ちょうど超え
    await db.adjust_balance(uid, -4600, "slot_bet", allow_negative=True)
    p3 = await _progress_for(db, uid, bet_5k)
    assert p3 == 5000, p3
    await db.close()
    print("progress OK")


if __name__ == "__main__":
    asyncio.run(test_stats())
    test_pick_deterministic()
    asyncio.run(test_progress())
    print("=== FEATURES TESTS PASSED ===")
