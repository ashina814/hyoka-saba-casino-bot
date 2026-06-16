# 🎰 Discord カジノBot

PVE/PVP のミニカジノを Discord 上で運営できるBot。
コマンドは全て日本語、操作はパネル(ボタン)中心。
チップ経済は Bot 内で完結し、**インフレ対策・射幸性演出・管理ダッシュボード・大会・全体JP・称号・ガチャ・ショップ** まで一通り揃ったマルチ機能セット。

詳しい設計は [`ARCHITECTURE.md`](ARCHITECTURE.md)、
運用は [`OPS.md`](OPS.md)、
クライアント運営者向けは [`CLIENT_GUIDE.md`](CLIENT_GUIDE.md) を参照。

---

## ゲーム
| ゲーム | 種別 | 概要 |
|---|---|---|
| 🎰 スロット | PVE | 3リール、段階表示・リーチ演出・プログレッシブJP |
| 🎲 チンチロ | PVE | 親(Bot)と役勝負(ピンゾロ/シゴロ/ヒフミ等) |
| 📈 ハイロー | PVE | 次のカードが上か下か。連続で倍率上昇 |
| 🃏 ブラックジャック | PVE | Hit/Stand/Double/Split。ナチュラル1.5倍、Stand on Soft 17 |
| 🀄 丁半 | PVP | 偶奇に賭けて当てた側が山分け |
| ♠️ ホールデム | PVP | テキサスホールデム(サイドポット対応) |
| 🎴 ドローポーカー | PVP | 5カードドロー、1回交換 |

`.env` の `ENABLED_GAMES` でサーバーごとに有効ゲームを切り替え可能。

## スラッシュコマンド (4個固定)

| コマンド | 用途 |
|---|---|
| `/カジノ` | メインハブ(ゲーム/経済/その他へのルーティング) |
| `/プロフィール` | 残高/履歴/統計/称号/自己制限のタブUI |
| `/送金` | チップ送金 |
| `/管理` | 管理ダッシュボード(env管理者 vs DB管理者で権限差) |

その他の機能は全てハブパネル経由のボタン操作。コマンド数を **4個に固定** することで Discord 補完を汚さない設計。

## メイン機能一覧

### プレイヤー向け
- ✅ **デイリー** — 24h毎に貰える基本収入(残高に応じて減衰)
- ✅ **ランキング** — 残高長者番付
- ✅ **送金** — チップを他人に送付
- ✅ **🎁 ガチャ** — レアリティ別アイテム獲得 (SSR まで)
- ✅ **🛒 ショップ** — チップで称号購入(コレクション)
- ✅ **🗓️ デイリーチャレンジ** — 日替わり3個、達成で報酬
- ✅ **🎴 おみくじ** — 1日1回、運勢で日替わりおまけ
- ✅ **🏆 大会** — 期間内ランキングで賞金分配(収支/連勝/JP獲得 3種)
- ✅ **🌟 全体ジャックポット** — 全PVEから少しずつ積立、確率自動上昇
- ✅ **🏅 称号** — 偉業達成で獲得、プロフィールに表示
- ✅ **🏛️ ハイランカー殿堂** — 歴代JP獲得・最高連勝・大会優勝者
- ✅ **🛡️ 自己制限** — 1日のベット上限を自分で設定可
- ✅ **🎁 招待ボーナス** — 招待した/された両方に報酬
- ✅ **💱 両替** — ゼニー(別Botの第一通貨)↔ カジノコイン半自動承認制

### 運営向け
- ✅ **📊 経済ダッシュボード** — Gini・インフレ・健康度を3タブで可視化
- ✅ **🛠️ メンテモード** — 一時停止/再開を1ボタン
- ✅ **🚀 ブースト** — 期間限定配当倍率アップを叩ける
- ✅ **🚨 危険ゾーン** — 残高直接セット、管理者追加削除(env管理者専用)
- ✅ **🔐 安全装置** — 確認ステップ、高額理由必須、自己付与禁止、CD、Undo、自動監査ログ
- ✅ **🚨 例外時の運営DM** — 未捕捉エラーを管理者全員にTraceback付きで通知
- ✅ **📦 日次自動バックアップ** — SQLite VACUUM INTO、14日保持

## 設計の特徴
- **マルチサーバー対応** — 同じソース、別Bot/別DB で複数サーバーに展開可能
- **チューニングは settings テーブル** — 全パラメータが運用中変更可能(再起動不要)
- **乱数は `secrets.SystemRandom`** — 暗号論的乱数で公正性担保
- **インフレ中立な全体JP** — 積立=ベット側からの再分配のみ、Bot側で新規発行ゼロ
- **取引ログ完備** — 全チップ移動が `tx_logs` に残る(削除不可、監査可能)

## ディレクトリ
```
bot.py                  起動、Cogロード、エラーハンドラ、定期タスク
config.py               .env 読み込み、ALL_GAMES 定義
core/                   Discord非依存の純粋ロジック
  deck dice hand badges economy match
  global_jackpot external_currency
db/                     スキーマ + DAO (aiosqlite)
cogs/                   ゲーム/経済/管理など各Cog
ui/common.py            共通View部品 (Modal/Button/Embed)
deploy/                 systemdユニット、VPS新規インスタンス追加手順
tests/                  Discord非依存のロジック検証
docs/                   ARCHITECTURE.md / OPS.md / CLIENT_GUIDE.md(本リポ直下)
```

## ローカル起動(Windows)
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env       # DISCORD_TOKEN / ADMIN_IDS を入力
.venv\Scripts\python bot.py
```

## テスト
```powershell
.venv\Scripts\python tests\test_core.py
.venv\Scripts\python tests\test_holdem.py
.venv\Scripts\python tests\test_daily.py
.venv\Scripts\python tests\test_exchange.py
.venv\Scripts\python tests\test_games.py
.venv\Scripts\python tests\test_features.py
.venv\Scripts\python tests\test_dashboard.py
.venv\Scripts\python tests\test_quality.py
.venv\Scripts\python tests\test_event_pack.py
```

## VPS デプロイ
[`deploy/README.md`](deploy/README.md) (基本) / [`deploy/ADD_NEW_INSTANCE.md`](deploy/ADD_NEW_INSTANCE.md) (2台目以降の並走) を参照。

## 主要設定キー (`/管理` → 設定変更 で随時変更可)
| キー | 既定 | 説明 |
|---|---|---|
| `starting_balance` | 1000 | 初期残高 |
| `daily_base` | 1000 | デイリー基本額 |
| `daily_decay_pivot` | 20000 | デイリーが半減する残高基準 |
| `slot_house_edge` | 0.05 | スロットエッジ |
| `pvp_rake` | 0.03 | PVP手数料 |
| `jackpot_contrib` | 0.01 | スロットJP積立率 |
| `global_jp_contrib` | 0.005 | 全体JP積立率 |
| `global_jp_full_speed` | 5000000 | 全体JP当選確率がほぼ100%/1ベットになる基準額 |
| `exchange_fee_rate` | 0.10 | 両替手数料 |
| `exchange_daily_cap` | 50000 | 両替日次上限(方向別) |
| `admin_confirm_threshold` | 100000 | 高額管理操作の理由必須閾値 |
| `holding_tax_enabled` | 0 | 保有税ON/OFF |
| `backup_keep_days` | 14 | DBバックアップ保持日数 |

## 関連ドキュメント
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 設計図、レイヤ構造、起動シーケンス
- [`OPS.md`](OPS.md) — 運用手順、トラブルシューティング、バックアップ・復元
- [`CLIENT_GUIDE.md`](CLIENT_GUIDE.md) — クライアントサーバー運営者向け簡易マニュアル
- [`deploy/ADD_NEW_INSTANCE.md`](deploy/ADD_NEW_INSTANCE.md) — 2台目以降の Bot を立てる手順
