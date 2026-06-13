"""DB アクセス層。

設計上の要点:
- 接続は1本(aiosqlite)。aiosqlite は内部キューで直列化されるため、
  単一プロセス内ではこれで十分。残高の読み書きは1トランザクションに収める。
- 「読んで→判定して→書く」(例: 残高チェックしてベット)を跨いだ
  二重消費を防ぐため、user_id 単位の asyncio.Lock を別途提供する。
- settings はメモリにキャッシュし、set 時に更新する(高頻度の読みを軽く)。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class InsufficientFunds(Exception):
    """残高不足。ベット等で所持を超える引き落としをしようとした。"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._settings: dict[str, Any] = {}

    # ───────────────────────── ライフサイクル ─────────────────────────
    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._conn.commit()
        await self._reload_settings()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() が呼ばれていません。")
        return self._conn

    # ───────────────────────── ロック ─────────────────────────
    def user_lock(self, user_id: int) -> asyncio.Lock:
        """`async with db.user_lock(uid):` で、そのユーザーの残高操作を直列化する。"""
        return self._user_locks[user_id]

    # ───────────────────────── settings ─────────────────────────
    @staticmethod
    def _cast(value: str, vtype: str) -> Any:
        if vtype == "int":
            return int(value)
        if vtype == "float":
            return float(value)
        if vtype == "bool":
            return value not in ("0", "false", "False", "", "off")
        return value

    async def _reload_settings(self) -> None:
        cur = await self.conn.execute("SELECT key, value, vtype FROM settings")
        rows = await cur.fetchall()
        self._settings = {r["key"]: self._cast(r["value"], r["vtype"]) for r in rows}

    def setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def all_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    async def settings_meta(self) -> list[aiosqlite.Row]:
        """管理パネル表示用に key/value/vtype/label を全件返す。"""
        cur = await self.conn.execute(
            "SELECT key, value, vtype, label FROM settings ORDER BY key"
        )
        return list(await cur.fetchall())

    async def set_setting(self, key: str, raw_value: str) -> Any:
        """文字列で受け取り、登録済み vtype でバリデートして保存。新しい値を返す。"""
        cur = await self.conn.execute(
            "SELECT vtype FROM settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        if row is None:
            raise KeyError(f"未知の設定キー: {key}")
        vtype = row["vtype"]
        # キャストできるか検証(例外はそのまま呼び出し側へ)
        casted = self._cast(raw_value, vtype)
        await self.conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (raw_value, key)
        )
        await self.conn.commit()
        self._settings[key] = casted
        return casted

    # ───────────────────────── users ─────────────────────────
    async def ensure_user(self, user_id: int) -> aiosqlite.Row:
        """ユーザー行を保証して返す。新規なら starting_balance を付与しログに残す。"""
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is not None:
            return row

        start = int(self.setting("starting_balance", 1000))
        await self.conn.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, start)
        )
        if start:
            await self._log_tx(user_id, start, start, "initial_grant", None)
        await self.conn.commit()
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        return await cur.fetchone()  # type: ignore[return-value]

    async def get_balance(self, user_id: int) -> int:
        row = await self.ensure_user(user_id)
        return int(row["balance"])

    async def is_frozen(self, user_id: int) -> bool:
        row = await self.ensure_user(user_id)
        return bool(row["frozen"])

    async def _log_tx(
        self, user_id: int, delta: int, balance_after: int, reason: str, ref: str | None
    ) -> None:
        await self.conn.execute(
            "INSERT INTO tx_logs (user_id, delta, balance_after, reason, ref) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, delta, balance_after, reason, ref),
        )

    async def adjust_balance(
        self,
        user_id: int,
        delta: int,
        reason: str,
        ref: str | None = None,
        *,
        allow_negative: bool = False,
    ) -> int:
        """残高を delta だけ増減し、tx_logs に記録。新残高を返す。

        delta<0 で残高を割り込む場合、allow_negative=False なら
        InsufficientFunds を送出(管理操作では True を許す)。
        呼び出し側は user_lock を取った状態で使うこと。
        """
        await self.ensure_user(user_id)
        cur = await self.conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        balance = int((await cur.fetchone())["balance"])  # type: ignore[index]
        new_balance = balance + delta
        if new_balance < 0 and not allow_negative:
            raise InsufficientFunds(
                f"残高不足: 所持 {balance} に対し {-delta} を引き落とそうとしました。"
            )
        await self.conn.execute(
            "UPDATE users SET balance = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE user_id = ?",
            (new_balance, user_id),
        )
        await self._log_tx(user_id, delta, new_balance, reason, ref)
        await self.conn.commit()
        return new_balance

    async def set_balance(self, user_id: int, value: int, reason: str) -> int:
        """残高を絶対値で設定(管理操作)。差分を tx_logs に残す。"""
        await self.ensure_user(user_id)
        cur = await self.conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        balance = int((await cur.fetchone())["balance"])  # type: ignore[index]
        delta = value - balance
        await self.conn.execute(
            "UPDATE users SET balance = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE user_id = ?",
            (value, user_id),
        )
        await self._log_tx(user_id, delta, value, reason, None)
        await self.conn.commit()
        return value

    async def set_frozen(self, user_id: int, frozen: bool) -> None:
        await self.ensure_user(user_id)
        await self.conn.execute(
            "UPDATE users SET frozen = ? WHERE user_id = ?",
            (1 if frozen else 0, user_id),
        )
        await self.conn.commit()

    async def set_active_match(self, user_id: int, match_id: str | None) -> None:
        await self.conn.execute(
            "UPDATE users SET active_match = ? WHERE user_id = ?", (match_id, user_id)
        )
        await self.conn.commit()

    # ───────────────────────── streak / daily ─────────────────────────
    async def update_daily(self, user_id: int, streak: int, ts: str) -> None:
        await self.conn.execute(
            "UPDATE users SET last_daily = ?, daily_streak = ? WHERE user_id = ?",
            (ts, streak, user_id),
        )
        await self.conn.commit()

    async def pay_daily(
        self, user_id: int, amount: int, streak: int, ts: str, reason: str = "daily"
    ) -> int:
        """デイリーを **アトミックに** 精算する。

        balance / last_daily / daily_streak / tx_logs を **1つの commit** で更新する。
        途中で例外が出れば rollback して、何も更新されない状態に戻す。
        これにより「last_daily だけ進んで残高は据え置き」の不整合を防ぐ。
        呼び出し側は user_lock を取得した状態で使うこと。
        """
        await self.ensure_user(user_id)
        try:
            cur = await self.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            )
            balance = int((await cur.fetchone())["balance"])  # type: ignore[index]
            new_balance = balance + amount
            await self.conn.execute(
                "UPDATE users SET balance = ?, last_daily = ?, daily_streak = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE user_id = ?",
                (new_balance, ts, streak, user_id),
            )
            await self._log_tx(user_id, amount, new_balance, reason, None)
            await self.conn.commit()
            return new_balance
        except Exception:
            await self.conn.rollback()
            raise

    async def set_win_streak(self, user_id: int, value: int) -> None:
        await self.conn.execute(
            "UPDATE users SET win_streak = ? WHERE user_id = ?", (value, user_id)
        )
        await self.conn.commit()

    # ───────────────────────── jackpot ─────────────────────────
    async def jackpot_amount(self, name: str = "slot") -> int:
        cur = await self.conn.execute(
            "SELECT amount FROM jackpot WHERE name = ?", (name,)
        )
        row = await cur.fetchone()
        return int(row["amount"]) if row else 0

    async def jackpot_add(self, delta: int, name: str = "slot") -> int:
        await self.conn.execute(
            "INSERT INTO jackpot (name, amount) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET amount = amount + ?",
            (name, max(0, delta), delta),
        )
        await self.conn.commit()
        return await self.jackpot_amount(name)

    async def jackpot_reset(self, seed: int, name: str = "slot") -> None:
        await self.conn.execute(
            "UPDATE jackpot SET amount = ? WHERE name = ?", (seed, name)
        )
        await self.conn.commit()

    # ───────────────────────── 統計(管理パネル用) ─────────────────────────
    async def economy_stats(self) -> dict[str, Any]:
        c = self.conn
        total = int((await (await c.execute(
            "SELECT COALESCE(SUM(balance),0) s FROM users")).fetchone())["s"])
        users = int((await (await c.execute(
            "SELECT COUNT(*) n FROM users")).fetchone())["n"])
        richest = list(await (await c.execute(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 5"
        )).fetchall())
        jp = await self.jackpot_amount("slot")

        # チップが消滅した総量(シンク)を tx_logs から正確に集計する。
        #  - PVE: 賭けた額 − 払い戻し(ハウスエッジ分が消滅)
        #  - PVP: pvp_escrow/win/refund の純額の符号反転 = 徴収したレーキ
        #  - 保有税・管理没収: そのまま消滅
        async def _sum(where: str) -> int:
            row = await (await c.execute(
                f"SELECT COALESCE(SUM(delta),0) s FROM tx_logs WHERE {where}"
            )).fetchone()
            return int(row["s"])

        pve_house = -(await _sum(
            "reason IN ('slot_bet','chinchiro_bet','slot_win','chinchiro_win','slot_jackpot')"
        ))
        pvp_rake = -(await _sum("reason IN ('pvp_escrow','pvp_win','pvp_refund')"))
        explicit = -(await _sum("reason IN ('holding_tax','admin_take')"))
        sink = pve_house + pvp_rake + explicit
        return {
            "total_supply": total,
            "user_count": users,
            "richest": richest,
            "jackpot": jp,
            "lifetime_sink": sink,
        }

    async def leaderboard(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        return list(await cur.fetchall())

    async def recent_tx(self, user_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT delta, balance_after, reason, ref, ts FROM tx_logs "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return list(await cur.fetchall())

    # ───────────────────────── admin log ─────────────────────────
    async def log_admin(
        self, admin_id: int, action: str, target_id: int | None, detail: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO admin_logs (admin_id, action, target_id, detail) "
            "VALUES (?, ?, ?, ?)",
            (admin_id, action, target_id, detail),
        )
        await self.conn.commit()
