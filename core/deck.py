"""トランプ: カード表現・デッキ・シャッフル。

乱数は secrets.SystemRandom を使う。賭博系は予測不能性が信頼の土台なので、
擬似乱数(random)ではなく OS の暗号論的乱数を用いる。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

_RNG = secrets.SystemRandom()

SUITS = ["♠", "♥", "♦", "♣"]
# rank: 2..14 (11=J,12=Q,13=K,14=A)
RANK_LABEL = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A",
}


@dataclass(frozen=True)
class Card:
    rank: int  # 2..14
    suit: str  # ♠♥♦♣

    def __str__(self) -> str:
        return f"{self.suit}{RANK_LABEL[self.rank]}"


def full_deck() -> list[Card]:
    return [Card(r, s) for s in SUITS for r in range(2, 15)]


class Deck:
    """シャッフル済みデッキ。draw() で上から引く。"""

    def __init__(self) -> None:
        self.cards = full_deck()
        _RNG.shuffle(self.cards)

    def draw(self, n: int = 1) -> list[Card]:
        if n > len(self.cards):
            raise ValueError("デッキの残り枚数が足りません。")
        out, self.cards = self.cards[:n], self.cards[n:]
        return out

    def __len__(self) -> int:
        return len(self.cards)
