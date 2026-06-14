"""両替まわりのロジック(計算式・DAO・日次上限・期限切れ)を Discord 非依存で検証。"""
import asyncio
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.exchange import _calc, DIR_Z2C, DIR_C2Z
from db.dao import Database


def test_calc():
    # 10% 控除、受け取り側から減る
    receive, fee = _calc(1000, 0.10)
    assert receive == 900 and fee == 100, (receive, fee)
    # 端数は切り捨て
    receive, fee = _calc(999, 0.10)
    assert receive == 899 and fee == 100, (receive, fee)
    # 手数料 0 ならそのまま
    receive, fee = _calc(1000, 0.0)
    assert receive == 1000 and fee == 0
    print("calc OK")


async def test_dao():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_ex_{uuid.uuid4().hex}.db")
    db = Database(tmp)
    await db.connect()

    uid = 1001
    await db.ensure_user(uid)

    # 申請作成
    req_id = await db.create_exchange_request(uid, DIR_Z2C, 1000, 900, 100)
    assert req_id > 0
    row = await db.get_exchange_request(req_id)
    assert row["status"] == "pending"
    assert row["receive_amount"] == 900

    # 日次受領(pending も含む)
    got = await db.daily_exchange_received(uid, DIR_Z2C)
    assert got == 900, got
    # 違う方向はカウント別
    other = await db.daily_exchange_received(uid, DIR_C2Z)
    assert other == 0

    # 状態変更
    await db.set_exchange_status(req_id, "approved", 9999)
    row = await db.get_exchange_request(req_id)
    assert row["status"] == "approved"
    assert row["approver_id"] == 9999

    # rejected も上限カウントに **含めない** ことを確認するため、新規申請作って rejected に
    req2 = await db.create_exchange_request(uid, DIR_Z2C, 2000, 1800, 200)
    await db.set_exchange_status(req2, "rejected", 9999)
    got = await db.daily_exchange_received(uid, DIR_Z2C)
    # approved=900 + rejected=対象外 = 900 のまま
    assert got == 900, got

    # owner_id を統計から除外
    await db.set_setting("owner_id", str(uid))
    await db.adjust_balance(uid, 1_000_000_000, "exchange_burn")
    # 別ユーザーを足す
    await db.ensure_user(2002)
    stats = await db.economy_stats()
    # owner=巨額 のはずだが total_supply からは除外され、normal user の残高だけ
    assert stats["total_supply"] < 1_000_000_000, stats
    # 上位にも owner は出ない
    for r in stats["richest"]:
        assert r["user_id"] != uid

    await db.close()
    print("dao OK")


async def test_expire():
    tmp = os.path.join(tempfile.gettempdir(), f"casino_ex2_{uuid.uuid4().hex}.db")
    db = Database(tmp)
    await db.connect()
    uid = 3003
    await db.ensure_user(uid)

    await db.set_setting("exchange_request_ttl_hours", "48")
    rid = await db.create_exchange_request(uid, DIR_C2Z, 500, 450, 50)
    # 申請を 49時間前のものに書き換える(ttl=48時間より古い)
    await db.conn.execute(
        "UPDATE exchange_requests SET created_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-49 hours') WHERE id=?",
        (rid,),
    )
    await db.conn.commit()
    rows = await db.expired_pending_requests()
    assert any(r["id"] == rid for r in rows), [dict(r) for r in rows]

    # ttl を 100時間にすれば対象から外れる(49時間前 > 100時間前)
    await db.set_setting("exchange_request_ttl_hours", "100")
    rows = await db.expired_pending_requests()
    assert not any(r["id"] == rid for r in rows)
    await db.close()
    print("expire OK")


if __name__ == "__main__":
    test_calc()
    asyncio.run(test_dao())
    asyncio.run(test_expire())
    print("=== EXCHANGE TESTS PASSED ===")
