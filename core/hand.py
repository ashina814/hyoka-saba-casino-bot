"""ポーカー役判定。5カードドローとテキサスホールデム双方で使う。

evaluate() は (カテゴリ, タイブレーク...) のタプルを返す。タプル同士は
そのまま大小比較でき、大きいほど強い手。7枚からは最良5枚を選ぶ。
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations

from .deck import RANK_LABEL, Card

# カテゴリ(大きいほど強い)
HIGH_CARD = 1
ONE_PAIR = 2
TWO_PAIR = 3
THREE_KIND = 4
STRAIGHT = 5
FLUSH = 6
FULL_HOUSE = 7
FOUR_KIND = 8
STRAIGHT_FLUSH = 9

CATEGORY_NAME = {
    HIGH_CARD: "ハイカード",
    ONE_PAIR: "ワンペア",
    TWO_PAIR: "ツーペア",
    THREE_KIND: "スリーカード",
    STRAIGHT: "ストレート",
    FLUSH: "フラッシュ",
    FULL_HOUSE: "フルハウス",
    FOUR_KIND: "フォーカード",
    STRAIGHT_FLUSH: "ストレートフラッシュ",
}


def _straight_high(ranks: list[int]) -> int | None:
    """ユニークな降順 rank 集合からストレートの最高位 rank を返す。無ければ None。

    A-2-3-4-5(ホイール)は最高位を 5 とする。
    """
    u = sorted(set(ranks), reverse=True)
    # A(14) を 1 としても見るためにホイール用の 1 を足す
    if 14 in u:
        u = u + [1]
    run = 1
    for i in range(1, len(u)):
        if u[i] == u[i - 1] - 1:
            run += 1
            if run >= 5:
                return u[i] + 4
        else:
            run = 1
    return None


def evaluate5(cards: list[Card]) -> tuple:
    """ちょうど5枚を評価して比較用タプルを返す。"""
    ranks = sorted((c.rank for c in cards), reverse=True)
    suits = [c.suit for c in cards]
    rank_counts = Counter(ranks)
    is_flush = len(set(suits)) == 1
    straight_high = _straight_high(ranks)

    # (回数, rank) を回数優先・rank 優先で並べる → タイブレークに使う
    by_count = sorted(rank_counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ordered_ranks = [r for r, _ in by_count]
    counts = [c for _, c in by_count]

    if straight_high and is_flush:
        return (STRAIGHT_FLUSH, straight_high)
    if counts[0] == 4:
        return (FOUR_KIND, ordered_ranks[0], ordered_ranks[1])
    if counts[0] == 3 and counts[1] >= 2:
        return (FULL_HOUSE, ordered_ranks[0], ordered_ranks[1])
    if is_flush:
        return (FLUSH, *ranks)
    if straight_high:
        return (STRAIGHT, straight_high)
    if counts[0] == 3:
        return (THREE_KIND, ordered_ranks[0], *ordered_ranks[1:])
    if counts[0] == 2 and counts[1] == 2:
        return (TWO_PAIR, ordered_ranks[0], ordered_ranks[1], ordered_ranks[2])
    if counts[0] == 2:
        return (ONE_PAIR, ordered_ranks[0], *ordered_ranks[1:])
    return (HIGH_CARD, *ranks)


def best_hand(cards: list[Card]) -> tuple:
    """5枚以上(ホールデムなら7枚)から最良の5枚評価を返す。"""
    if len(cards) == 5:
        return evaluate5(cards)
    return max(evaluate5(list(combo)) for combo in combinations(cards, 5))


def category_name(score: tuple) -> str:
    return CATEGORY_NAME.get(score[0], "?")


def describe(score: tuple) -> str:
    """役名＋主要ランクの短い説明。"""
    name = category_name(score)
    if len(score) > 1:
        return f"{name}({RANK_LABEL.get(score[1], score[1])} ハイ)"
    return name
