"""経済まわりの計算ヘルパー(純粋関数寄り)。

settings の値は Database から読む。インフレ対策(デイリー減衰・レーキ・保有税)の
式をここに集約し、ゲーム側からは結果だけ使う。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from db.dao import Database

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in (_ISO, "%Y-%m-%dT%H:%M:%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def compute_daily(
    db: Database, balance: int, streak: int, last_daily: str | None
) -> tuple[int, int, str]:
    """デイリー受給額・更新後ストリーク・説明文を返す。受給不可なら額0。

    インフレ対策:
    - 残高が多いほど基本額が減衰(daily_decay)。pivot 残高で半減。
    - 連続ログインで加算(上限あり)。これは射幸性(継続)演出も兼ねる。
    """
    now = now_utc()
    last = _parse_ts(last_daily)

    # 24時間経過チェック
    if last is not None and (now - last).total_seconds() < 24 * 3600:
        remain = 24 * 3600 - (now - last).total_seconds()
        h, m = int(remain // 3600), int((remain % 3600) // 60)
        return 0, streak, f"次のデイリーまであと {h}時間{m}分"

    # ストリーク: 48時間以内なら継続、超えたらリセット
    if last is not None and (now - last).total_seconds() <= 48 * 3600:
        new_streak = streak + 1
    else:
        new_streak = 1

    base = int(db.setting("daily_base", 1000))

    # 残高減衰: amount = base * pivot / (pivot + balance) で滑らかに半減
    if db.setting("daily_decay_enabled", True):
        pivot = max(1, int(db.setting("daily_decay_pivot", 20000)))
        base = int(base * pivot / (pivot + max(0, balance)))

    # ストリークボーナス(上限日数まで)
    cap = int(db.setting("daily_streak_cap", 7))
    per = int(db.setting("daily_streak_bonus", 100))
    bonus_days = min(new_streak, cap)
    bonus = per * bonus_days

    amount = max(0, base + bonus)
    msg = f"基本 {base} + 連続{new_streak}日ボーナス {bonus}"
    return amount, new_streak, msg


def rake(db: Database, amount: int) -> int:
    """PVP の勝ち分から徴収する手数料(シンク)。切り捨て。"""
    rate = float(db.setting("pvp_rake", 0.03))
    return int(math.floor(amount * rate))


def jackpot_contribution(db: Database, bet: int) -> int:
    """スロットのベットからジャックポットに積む額(再分配=インフレ中立)。"""
    if not db.setting("jackpot_enabled", True):
        return 0
    rate = float(db.setting("jackpot_contrib", 0.01))
    return int(math.floor(bet * rate))


def gini(balances: list[int]) -> float:
    """残高分布の Gini 係数(0=平等, 1=独占)を返す。

    定義: G = (Σ_i (2i - n - 1) * b_i) / (n * Σ b_i)  (b は昇順ソート、i は1始まり)
    総資産が0 or 人数1人以下なら 0.0(計算不能=平等とみなす)。
    """
    arr = sorted(b for b in balances if b > 0)
    n = len(arr)
    if n < 2:
        return 0.0
    total = sum(arr)
    if total <= 0:
        return 0.0
    cumulative = 0
    for i, b in enumerate(arr, start=1):
        cumulative += (2 * i - n - 1) * b
    return cumulative / (n * total)


def classify_gini(g: float) -> tuple[str, str]:
    """Gini 値 → (アイコン, 判定文)。"""
    if g >= 0.85:
        return ("🔴", "格差が極端。少数のクジラに資産が集中している")
    if g >= 0.75:
        return ("🟡", "格差が大きい。デイリー減衰や保有税の強化を検討")
    if g >= 0.55:
        return ("🟢", "やや偏りはあるが許容範囲")
    return ("🟢", "比較的平等")


def classify_inflation(monthly_net: int, total_supply: int) -> tuple[str, str, float]:
    """月次の純発行量から (アイコン, 判定文, インフレ率%) を返す。"""
    if total_supply <= 0:
        return ("🟢", "判定不能(供給ゼロ)", 0.0)
    rate = monthly_net / total_supply * 100.0
    if rate >= 15:
        return ("🔴", "急速にインフレ進行中。ハウスエッジ/レーキ上げ、daily 減衰強化を検討", rate)
    if rate >= 5:
        return ("🟡", "ややインフレ気味。今後も増え続けるなら対策を", rate)
    if rate <= -10:
        return ("🟡", "デフレ気味。チップが消えすぎ、プレイヤー離れに注意", rate)
    return ("🟢", "供給は概ね安定", rate)


def classify_activity(active: int, total: int) -> tuple[str, str]:
    """アクティブ率(直近Nプレイ済み / 総ユーザー)から判定。"""
    if total <= 0:
        return ("🟢", "ユーザーなし")
    ratio = active / total
    if ratio >= 0.5:
        return ("🟢", "活発に利用されている")
    if ratio >= 0.2:
        return ("🟡", "プレイ頻度は中程度")
    return ("🔴", "アクティブが少ない。イベントや報酬強化の検討余地")


def holding_tax(db: Database, balance: int) -> int:
    """保有税(日次)。閾値超過分にのみ課税。OFF または閾値以下なら0。"""
    if not db.setting("holding_tax_enabled", False):
        return 0
    threshold = int(db.setting("holding_tax_threshold", 100000))
    if balance <= threshold:
        return 0
    rate = float(db.setting("holding_tax_rate", 0.01))
    return int(math.floor((balance - threshold) * rate))
