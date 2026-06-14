"""ハイロー倍率計算と、ブラックジャックのハンド評価を Discord 非依存で検証。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.hilo import _hilo_multiplier
from cogs.blackjack import hand_value, is_blackjack
from core.deck import Card, card_emoji, hand_emoji


def test_hilo_mult():
    # 基準=7 のとき High: 8..14 = 7ランク × 4枚 = 28枚 / 51 ≒ 確率 0.549
    # 倍率 = 51/28 * 0.95 ≒ 1.730
    m = _hilo_multiplier(7, "high", 0.05)
    assert 1.7 < m < 1.75, m
    # 基準=2 のとき Low: 該当なし → 倍率 0
    assert _hilo_multiplier(2, "low", 0.05) == 0.0
    # 基準=14(A) のとき High: 該当なし → 倍率 0
    assert _hilo_multiplier(14, "high", 0.05) == 0.0
    # house_edge=0 で純粋な逆数倍率になる
    m = _hilo_multiplier(7, "high", 0.0)
    assert abs(m - (51 / 28)) < 1e-9
    print("hilo mult OK")


def test_bj_hand_value():
    # A + K = 21 (BJ, ソフト判定は値が21でAあり)
    cards = [Card(14, "♠"), Card(13, "♥")]
    v, soft = hand_value(cards)
    assert v == 21 and soft, (v, soft)
    assert is_blackjack(cards)
    # 9 + 7 = 16, soft=False
    cards = [Card(9, "♠"), Card(7, "♥")]
    v, soft = hand_value(cards)
    assert v == 16 and not soft
    # A + 6 + 10 = 17 (A=1 にダウングレード, soft=False)
    cards = [Card(14, "♠"), Card(6, "♥"), Card(10, "♦")]
    v, soft = hand_value(cards)
    assert v == 17 and not soft, (v, soft)
    # A + 6 = 17 ソフト
    cards = [Card(14, "♠"), Card(6, "♥")]
    v, soft = hand_value(cards)
    assert v == 17 and soft
    # K + 10 = 20、BJ ではない(2枚で21じゃないため)
    cards = [Card(13, "♠"), Card(10, "♥")]
    assert not is_blackjack(cards)
    # A + 5 + 5 + Q = 21 (1+5+5+10、ダウングレード)
    cards = [Card(14, "♠"), Card(5, "♥"), Card(5, "♦"), Card(12, "♣")]
    v, _ = hand_value(cards)
    assert v == 21
    # 5 + 9 + 8 = 22 (BUST)
    cards = [Card(5, "♠"), Card(9, "♥"), Card(8, "♦")]
    v, _ = hand_value(cards)
    assert v == 22
    print("bj hand_value OK")


def test_card_emoji():
    # 全カードで例外が出ず1文字を返すこと
    for s in "♠♥♦♣":
        for r in range(2, 15):
            em = card_emoji(Card(r, s))
            assert isinstance(em, str) and len(em) >= 1
    # 並べた表示でスペース区切り
    h = hand_emoji([Card(14, "♠"), Card(13, "♥")])
    assert " " in h
    print("card emoji OK")


if __name__ == "__main__":
    test_hilo_mult()
    test_bj_hand_value()
    test_card_emoji()
    print("=== GAMES TESTS PASSED ===")
