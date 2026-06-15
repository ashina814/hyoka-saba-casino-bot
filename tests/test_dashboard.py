"""経済ダッシュボードの集計とGini/インフレ判定をテスト。"""
import asyncio
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.economy import gini, classify_gini, classify_inflation, classify_activity
from db.dao import Database


def test_gini():
    # 完全平等
    assert gini([100, 100, 100, 100]) == 0.0
    # ほぼ独占(1人が大金、他は0)
    g = gini([1000000, 1, 1, 1, 1])
    assert g > 0.7, g
    # 中程度
    g2 = gini([100, 200, 300, 400, 500])
    assert 0.2 < g2 < 0.35, g2
    # 全員0
    assert gini([0, 0, 0]) == 0.0
    # 1人
    assert gini([1000]) == 0.0
    print("gini OK")


def test_classifiers():
    icon, _ = classify_gini(0.9)
    assert icon == "🔴"
    icon, _ = classify_gini(0.8)
    assert icon == "🟡"
    icon, _ = classify_gini(0.6)
    assert icon == "🟢"
    icon, _, rate = classify_inflation(20000, 100000)  # 20%/月
    assert icon == "🔴" and rate == 20.0
    icon, _, rate = classify_inflation(7000, 100000)   # 7%/月
    assert icon == "🟡"
    icon, _, rate = classify_inflation(1000, 100000)
    assert icon == "🟢"
    icon, _ = classify_activity(30, 100)   # 30% → 🟡 (>=20% かつ <50%)
    assert icon == "🟡"
    icon, _ = classify_activity(60, 100)   # 60% → 🟢
    assert icon == "🟢"
    icon, _ = classify_activity(5, 100)    # 5% → 🔴
    assert icon == "🔴"
    print("classifiers OK")


async def test_dashboard_dao():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_dash_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    # 数人分の残高を作る
    for uid, bal in [(1, 1000), (2, 5000), (3, 200), (4, 10), (5, 30000)]:
        await db.ensure_user(uid)
        # ensure_user で initial_grant=1000 が入ってるので、合わせて
        diff = bal - 1000
        if diff != 0:
            await db.adjust_balance(uid, diff, "admin_set", allow_negative=True)
    # 何かトランザクション流す
    await db.adjust_balance(1, -100, "slot_bet")
    await db.adjust_balance(1, 50, "slot_win")
    await db.adjust_balance(2, -500, "blackjack_bet")

    m = await db.economy_dashboard()
    assert m["user_count"] >= 5, m
    assert m["total_supply"] > 0
    assert 0.0 <= m["gini"] <= 1.0
    assert m["bet_volume_24h"] >= 600, m  # slot100 + bj500
    assert "period_1d" in m and "period_7d" in m and "period_30d" in m
    print("dashboard metrics OK")

    # スナップショット
    await db.write_snapshot_today()
    snaps = await db.recent_snapshots(5)
    assert len(snaps) >= 1
    assert snaps[0]["user_count"] == m["user_count"]
    # 同日2回呼ぶと UPSERT で1行のまま
    await db.write_snapshot_today()
    snaps2 = await db.recent_snapshots(5)
    assert len(snaps2) == len(snaps)
    print("snapshot OK")

    # owner_id 除外
    await db.set_setting("owner_id", "5")  # 一番金持ちを owner にして除外
    m2 = await db.economy_dashboard()
    assert m2["user_count"] < m["user_count"], (m2, m)
    assert m2["total_supply"] < m["total_supply"]
    print("owner exclusion OK")

    await db.close()


if __name__ == "__main__":
    test_gini()
    test_classifiers()
    asyncio.run(test_dashboard_dao())
    print("=== DASHBOARD TESTS PASSED ===")
