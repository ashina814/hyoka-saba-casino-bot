"""外部通貨(別Botが管理する第一次通貨)とのAPI連携層。

設計:
- ドライバ方式。.env の `EXCHANGE_AUTO_DRIVER` で実装を差し替え可能。
- 既定は `NoneDriver`(手動承認モード)。両替Cogから呼んでも `Result(ok=False, manual=True)`
  を返すだけで、現状の運用(承認ボタン)に何も影響しない。
- 外部APIに繋ぎ込む時は、`BaseDriver` を継承して `burn`/`mint` を実装し、
  下の `make_driver()` に1行追加するだけ。

burn = ユーザー → お釈迦さま方向の外部通貨減算(=焼却)
mint = お釈迦さま → ユーザー方向の外部通貨加算(=送付)

このBotから見ると:
- ゼニー→カジノ: 申請時に **burn** を呼んで成功したら、Bot内でカジノコイン発行
- カジノ→ゼニー: 申請時に内部エスクロー、その後 **mint** を呼んで成功したら完結
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Result:
    """API呼び出し結果。

    - ok=True: 成功。Bot側で対応する内部処理(発行・焼却記録)を進める。
    - ok=False, manual=True: ドライバ未設定(NoneDriver)。手動承認フローへフォールバック。
    - ok=False, manual=False: API実装あり、しかし失敗。エラーメッセージを `error` に。
    """
    ok: bool
    manual: bool = False
    error: str = ""
    # 外部側のトランザクションIDなど(監査用、任意)
    external_ref: str | None = None


class BaseDriver(Protocol):
    """外部通貨ドライバの最小契約。"""

    name: str   # ログ表示用 (例: "zeny", "coinX")
    # True なら自動完結モード、False なら手動承認フォールバック
    auto: bool

    async def burn(self, user_id: int, amount: int, ref: str) -> Result:
        ...

    async def mint(self, user_id: int, amount: int, ref: str) -> Result:
        ...

    async def close(self) -> None:
        """シャットダウン時の後始末(セッション解放など)。"""
        ...


# ───────────────────────── NoneDriver (既定) ─────────────────────────
class NoneDriver:
    """API未設定時のフォールバック。全呼び出しを「手動承認に回す」と返す。"""
    name = "none"
    auto = False

    async def burn(self, user_id: int, amount: int, ref: str) -> Result:
        return Result(ok=False, manual=True)

    async def mint(self, user_id: int, amount: int, ref: str) -> Result:
        return Result(ok=False, manual=True)

    async def close(self) -> None:
        return None


# ───────────────────────── ファクトリ ─────────────────────────
def make_driver(cfg) -> BaseDriver:
    """`cfg` の EXCHANGE_AUTO_DRIVER に従ってドライバインスタンスを作る。

    新しいドライバを足すときは:
    1. このファイル(または別ファイル)に BaseDriver 実装を追加
    2. ここに `elif name == 'xxx': return XxxDriver(...)` を追加
    """
    name = (cfg.exchange_auto_driver or "none").strip().lower()
    if name in ("", "none", "off", "disabled"):
        return NoneDriver()
    # API 実装が来たらここに分岐を追加
    # elif name == "zeny":
    #     from core.driver_zeny import ZenyDriver
    #     return ZenyDriver(cfg.exchange_auto_api_url, cfg.exchange_auto_api_key,
    #                       cfg.exchange_auto_timeout_sec)
    # 未知のドライバ名は安全側に倒して None で動かす(起動失敗回避)
    return NoneDriver()
