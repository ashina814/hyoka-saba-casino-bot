# 同じVPS上で「もう1個」Botを並走させる手順

既存の `hyoka-casino-bot`(評価鯖向け) と並行して、別サーバー向けに
**もう1個 別Bot トークン・別DB・別systemd ユニット** で同コードを動かす手順。
コードは共通(`git pull` 1回で両方に最終版が反映される)。

このガイドでは新しいインスタンス名を `casino-otherserver`(任意の英字)
と仮置きします。実際には依頼元サーバー名にちなんだものに変えてください。

---

## A. クライアント側にお願いすること(あなたじゃないとできない)

### 1. Discord Developer Portal で **新Bot Application** を作成

1. https://discord.com/developers/applications → **New Application**
2. 名前を決める(例: `casino-otherserver`)
3. 左メニュー **Bot** → **Add Bot** → **Reset Token** で `DISCORD_TOKEN` を取得して控える
4. 同 Bot 設定で **Privileged Gateway Intents は全部 OFF のまま**(このBotは不要)
5. 左メニュー **OAuth2 → URL Generator**:
   - SCOPES: `bot`, `applications.commands`
   - BOT PERMISSIONS: `Send Messages`, `Embed Links`, `Use Slash Commands`, `Read Message History`
   - 生成された URL を相手に渡してサーバーに招待してもらう
6. クライアントサーバーのオーナーから **管理者の Discord ユーザーID** を聞く

### 2. 「お釈迦さま」アカウントを決めてもらう

相手サーバーで使う第一通貨を焼却受取するアカウント(運営アカウントで可)を決めて、
その **Discord ユーザーID** を控える。後で管理パネルから設定します。

### 3. 承認用チャンネルを作ってもらう

両替申請のログ用に **#両替承認ログ** のような運営専用チャンネルを1つ用意。

---

## B. VPS 側のセットアップ(全部 SSH で)

### 1. clone(別ディレクトリ名で)

```bash
cd ~
git clone https://github.com/ashina814/hyoka-saba-casino-bot.git casino-otherserver
cd casino-otherserver
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2. .env を作る(**別トークン・別DB・別通貨名**)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

最低でも次の値を **このインスタンス専用** のものに変える:

```env
DISCORD_TOKEN=<クライアント向け新Botのトークン>
DEV_GUILD_ID=<クライアントのテストサーバーIDを入れると即時反映>
ADMIN_IDS=<あなたのID>,<クライアント管理者のID>

# DBファイルは絶対に既存と別名にする(同じだと残高混ざる)
DB_PATH=casino-otherserver.db

# 通貨名はサーバーごとに違うはずなので変える(クライアントの第一通貨に合わせる)
CURRENCY_NAME=コイン
CURRENCY_EMOJI=🪙

# 有効ゲーム(PVE のみ運用の例)
ENABLED_GAMES=slot,chinchiro,hilo,blackjack
```

> **要点**: `DISCORD_TOKEN` と `DB_PATH` だけは絶対に既存と被らせない。
> 被ると「Botが起動しない」「残高が混ざる」事故になります。

### 3. 起動テスト(まずフォアグラウンドで)

```bash
.venv/bin/python bot.py
```

`ログイン: <Bot名>` が出ればOK。`Ctrl+C` で止める。

エラーが出たら止まる前のログを確認。多いのは:
- `DISCORD_TOKEN が設定されていません` → .env のトークン入れ忘れ
- `Privileged intents` 系 → このコードは特権インテント不要なので普通は出ない

### 4. systemd ユニット作成

```bash
sudo tee /etc/systemd/system/casino-otherserver.service > /dev/null <<'EOF'
[Unit]
Description=Casino Bot (other server instance)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kabu
WorkingDirectory=/home/kabu/casino-otherserver
ExecStart=/home/kabu/casino-otherserver/.venv/bin/python /home/kabu/casino-otherserver/bot.py
EnvironmentFile=/home/kabu/casino-otherserver/.env
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now casino-otherserver
sudo systemctl status casino-otherserver --no-pager
```

`Active: active (running)` が出れば稼働中。

ライブログ:
```bash
journalctl -u casino-otherserver -f
```

### 5. Discord での初回セットアップ(クライアントサーバー上で)

招待したBotがオンラインになったら、クライアントサーバーで管理者として:

1. **#両替承認ログ** チャンネルに入る
2. `/管理パネル` → **「📥 承認CHをここに設定」**
3. 続けて **「🔥 お釈迦さま設定」** → Discord ユーザーIDをモーダル入力
4. `/カジノ` がパネルを出せば動作OK

---

## C. 今後の更新

コード修正(GitHub に push) のあと:

```bash
# 両方のインスタンスに反映
cd ~/hyoka-saba-casino-bot && git pull && sudo systemctl restart hyoka-casino-bot
cd ~/casino-otherserver  && git pull && sudo systemctl restart casino-otherserver
```

ゲームロジックだけの変更なら、管理者として Discord で各インスタンスごとに:
```
/管理 リロード コグ:slot
```
で無停止反映も可能。

---

## D. インスタンス追加時のチェックリスト

| 項目 | 別にする | 理由 |
|---|---|---|
| ディレクトリ名 | ✅ | 衝突回避 |
| DISCORD_TOKEN | ✅ | 1トークン=1Bot、共用不可 |
| DB_PATH | ✅ | 残高混ざる事故防止 |
| systemd ユニット名 | ✅ | start/stop を独立に |
| CURRENCY_NAME / EMOJI | 任意 | サーバーごとに通貨名を変えたいとき |
| ENABLED_GAMES | 任意 | サーバーごとにゲームを取捨選択するとき |
| ADMIN_IDS | 任意 | クライアント管理者を追加するなら |
| 承認チャンネル / OWNER_ID | ✅ | DB に別々に保存される(初回 `/管理パネル` で設定) |

`.env` 以外で**コードに手を入れる必要はゼロ**です。同じソース、別 .env、別 systemd ユニット、別 DB ファイルだけで完全に独立した Bot として動きます。

---

## E. リソース見積もり

1インスタンス あたり概ね:
- メモリ: **130〜180 MB**
- CPU: アイドル時 < 1%、ピーク(コマンド処理時)で 数%
- ディスク: SQLite で運用1年規模でも数MB〜数十MB

現在の ConoHa 2GB / 3Core プランなら、**5〜8インスタンス**くらいまで余裕で並走できます。
