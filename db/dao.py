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
        await self._migrate()
        await self._reload_settings()

    async def _migrate(self) -> None:
        """既存DBに後から追加された列を補う簡易マイグレーション。

        スキーマに追記された ALTER TABLE 系を idempotent に流す。
        SQLite には IF NOT EXISTS が ALTER にないので、列の有無を見てから流す。
        """
        cur = await self._conn.execute("PRAGMA table_info(users)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "max_win_streak" not in cols:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN max_win_streak INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.commit()

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
        # 最高記録も同時に更新(自己統計の表示用)
        await self.conn.execute(
            "UPDATE users SET win_streak = ?, "
            "max_win_streak = CASE WHEN ? > max_win_streak THEN ? ELSE max_win_streak END "
            "WHERE user_id = ?",
            (value, value, value, user_id),
        )
        await self.conn.commit()

    async def user_stats(self, user_id: int) -> dict[str, Any]:
        """自分用統計。ゲーム別収支、JP獲得回数、勝率、最高連勝など。

        ゲーム別収支は `<game>_bet`(負側) と `<game>_win`/`<game>_jackpot` 等(正側)
        の delta を合算。tx_logs 上の値そのままなので、内部で消滅したシンク分も
        自然に「負け」に含まれて見える。
        """
        await self.ensure_user(user_id)
        c = self.conn

        async def _sum(where: str) -> int:
            row = await (await c.execute(
                f"SELECT COALESCE(SUM(delta),0) s FROM tx_logs "
                f"WHERE user_id = ? AND {where}", (user_id,)
            )).fetchone()
            return int(row["s"])

        async def _cnt(where: str) -> int:
            row = await (await c.execute(
                f"SELECT COUNT(*) n FROM tx_logs "
                f"WHERE user_id = ? AND {where}", (user_id,)
            )).fetchone()
            return int(row["n"])

        games = {
            "slot":      ("slot_bet|slot_win|slot_jackpot",
                          "slot_bet"),
            "chinchiro": ("chinchiro_bet|chinchiro_win",
                          "chinchiro_bet"),
            "hilo":      ("hilo_bet|hilo_win",
                          "hilo_bet"),
            "blackjack": ("blackjack_bet|blackjack_win|blackjack_double|blackjack_split",
                          "blackjack_bet"),
            "pvp":       ("pvp_escrow|pvp_win|pvp_refund",
                          "pvp_escrow"),
        }
        per_game: dict[str, dict[str, int]] = {}
        for key, (delta_reasons, play_reason) in games.items():
            where_d = "reason IN (" + ",".join(f"'{r}'" for r in delta_reasons.split("|")) + ")"
            where_p = f"reason = '{play_reason}'"
            per_game[key] = {
                "net": await _sum(where_d),
                "plays": await _cnt(where_p),
            }

        row = await (await c.execute(
            "SELECT balance, win_streak, max_win_streak, daily_streak, created_at "
            "FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()

        jp = await _cnt("reason='slot_jackpot'")
        total_bets = -await _sum(
            "reason IN ('slot_bet','chinchiro_bet','hilo_bet','blackjack_bet',"
            "'blackjack_double','blackjack_split','pvp_escrow')"
        )

        return {
            "balance": int(row["balance"]),
            "win_streak": int(row["win_streak"]),
            "max_win_streak": int(row["max_win_streak"]),
            "daily_streak": int(row["daily_streak"]),
            "created_at": row["created_at"],
            "per_game": per_game,
            "jackpots_won": jp,
            "total_bet_volume": total_bets,
        }

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

    # ───────────────────────── 両替申請 ─────────────────────────
    # direction ラベル
    EX_ZENY_TO_COIN = "zeny_to_coin"
    EX_COIN_TO_ZENY = "coin_to_zeny"

    async def create_exchange_request(
        self,
        user_id: int,
        direction: str,
        send_amount: int,
        receive_amount: int,
        fee_amount: int,
    ) -> int:
        """両替申請を作成して id を返す(pending)。"""
        cur = await self.conn.execute(
            "INSERT INTO exchange_requests "
            "(user_id, direction, send_amount, receive_amount, fee_amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, direction, send_amount, receive_amount, fee_amount),
        )
        await self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    async def attach_exchange_message(
        self, req_id: int, channel_id: int, message_id: int
    ) -> None:
        """承認メッセージID を申請に紐付け(後から編集や復旧に使う)。"""
        await self.conn.execute(
            "UPDATE exchange_requests SET log_channel_id=?, log_message_id=? "
            "WHERE id=?",
            (channel_id, message_id, req_id),
        )
        await self.conn.commit()

    async def get_exchange_request(self, req_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM exchange_requests WHERE id=?", (req_id,)
        )
        return await cur.fetchone()

    async def set_exchange_status(
        self, req_id: int, status: str, approver_id: int | None
    ) -> None:
        await self.conn.execute(
            "UPDATE exchange_requests SET status=?, approver_id=?, "
            "decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (status, approver_id, req_id),
        )
        await self.conn.commit()

    async def daily_exchange_received(self, user_id: int, direction: str) -> int:
        """直近24時間に同じ方向で **受領予定/受領済み** になっている合計受領額。

        日次上限の判定はこの値 + 今回希望額が cap を超えないか で行う。
        pending と approved を含む(申請段階で枠を予約する形)。
        """
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(receive_amount),0) s FROM exchange_requests "
            "WHERE user_id=? AND direction=? AND status IN ('pending','approved') "
            "AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day')",
            (user_id, direction),
        )
        return int((await cur.fetchone())["s"])  # type: ignore[index]

    async def expired_pending_requests(self):
        """有効期限切れの pending を返す(自動失効処理で使う)。"""
        ttl = int(self.setting("exchange_request_ttl_hours", 48))
        cur = await self.conn.execute(
            "SELECT * FROM exchange_requests WHERE status='pending' "
            "AND created_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)",
            (f"-{ttl} hours",),
        )
        return list(await cur.fetchall())

    # ───────────────────────── 大会 ─────────────────────────
    async def start_tournament(
        self, name: str, kind: str, prize_pool: int,
        start_ts: int, end_ts: int, started_by: int
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO tournaments (name, kind, prize_pool, start_ts, end_ts, started_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, kind, prize_pool, start_ts, end_ts, started_by),
        )
        await self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    async def current_tournament(self) -> Any:
        cur = await self.conn.execute(
            "SELECT * FROM tournaments WHERE status='running' ORDER BY id DESC LIMIT 1"
        )
        return await cur.fetchone()

    async def finish_tournament(self, tour_id: int, winners_json: str) -> None:
        await self.conn.execute(
            "UPDATE tournaments SET status='finished', winners=? WHERE id=?",
            (winners_json, tour_id),
        )
        await self.conn.commit()

    async def cancel_tournament(self, tour_id: int) -> None:
        await self.conn.execute(
            "UPDATE tournaments SET status='cancelled' WHERE id=?",
            (tour_id,),
        )
        await self.conn.commit()

    # ───────────────────────── 全体JP ─────────────────────────
    async def global_jp_amount(self) -> int:
        cur = await self.conn.execute("SELECT amount FROM global_jackpot WHERE id=1")
        row = await cur.fetchone()
        return int(row["amount"]) if row else 0

    async def global_jp_add(self, delta: int) -> int:
        await self.conn.execute(
            "UPDATE global_jackpot SET amount = amount + ? WHERE id=1",
            (max(0, delta),),
        )
        await self.conn.commit()
        return await self.global_jp_amount()

    async def global_jp_win(self, winner_id: int) -> int:
        """当選処理。獲得額(現プール)を返し、プールをseed額にリセット。"""
        amount = await self.global_jp_amount()
        seed = int(self.setting("global_jp_seed", 0) or 0)
        await self.conn.execute(
            "UPDATE global_jackpot SET amount=?, last_winner=?, last_amount=?, "
            "last_won_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1",
            (seed, winner_id, amount),
        )
        await self.conn.commit()
        return amount

    # ───────────────────────── 称号 ─────────────────────────
    async def award_badge(self, user_id: int, badge_id: str) -> bool:
        """称号を付与。既に持っていれば False を返す(再付与なし)。"""
        try:
            await self.conn.execute(
                "INSERT INTO badges (user_id, badge_id) VALUES (?, ?)",
                (user_id, badge_id),
            )
            await self.conn.commit()
            return True
        except Exception:
            return False

    async def has_badge(self, user_id: int, badge_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM badges WHERE user_id=? AND badge_id=?",
            (user_id, badge_id),
        )
        return (await cur.fetchone()) is not None

    async def user_badges(self, user_id: int) -> list[str]:
        cur = await self.conn.execute(
            "SELECT badge_id FROM badges WHERE user_id=? ORDER BY earned_at",
            (user_id,),
        )
        return [r["badge_id"] for r in await cur.fetchall()]

    # ───────────────────────── 統計(管理パネル用) ─────────────────────────
    async def economy_stats(self) -> dict[str, Any]:
        c = self.conn
        # owner_id (お釈迦さま=焼却用アカウント) は経済統計から除外する。
        # 焼却済みカジノコインが残高として残ったままになるため、含めると総供給量・
        # 資産上位ランキングが歪む。
        owner_id = int(self.setting("owner_id", 0) or 0)
        excl = (owner_id,) if owner_id else (0,)  # 0 を渡してもどこにも一致しない
        total = int((await (await c.execute(
            "SELECT COALESCE(SUM(balance),0) s FROM users WHERE user_id<>?", excl
        )).fetchone())["s"])
        users = int((await (await c.execute(
            "SELECT COUNT(*) n FROM users WHERE user_id<>?", excl
        )).fetchone())["n"])
        richest = list(await (await c.execute(
            "SELECT user_id, balance FROM users WHERE user_id<>? "
            "ORDER BY balance DESC LIMIT 5", excl
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

    async def economy_dashboard(self) -> dict[str, Any]:
        """経済ダッシュボード用の集計。

        owner_id(お釈迦さま) は供給/集中/Gini から除外。お釈迦さま口座の残高は
        焼却済みカジノコインを抱える設計なので含めると指標が歪む。
        """
        from core.economy import gini as _gini
        c = self.conn
        owner_id = int(self.setting("owner_id", 0) or 0)
        excl = (owner_id if owner_id else 0,)

        # 全残高(>0 のみ Gini に使う)。owner 除外。
        cur = await c.execute(
            "SELECT balance FROM users WHERE user_id<>? ORDER BY balance ASC", excl
        )
        balances = [int(r["balance"]) for r in await cur.fetchall()]
        positive = [b for b in balances if b > 0]
        total_supply = sum(positive)
        user_count = len(balances)
        g = _gini(positive)
        # 上位10%が持つ割合
        if positive:
            sorted_desc = sorted(positive, reverse=True)
            top_n = max(1, len(sorted_desc) // 10)
            top10_share = sum(sorted_desc[:top_n]) / total_supply if total_supply else 0.0
        else:
            top10_share = 0.0
        median = positive[len(positive) // 2] if positive else 0

        # アクティブ: 直近7日に tx あるユーザー数
        cur = await c.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM tx_logs "
            "WHERE ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days')"
        )
        active_count = int((await cur.fetchone())["n"])

        # ソース/シンク 内訳(reason 別 SUM(delta))
        async def _sum_period(where_extra: str, days: int) -> int:
            row = await (await c.execute(
                f"SELECT COALESCE(SUM(delta),0) s FROM tx_logs "
                f"WHERE {where_extra} "
                f"AND ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-{days} days')"
            )).fetchone()
            return int(row["s"])

        # 「発行」= 残高が増えるトランザクション(正の delta)。
        # 「消滅」= 残高が減るトランザクション(負の delta、絶対値)。
        # 純発行 = 発行 + 消滅(消滅が負なのでそのまま足す)
        async def _period(days: int) -> dict[str, int]:
            return {
                "source": await _sum_period("delta > 0", days),
                "sink": -(await _sum_period("delta < 0", days)),
                "net": await _sum_period("1=1", days),
            }
        d1 = await _period(1)
        d7 = await _period(7)
        d30 = await _period(30)

        # ベットボリューム(24h): ゲームの bet 系 reason の絶対値
        bet_reasons = ("slot_bet", "chinchiro_bet", "hilo_bet",
                       "blackjack_bet", "blackjack_double", "blackjack_split",
                       "pvp_escrow")
        in_list = ",".join(f"'{r}'" for r in bet_reasons)
        bet_vol_24h = -(await _sum_period(f"reason IN ({in_list})", 1))

        # reason 別ランキング(ソース/シンク TOP)
        cur = await c.execute(
            f"SELECT reason, SUM(delta) s FROM tx_logs "
            f"WHERE delta > 0 "
            f"AND ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days') "
            f"GROUP BY reason ORDER BY s DESC LIMIT 5"
        )
        top_sources = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            f"SELECT reason, -SUM(delta) s FROM tx_logs "
            f"WHERE delta < 0 "
            f"AND ts >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days') "
            f"GROUP BY reason ORDER BY s DESC LIMIT 5"
        )
        top_sinks = [dict(r) for r in await cur.fetchall()]

        jp = await self.jackpot_amount("slot")

        return {
            "total_supply": total_supply,
            "user_count": user_count,
            "active_count_7d": active_count,
            "gini": g,
            "top10_share": top10_share,
            "median_balance": median,
            "jackpot": jp,
            "period_1d": d1,
            "period_7d": d7,
            "period_30d": d30,
            "bet_volume_24h": bet_vol_24h,
            "top_sources_7d": top_sources,
            "top_sinks_7d": top_sinks,
        }

    async def write_snapshot_today(self) -> dict[str, Any]:
        """今日(UTC)のスナップショットを INSERT OR REPLACE。集計後のメトリクスを返す。"""
        m = await self.economy_dashboard()
        today = self._today_utc()
        await self.conn.execute(
            "INSERT INTO economy_snapshots "
            "(date, total_supply, user_count, active_count, gini, top10_share, "
            "median_balance, jp_amount, monthly_net) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "total_supply=excluded.total_supply, user_count=excluded.user_count, "
            "active_count=excluded.active_count, gini=excluded.gini, "
            "top10_share=excluded.top10_share, median_balance=excluded.median_balance, "
            "jp_amount=excluded.jp_amount, monthly_net=excluded.monthly_net",
            (today, m["total_supply"], m["user_count"], m["active_count_7d"],
             m["gini"], m["top10_share"], m["median_balance"], m["jackpot"],
             m["period_30d"]["net"]),
        )
        await self.conn.commit()
        return m

    @staticmethod
    def _today_utc() -> str:
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    async def recent_snapshots(self, n: int = 14) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM economy_snapshots ORDER BY date DESC LIMIT ?", (n,)
        )
        return list(await cur.fetchall())

    async def leaderboard(self, limit: int = 10) -> list[aiosqlite.Row]:
        owner_id = int(self.setting("owner_id", 0) or 0)
        cur = await self.conn.execute(
            "SELECT user_id, balance FROM users WHERE user_id<>? "
            "ORDER BY balance DESC LIMIT ?",
            (owner_id if owner_id else 0, limit),
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
