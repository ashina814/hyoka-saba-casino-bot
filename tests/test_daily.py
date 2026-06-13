"""デイリーのアトミック精算と、失敗時に状態が一切進まないことを検証する。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.dao import Database


async def main():
    tmp = os.path.join(tempfile.gettempdir(), "casino_daily.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = Database(tmp)
    await db.connect()

    uid = 42
    bal0 = await db.get_balance(uid)
    assert bal0 == 1000

    # 正常系: pay_daily で残高/last_daily/streakが一括更新される
    new_bal = await db.pay_daily(uid, 500, 1, "2026-06-10T22:00:00.000Z")
    assert new_bal == 1500, new_bal
    cur = await db.conn.execute(
        "SELECT balance, last_daily, daily_streak FROM users WHERE user_id=?", (uid,)
    )
    row = await cur.fetchone()
    assert row["balance"] == 1500
    assert row["last_daily"] == "2026-06-10T22:00:00.000Z"
    assert row["daily_streak"] == 1
    # tx_logs にも残ってる
    cur = await db.conn.execute(
        "SELECT COUNT(*) n FROM tx_logs WHERE user_id=? AND reason='daily'", (uid,)
    )
    assert (await cur.fetchone())["n"] == 1
    print("pay_daily 正常系 OK")

    # 失敗系シミュレーション: 不正な SQL を仕込んで rollback されることを確認
    # _log_tx をテンポラリに壊して再現
    orig_log = db._log_tx
    async def broken(*a, **kw):
        raise RuntimeError("simulated failure")
    db._log_tx = broken
    bal_before = await db.get_balance(uid)
    last_before = (await (await db.conn.execute(
        "SELECT last_daily FROM users WHERE user_id=?", (uid,)
    )).fetchone())["last_daily"]
    try:
        await db.pay_daily(uid, 999, 99, "9999-12-31T23:59:59.000Z")
        assert False, "should raise"
    except RuntimeError:
        pass
    db._log_tx = orig_log
    # 全部 rollback されているはず
    bal_after = await db.get_balance(uid)
    last_after = (await (await db.conn.execute(
        "SELECT last_daily FROM users WHERE user_id=?", (uid,)
    )).fetchone())["last_daily"]
    assert bal_before == bal_after, (bal_before, bal_after)
    assert last_before == last_after, (last_before, last_after)
    print("pay_daily 失敗時 rollback OK")

    await db.close()
    print("=== DAILY ATOMIC TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
