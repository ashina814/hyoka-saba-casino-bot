# 運用ガイド

## 日常運用

### 状態確認
```bash
sudo systemctl status hyoka-casino-bot casino-otherserver --no-pager
journalctl -u hyoka-casino-bot -f          # ライブログ
```

### コード更新(GitHub → 両Bot 反映)
```bash
for d in ~/hyoka-saba-casino-bot ~/casino-otherserver; do
  cd "$d" && git pull
done
sudo systemctl restart hyoka-casino-bot casino-otherserver
```

### Bot ごと再起動
```bash
sudo systemctl restart hyoka-casino-bot
# または
sudo systemctl restart casino-otherserver
```

### 設定変更（再起動不要）
Discord で `/管理` → 💰経済 → 設定一覧 / 設定を変更

### 管理者を追加（再起動不要）
Discord で `/管理` → 🚨危険ゾーン → ➕管理者追加 (env管理者のみ可)

---

## バックアップ

- `_backup_loop` が日次で `<DBファイル名>-YYYY-MM-DD.db` を `backups/` に保存
- 保持日数: `backup_keep_days` setting (既定14日)
- 古いものは自動削除

### 手動バックアップ
```bash
# Bot を止めずに整合性ある複製を取る方法
sqlite3 ~/hyoka-saba-casino-bot/casino.db ".backup /tmp/casino-manual-$(date +%F).db"
```

### 復元
```bash
sudo systemctl stop hyoka-casino-bot
cp ~/hyoka-saba-casino-bot/backups/casino-YYYY-MM-DD.db ~/hyoka-saba-casino-bot/casino.db
sudo systemctl start hyoka-casino-bot
```

---

## トラブルシューティング

### Bot がオフライン
1. `sudo systemctl status hyoka-casino-bot` で active 確認
2. inactive なら `sudo systemctl start hyoka-casino-bot`
3. それでもダメなら `journalctl -u hyoka-casino-bot -n 80 --no-pager` で原因確認

よくある原因:
- `DISCORD_TOKEN が設定されていません` → `.env` 確認
- `PrivilegedIntentsRequired` → このコードでは出ないはずだが、出たら Developer Portal の Intents 設定確認
- `Invalid emoji` → ボタンの絵文字が不正(過去にあったバグ)

### スラッシュコマンドが見えない
- グローバル同期は反映に最大1時間
- 急ぐなら `.env` に `DEV_GUILD_ID=<テストサーバーID>` 設定 → restart で即時反映
- 招待時 `applications.commands` スコープが付いてるか確認

### 「インタラクションに失敗」と出る
- 例外で処理が落ちている可能性
- `journalctl -u <unit> -n 80` で赤い Traceback を探す
- すでに管理者DMにも例外通知が来ているはず

### 残高がおかしい
- `/管理` → 👤ユーザー操作 → 取引履歴監査 で対象ユーザーの履歴を見る
- 不正な取引があれば該当ユーザーIDで `/管理` → 残高セット(危険ゾーン、env管理者のみ)

---

## メンテナンス時の流れ

ゲームバランス調整やデプロイで一時停止したい時:

1. Discord で `/管理` → 🛠️システム → 🛠️メンテモード切替 (ON)
   - お喋りログに自動アナウンス
   - 管理者以外は `/カジノ` を叩いてもブロックされる
2. 作業 (DB操作、コード反映、Bot再起動)
3. もう一度 🛠️メンテモード切替 (OFF)
   - 再開アナウンスが自動で流れる

管理者は メンテモード中でも全機能を触れる(動作確認可能)。

---

## バランス調整の指針

`/管理` → 📊経済ダッシュボード を観察:

| 指標 | 警告レベル | 対策 |
|---|---|---|
| Gini ≥ 0.85 | 🔴 格差過大 | デイリー減衰強化、保有税ON、ショップ品増設 |
| 月次インフレ ≥ 15% | 🔴 急激なインフレ | ハウスエッジ↑、JP積立↑、賞金プール抑制 |
| アクティブ率 < 20% | 🔴 過疎 | 大会開催、ブースト発動、招待ボーナス周知 |

全部の数値は `/管理` → 💰経済 → 設定変更 から実行中に変更可能。

---

## VPS新規インスタンス追加
[`deploy/ADD_NEW_INSTANCE.md`](deploy/ADD_NEW_INSTANCE.md) 参照。

要点:
- ディレクトリ名・systemd ユニット名・DB ファイル名・Botトークンを別にする
- `ENABLED_GAMES` でそのサーバー向けゲームに絞る
- `CURRENCY_NAME` でそのサーバーの呼称に揃える

---

## セキュリティ運用

### 管理者トークン管理
- `.env` の `DISCORD_TOKEN` は **絶対にコミットしない**(`.gitignore` で除外済み)
- 万一漏れたら Discord Developer Portal で即 Reset Token、新トークンで再起動

### 管理者権限
- env 管理者 = 全権限(管理者の追加削除、残高直接セット可)
- DB 管理者 = 通常運用(残高付与/没収、設定変更、大会開催 等)
- 信頼の起点は **env管理者(=あなた)** のみ。DB管理者からは新管理者を増やせない

### 監査
- 全管理操作は `admin_logs` テーブル + 承認チャンネルへ自動投稿
- 全チップ移動は `tx_logs` テーブルに残る(削除不可)
- ユーザー個別履歴は `/管理` → 👤ユーザー操作 → 取引履歴監査

---

## クライアントサーバー引き渡し時のチェックリスト
- [ ] 新Botアプリ作成・トークン取得
- [ ] OAuth2 URLで`bot`+`applications.commands` スコープ付き招待
- [ ] VPS で新ディレクトリ clone・venv・依存導入
- [ ] `.env` に新トークン・別DB_PATH・ENABLED_GAMES・通貨名
- [ ] systemd 別ユニット作成・enable --now
- [ ] Discord で `/管理` 動くこと確認(ADMIN_IDSにあなたのIDを必ず入れる)
- [ ] クライアント運営者のIDを `/管理` → 危険ゾーン → 管理者追加 で登録
- [ ] 承認チャンネル設定、お釈迦さま設定、お喋りCH設定
- [ ] [`CLIENT_GUIDE.md`](CLIENT_GUIDE.md) を運営者に渡す
