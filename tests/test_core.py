"""非Discordの範囲で中核ロジックとDBを検証するスモークテスト。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import deck, dice, hand
from core.hand import FOUR_KIND, FULL_HOUSE, STRAIGHT, STRAIGHT_FLUSH
from core.deck import Card


def test_hand():
    # ロイヤル
    royal = [Card(10, "♠"), Card(11, "♠"), Card(12, "♠"), Card(13, "♠"), Card(14, "♠")]
    assert hand.evaluate5(royal)[0] == STRAIGHT_FLUSH
    # ホイール A-2-3-4-5
    wheel = [Card(14, "♠"), Card(2, "♥"), Card(3, "♦"), Card(4, "♣"), Card(5, "♠")]
    assert hand.evaluate5(wheel)[0] == STRAIGHT
    assert hand.evaluate5(wheel)[1] == 5
    # フルハウス > フラッシュ
    fh = [Card(9, "♠"), Card(9, "♥"), Card(9, "♦"), Card(4, "♣"), Card(4, "♠")]
    fl = [Card(2, "♠"), Card(5, "♠"), Card(8, "♠"), Card(11, "♠"), Card(13, "♠")]
    assert hand.evaluate5(fh) > hand.evaluate5(fl)
    assert hand.evaluate5(fh)[0] == FULL_HOUSE
    # フォーカード
    quads = [Card(7, "♠"), Card(7, "♥"), Card(7, "♦"), Card(7, "♣"), Card(2, "♠")]
    assert hand.evaluate5(quads)[0] == FOUR_KIND
    # 7枚ベスト
    seven = [Card(14, "♠"), Card(14, "♥"), Card(14, "♦"), Card(5, "♣"),
             Card(5, "♠"), Card(2, "♦"), Card(9, "♣")]
    assert hand.best_hand(seven)[0] == FULL_HOUSE
    print("hand OK")


def test_dice():
    assert dice.evaluate_chinchiro([1, 1, 1]).rank.name == "PINZORO"
    assert dice.evaluate_chinchiro([4, 5, 6]).rank.name == "SHIGORO"
    assert dice.evaluate_chinchiro([1, 2, 3]).rank.name == "HIFUMI"
    assert dice.evaluate_chinchiro([3, 3, 5]).eye == 5
    a = dice.evaluate_chinchiro([3, 3, 5])  # 5の目
    b = dice.evaluate_chinchiro([4, 4, 2])  # 2の目
    assert dice.chinchiro_compare(a, b) == 1
    print("dice OK")


def test_deck():
    d = deck.Deck()
    assert len(d) == 52
    drawn = d.draw(5)
    assert len(drawn) == 5 and len(d) == 47
    # 重複なし
    full = deck.full_deck()
    assert len(set((c.rank, c.suit) for c in full)) == 52
    print("deck OK")


async def test_db():
    from config import Config
    from db.dao import Database, InsufficientFunds
    tmp = os.path.join(tempfile.gettempdir(), "casino_smoke.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = Database(tmp)
    await db.connect()
    # 初期残高付与
    bal = await db.get_balance(111)
    assert bal == 1000, bal
    # ベット引き落とし
    await db.adjust_balance(111, -300, "slot_bet")
    assert await db.get_balance(111) == 700
    # 残高不足
    try:
        await db.adjust_balance(111, -99999, "slot_bet")
        assert False, "should raise"
    except InsufficientFunds:
        pass
    # 設定読み書き
    assert db.setting("slot_house_edge") == 0.05
    await db.set_setting("slot_house_edge", "0.08")
    assert db.setting("slot_house_edge") == 0.08
    # 不正値は弾く
    try:
        await db.set_setting("slot_house_edge", "abc")
        assert False
    except ValueError:
        pass
    # jackpot
    await db.jackpot_add(500)
    amt = await db.jackpot_amount()
    assert amt == 10500, amt
    # stats
    s = await db.economy_stats()
    assert s["user_count"] >= 1
    await db.close()
    print("db OK")


def test_economy():
    import asyncio as _a
    from db.dao import Database
    async def run():
        tmp = os.path.join(tempfile.gettempdir(), "casino_smoke2.db")
        if os.path.exists(tmp):
            os.remove(tmp)
        db = Database(tmp)
        await db.connect()
        from core import economy
        # 初回デイリー
        amt, streak, msg = economy.compute_daily(db, 0, 0, None)
        assert amt > 0 and streak == 1, (amt, streak)
        # 金持ちは減衰
        amt_rich, _, _ = economy.compute_daily(db, 1_000_000, 0, None)
        assert amt_rich < amt, (amt_rich, amt)
        # レーキ
        assert economy.rake(db, 1000) == 30  # 3%
        await db.close()
    _a.run(run())
    print("economy OK")


if __name__ == "__main__":
    test_deck()
    test_dice()
    test_hand()
    test_economy()
    asyncio.run(test_db())
    print("=== ALL SMOKE TESTS PASSED ===")
