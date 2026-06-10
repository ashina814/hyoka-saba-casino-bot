-- カジノBot スキーマ
-- 方針: 全チップ移動は tx_logs に必ず残す(監査・不正調査用)。
--       チューニング値は settings に置き、管理パネルから実行中に変更可能にする。

PRAGMA journal_mode = WAL;        -- 同時読み書きに強い
PRAGMA foreign_keys = ON;

-- ───────────────────────── ユーザー残高 ─────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    balance      INTEGER NOT NULL DEFAULT 0,
    frozen       INTEGER NOT NULL DEFAULT 0,   -- 1=賭博凍結中
    last_daily   TEXT,                          -- ISO8601 UTC、最後にデイリーを受け取った時刻
    daily_streak INTEGER NOT NULL DEFAULT 0,    -- 連続ログイン日数
    win_streak   INTEGER NOT NULL DEFAULT 0,    -- 現在の連勝数(全ゲーム共通の演出用)
    active_match TEXT,                          -- 参加中の PVP マッチID(二重参加防止)。NULL=なし
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ───────────────────────── 取引ログ(監査) ─────────────────────────
CREATE TABLE IF NOT EXISTS tx_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,             -- +付与 / -没収
    balance_after INTEGER NOT NULL,
    reason        TEXT NOT NULL,                -- 'slot_bet','slot_win','daily','rake','admin_give' 等
    ref           TEXT,                          -- 関連 match_id 等
    ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_user_ts ON tx_logs(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_tx_reason  ON tx_logs(reason);

-- ───────────────────────── チューニング設定 ─────────────────────────
-- value は文字列で保持し、vtype に従って読み出し側でキャストする。
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    vtype TEXT NOT NULL DEFAULT 'str',          -- 'int' | 'float' | 'bool' | 'str'
    label TEXT NOT NULL DEFAULT ''               -- 管理パネルでの表示名・説明
);

-- ───────────────────────── PVP マッチ ─────────────────────────
CREATE TABLE IF NOT EXISTS matches (
    match_id   TEXT PRIMARY KEY,
    game       TEXT NOT NULL,                    -- 'holdem' | 'draw' | 'chohan'
    channel_id INTEGER NOT NULL,
    host_id    INTEGER NOT NULL,
    bet        INTEGER NOT NULL,                 -- 基本ベット額(参加費/ブラインド基準)
    pot        INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'lobby',    -- 'lobby'|'in_progress'|'finished'|'cancelled'
    data       TEXT NOT NULL DEFAULT '{}',       -- ゲーム固有状態(プレイヤー・手札・ラウンド等)を JSON で
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_status ON matches(status);

-- ───────────────────────── ジャックポット(プログレッシブ) ─────────────────────────
CREATE TABLE IF NOT EXISTS jackpot (
    name   TEXT PRIMARY KEY,                     -- 'slot'
    amount INTEGER NOT NULL DEFAULT 0
);

-- ───────────────────────── 管理操作ログ ─────────────────────────
CREATE TABLE IF NOT EXISTS admin_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id  INTEGER NOT NULL,
    action    TEXT NOT NULL,                     -- 'give','take','set','freeze','config' 等
    target_id INTEGER,
    detail    TEXT,
    ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ───────────────────────── 設定の初期値(マイルド設定) ─────────────────────────
-- INSERT OR IGNORE なので、既存値は上書きしない(運用中の調整が消えない)。
INSERT OR IGNORE INTO settings (key, value, vtype, label) VALUES
    ('starting_balance',   '1000', 'int',   '初期残高'),
    ('min_bet',            '10',   'int',   '最低ベット額'),
    ('max_bet',            '100000','int',  '最高ベット額'),

    -- デイリー(ソース): 残高に応じて減衰させ、資産家の雪だるまを抑える
    ('daily_base',         '1000', 'int',   'デイリー基本額'),
    ('daily_decay_enabled','1',    'bool',  'デイリー残高減衰 ON/OFF'),
    ('daily_decay_pivot',  '20000','int',   'この残高でデイリーが半減する基準'),
    ('daily_streak_bonus', '100',  'int',   '連続ログイン1日あたりの加算'),
    ('daily_streak_cap',   '7',    'int',   '連続ログインボーナスの上限日数'),

    -- PVE ハウスエッジ(ここを管理パネルから自由にいじる)
    ('slot_house_edge',    '0.05', 'float', 'スロットのハウスエッジ'),
    ('chinchiro_house_edge','0.05','float', 'チンチロのハウスエッジ'),

    -- スロット ジャックポット(再分配なのでインフレ中立)
    ('jackpot_enabled',    '1',    'bool',  'スロットJP ON/OFF'),
    ('jackpot_contrib',    '0.01', 'float', 'ベットからJPへ積む割合'),
    ('jackpot_seed',       '10000','int',   'JP当選後の再シード額'),

    -- PVP レーキ(シンク): 勝ち分から徴収して消滅(インフレ対策)
    ('pvp_rake',           '0.03', 'float', 'PVP手数料(レーキ)率'),

    -- 保有税(シンク): デフォルト OFF。退蔵対策として後から ON にできる
    ('holding_tax_enabled','0',    'bool',  '保有税 ON/OFF'),
    ('holding_tax_threshold','100000','int','保有税の課税閾値'),
    ('holding_tax_rate',   '0.01', 'float', '保有税 日次率(閾値超過分に課税)');

INSERT OR IGNORE INTO jackpot (name, amount) VALUES ('slot', 10000);
