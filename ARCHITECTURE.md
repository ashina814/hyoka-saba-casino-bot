# アーキテクチャ

## 全体像

```
                       Discord Gateway
                              │
                              ▼
                ┌─────────────────────────┐
                │     CasinoBot (Cog)     │
                │  cogs/{game,hub,...}    │
                └────────────┬────────────┘
                             │
                ┌────────────┴────────────┐
                │   core/ (純粋ロジック)  │
                │  deck dice hand badges │
                │  global_jackpot ...     │
                └────────────┬────────────┘
                             │
                ┌────────────┴────────────┐
                │  db/dao.py (aiosqlite)  │
                │       casino.db         │
                └─────────────────────────┘
```

## レイヤ
| 層 | 役割 | 触っていいもの |
|---|---|---|
| `cogs/` | Discord UI 層 (View/Modal/コマンド) | core / ui / db.dao |
| `ui/common.py` | 共通View部品 (BetModal, PlayAgainView, 等) | core のみ |
| `core/` | 純粋なゲーム/経済ロジック | Discord に依存しない |
| `db/dao.py` | DB アクセス層 (aiosqlite) | DB 操作だけ |
| `db/schema.sql` | スキーマ + 既定 settings | 起動時にidempotent適用 |

`core/` は **Discord も DB も知らない**ので、テストは Bot 抜きで走ります（`tests/test_*.py`）。

## 主要テーブル
- `users` — 残高、frozen、win_streak、max_win_streak、daily 関連
- `tx_logs` — 全チップ移動の不可逆ログ(監査用)
- `settings` — 運用中に変更可能なチューニング値(key/value/vtype/label)
- `matches` — PVPゲームのマッチ状態
- `jackpot` — スロットJP プール
- `global_jackpot` — 全体JP プール（PVE横串）
- `tournaments` — 大会(同時1個)
- `economy_snapshots` — 日次スナップショット(推移分析用)
- `claimed_challenges` — デイリーチャレンジ受取記録
- `omikuji_claimed` — おみくじ受取記録
- `badges` — 称号獲得記録
- `exchange_requests` — 両替申請
- `admins` — DB管理者(env由来とは別)
- `user_meta` — チュートリアル既読等の小フラグ
- `user_limits` — 自己制限(1日ベット上限)
- `last_bets` — ベットプリセット default 用
- `invites` — 招待ボーナス受取記録
- `shop_purchases` — ショップ購入履歴
- `gacha_inventory` — ガチャ獲得記録

スキーマ変更時は `db/dao.py` の `_migrate()` に idempotent な ALTER を1行追加。

## ボット起動シーケンス
1. `bot.py main()` → `load_config()` → `CasinoBot.__init__()`
2. `setup_hook()`:
   - `db.connect()` → スキーマ適用 → `_migrate()` → settings reload
   - `refresh_admins()` で env ∪ DB の admin set を構成
   - `_resolve_cogs(cfg)` で BASE_COGS + 有効ゲーム Cog をロード
   - スラッシュコマンドを同期 (DEV_GUILD_ID あれば即時、なければ global)
3. `on_ready()`:
   - presence 設定
   - `_wal_loop` (毎時)、`_backup_loop` (日次) を開始
   - 各 Cog の永続View 再登録

## 主要な横串フック
| イベント | 呼び出し場所 | 副作用 |
|---|---|---|
| PVE ベット | 各 `_start` 内、引き落とし直後 | 全体JP積立＋当選判定、`set_last_bet`、自己制限ガード |
| 勝利確定 | 各 `_settle` 内 | `set_win_streak`、`badges.on_*` フック |
| 大会期間内のイベント | 自動 (tx_logs から集計) | `score_*` 関数で順位計算 |
| 残高変更 | `db.adjust_balance` | 必ず `tx_logs` に記録 |
| エラー発生 | `bot.on_app_command_error` | 管理者全員に DM で Traceback |

## マルチサーバー構成
**同じソース・別 `.env`** で複数 Bot プロセスを並走させる方式。
- `DISCORD_TOKEN`: 別Botアプリ
- `DB_PATH`: 別ファイル(残高混在防止)
- `ENABLED_GAMES`: そのサーバー向けゲームのみ
- `CURRENCY_NAME` / `CURRENCY_EMOJI`: そのサーバーの呼称

新インスタンスを追加するときは `deploy/ADD_NEW_INSTANCE.md` 参照。

## スラッシュコマンド構成
| コマンド | 用途 |
|---|---|
| `/カジノ` | メインハブ(ゲーム/経済/その他へのルーティング) |
| `/プロフィール` | 自分の残高/履歴/統計/称号/自己制限 タブUI |
| `/送金` | チップ送金(メンション選択UIが必要なため単独) |
| `/管理` | 管理ダッシュボード(ページ式、env管理者 vs DB管理者で権限差) |

それ以外は全部ハブ経由のボタン操作。コマンド数を **4個に固定** することで Discord 補完を汚さない設計。

## 安全装置サマリ
- 管理操作: 確認ステップ / 高額理由必須 / 自己付与禁止 / 5秒CD / Undo / 自動監査ログ
- ユーザー: 自己制限機能、メンテモード、凍結
- DB: ローテ済みバックアップ(日次14日)、WALチェックポイント(毎時)
- Bot: 例外時の管理者DM、catch-all エラーハンドラ
