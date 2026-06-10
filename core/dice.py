"""サイコロ系: チンチロの役判定と、丁半の偶奇判定。

乱数は deck と同じく secrets.SystemRandom。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import IntEnum

_RNG = secrets.SystemRandom()

DIE_FACE = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}


def roll(n: int = 3) -> list[int]:
    return [_RNG.randint(1, 6) for _ in range(n)]


def faces(values: list[int]) -> str:
    return " ".join(DIE_FACE[v] for v in values)


# ───────────────────────── チンチロ ─────────────────────────
class ChinchiroRank(IntEnum):
    """役の強さ。数値が大きいほど強い。HORDER(目なし)が最弱。"""
    SHONBEN = -2      # ションベン(場外/無効) ※本実装では使わないが予約
    HIFUMI = -1       # ヒフミ(1-2-3) 最悪、倍払い
    NO_ROLE = 0       # 役なし(同じ目が無い)→振り直し相当だが3回で確定時は目なし
    EYE = 1           # 目(2つ同じ＋1つの数字が出目)→ 出目の大きさで競う
    SHIGORO = 2       # シゴロ(4-5-6)
    TRIPLE = 3        # ゾロ目(アラシ)
    PINZORO = 4       # ピンゾロ(1-1-1) 最強


@dataclass(frozen=True)
class ChinchiroResult:
    values: list[int]
    rank: ChinchiroRank
    eye: int           # 「目」のときの出目(1..6)。役で competing に使う。それ以外は0
    payout_mult: int   # 基本配当倍率(親/子の精算に使う符号なし倍率)
    label: str

    def faces(self) -> str:
        return faces(self.values)


def evaluate_chinchiro(values: list[int]) -> ChinchiroResult:
    """3つのサイコロの目から役を判定する(1回振りの確定結果)。

    配当倍率(payout_mult)の意味:
      ピンゾロ=5倍, ゾロ目=3倍, シゴロ=2倍, 目=1倍, 役なし=0(引分相当),
      ヒフミ=-2(2倍払い)。符号は親子精算側で扱う。
    """
    vs = sorted(values)
    s = set(vs)

    # ピンゾロ
    if vs == [1, 1, 1]:
        return ChinchiroResult(values, ChinchiroRank.PINZORO, 6, 5, "ピンゾロ(1-1-1)")
    # ゾロ目(アラシ)
    if len(s) == 1:
        return ChinchiroResult(
            values, ChinchiroRank.TRIPLE, vs[0], 3, f"ゾロ目({vs[0]}のアラシ)"
        )
    # シゴロ
    if vs == [4, 5, 6]:
        return ChinchiroResult(values, ChinchiroRank.SHIGORO, 0, 2, "シゴロ(4-5-6)")
    # ヒフミ
    if vs == [1, 2, 3]:
        return ChinchiroResult(values, ChinchiroRank.HIFUMI, 0, -2, "ヒフミ(1-2-3)")
    # 目(2つ同じ＋1つ違い)→ 違う1つが出目
    if len(s) == 2:
        for v in s:
            if vs.count(v) == 1:
                return ChinchiroResult(
                    values, ChinchiroRank.EYE, v, 1, f"{v}の目"
                )
    # 役なし
    return ChinchiroResult(values, ChinchiroRank.NO_ROLE, 0, 0, "目なし")


def chinchiro_compare(a: ChinchiroResult, b: ChinchiroResult) -> int:
    """a が b に勝てば 1、負けば -1、引分 0。

    まず役の強さ、同じ「目」役なら出目の大小で比較する。
    """
    if a.rank != b.rank:
        return 1 if a.rank > b.rank else -1
    if a.rank == ChinchiroRank.EYE:
        if a.eye != b.eye:
            return 1 if a.eye > b.eye else -1
    if a.rank == ChinchiroRank.TRIPLE:
        if a.eye != b.eye:
            return 1 if a.eye > b.eye else -1
    return 0


# ───────────────────────── 丁半 ─────────────────────────
def chohan_roll() -> tuple[list[int], bool]:
    """サイコロ2個。合計が偶数なら丁(True)、奇数なら半(False)。"""
    vals = roll(2)
    is_cho = (sum(vals) % 2 == 0)
    return vals, is_cho
