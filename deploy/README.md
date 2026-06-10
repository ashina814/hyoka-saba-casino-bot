# VPS デプロイ手順(Linux / systemd)

前提: Ubuntu/Debian 系、Python 3.11+ が入っていること。

## 1. 配置とセットアップ
```bash
sudo useradd -r -m -d /opt/casino-bot casino     # 専用ユーザー(任意)
sudo mkdir -p /opt/casino-bot
# このリポジトリを /opt/casino-bot に配置(git clone か rsync)
cd /opt/casino-bot

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 2. 環境変数
```bash
cp .env.example .env
nano .env      # DISCORD_TOKEN と ADMIN_IDS を設定。本番では DEV_GUILD_ID は空でよい
sudo chown casino:casino .env && chmod 600 .env
```

## 3. 動作確認(フォアグラウンド)
```bash
.venv/bin/python bot.py        # ログが出てログイン成功を確認したら Ctrl+C
```

## 4. systemd 常駐化
```bash
sudo cp deploy/casino-bot.service /etc/systemd/system/casino-bot.service
sudo nano /etc/systemd/system/casino-bot.service   # User/パスを環境に合わせる
sudo chown -R casino:casino /opt/casino-bot

sudo systemctl daemon-reload
sudo systemctl enable --now casino-bot
sudo systemctl status casino-bot
journalctl -u casino-bot -f      # ライブログ
```

## 5. 日次バックアップ(任意)
SQLite を WAL のまま安全にコピーするには `.backup` を使う:
```bash
# crontab -e に追記(毎日 4:00)
0 4 * * * sqlite3 /opt/casino-bot/casino.db ".backup '/opt/casino-bot/backup/casino-$(date +\%F).db'"
```

## 更新時
```bash
cd /opt/casino-bot && git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart casino-bot
```
ゲームロジックだけの修正なら、Discord 上で管理者が `/管理 リロード コグ:slot` のように
無停止で個別 Cog を再読み込みできる。

## コマンド同期について
- `DEV_GUILD_ID` を設定するとそのサーバーにだけ即時同期(開発向き)。
- 本番(グローバル同期)はコマンド反映に最大1時間かかることがある。
