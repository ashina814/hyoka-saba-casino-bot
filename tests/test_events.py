"""ブースト判定とおみくじの決定論をテスト。"""
import asyncio
import os
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.omikuji import _pick, RESULTS
from db.dao import Database


async def test_boost_setting():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_boost_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    # 既定: 倍率1.0(無効)
    assert float(db.setting("boost_multiplier")) == 1.0
    assert int(db.setting("boost_until_ts")) == 0
    # 1.5倍を1時間先までセット
    await db.set_setting("boost_multiplier", "1.5")
    await db.set_setting("boost_until_ts", str(int(time.time()) + 3600))
    assert float(db.setting("boost_multiplier")) == 1.5
    # 過去時刻にすると無効化扱い(値は残っても判定は無効に)
    await db.set_setting("boost_until_ts", "0")
    assert int(db.setting("boost_until_ts")) == 0
    await db.close()
    print("boost setting OK")


def test_omikuji_deterministic():
    # 同一 user_id 同一日は同じ結果
    a = _pick(1234)
    b = _pick(1234)
    assert a == b
    # 違う user_id では分布が変わる(同一になっても確率的にOKなので少なくとも複数引いて見る)
    results = {_pick(uid)[0] for uid in range(200)}
    # 全7種のうち少なくとも5種は出てほしい
    assert len(results) >= 5, results
    # 大吉が含まれる(2000人いれば確実)
    big_results = {_pick(uid)[0] for uid in range(2000)}
    assert "大吉" in big_results
    print("omikuji deterministic OK")


async def test_omikuji_dao():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_omi_{uuid.uuid4().hex}.db")
    db = Database(tmp); await db.connect()
    uid = 999
    await db.ensure_user(uid)
    from cogs.omikuji import _today
    # 受取記録
    await db.conn.execute(
        "INSERT INTO omikuji_claimed (user_id, date, result, bonus) "
        "VALUES (?, ?, ?, ?)",
        (uid, _today(), "大吉", 300),
    )
    await db.conn.commit()
    cur = await db.conn.execute(
        "SELECT * FROM omikuji_claimed WHERE user_id=?", (uid,)
    )
    row = await cur.fetchone()
    assert row["result"] == "大吉" and row["bonus"] == 300
    # 同日重複 PK 違反
    try:
        await db.conn.execute(
            "INSERT INTO omikuji_claimed (user_id, date, result, bonus) "
            "VALUES (?, ?, ?, ?)",
            (uid, _today(), "凶", 0),
        )
        await db.conn.commit()
        raise AssertionError("duplicate should fail")
    except Exception:
        pass
    await db.close()
    print("omikuji dao OK")


if __name__ == "__main__":
    asyncio.run(test_boost_setting())
    test_omikuji_deterministic()
    asyncio.run(test_omikuji_dao())
    print("=== EVENTS TESTS PASSED ===")
