"""ホールデムのサイドポット分配とチップ保存を検証(Discord非依存)。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.holdem import HoldemCog, HoldemState
from core.deck import Card


class _DBStub:
    def setting(self, key, default=None):
        return {"pvp_rake": 0.03}.get(key, default)


class _BotStub:
    def __init__(self):
        self.db = _DBStub()
        self.cfg = None

    def get_user(self, uid):
        return None


def test_sidepot():
    cog = HoldemCog(_BotStub())
    st = HoldemState("m", 1, 100)
    st.players = [1, 2, 3]   # A=1, B=2, C=3
    st.committed = {1: 100, 2: 100, 3: 40}   # C はショートオールイン
    st.board = [Card(2, "♣"), Card(7, "♦"), Card(9, "♠"), Card(11, "♥"), Card(13, "♣")]
    st.hole = {
        1: [Card(11, "♦"), Card(11, "♠")],  # A: トリップ J
        2: [Card(3, "♣"), Card(4, "♦")],    # B: ハイカード
        3: [Card(13, "♦"), Card(13, "♠")],  # C: トリップ K(最強)
    }
    st.folded = set()
    payouts = cog._distribute(st)

    # メインポット(40*3=120, rake3, net117) → C最強
    # サイドポット((100-40)*2=120, rake3, net117) → A,B のうち A
    assert payouts[3] == 117, payouts
    assert payouts[1] == 117, payouts
    assert payouts[2] == 0, payouts

    # 保存則: 拠出240, 払い戻し234, シンク(rake)=6
    total_in = sum(st.committed.values())
    total_out = sum(payouts.values())
    assert total_in - total_out == 6, (total_in, total_out)
    print("holdem sidepot OK")


def test_round_helpers():
    cog = HoldemCog(_BotStub())
    st = HoldemState("m", 1, 100)
    st.players = [1, 2, 3]
    st.stacks = {1: 100, 2: 100, 3: 100}
    st.street_bet = {1: 0, 2: 0, 3: 0}
    st.committed = {1: 0, 2: 0, 3: 0}
    # ブラインド
    cog._post_blind(st, 2, 5)    # SB
    cog._post_blind(st, 3, 10)   # BB
    st.current_bet = 10
    assert st.street_bet[2] == 5 and st.street_bet[3] == 10
    # 次に行動できる人(1から時計回り)
    assert cog._next_can_act(st, 1, include_start=True) == 1
    # まだ全員行動していない→未完了
    assert not cog._round_complete(st)
    # 全員コール&acted
    st.street_bet = {1: 10, 2: 10, 3: 10}
    st.acted = {1, 2, 3}
    assert cog._round_complete(st)
    print("holdem round-helpers OK")


def test_board_deal():
    cog = HoldemCog(_BotStub())
    st = HoldemState("m", 1, 100)
    st.street_idx = 1
    cog._deal_board(st)
    assert len(st.board) == 3   # flop
    st.street_idx = 2
    cog._deal_board(st)
    assert len(st.board) == 4   # turn
    st.street_idx = 3
    cog._deal_board(st)
    assert len(st.board) == 5   # river
    print("holdem board-deal OK")


if __name__ == "__main__":
    test_sidepot()
    test_round_helpers()
    test_board_deal()
    print("=== HOLDEM ENGINE TESTS PASSED ===")
