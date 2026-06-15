"""UI 共通ヘルパー: Embed 生成・権限チェック・金額整形・ベット検証。

各 Cog / View から再利用する。色やフォーマットをここに集約して見た目を統一する。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot import CasinoBot

# 配色(統一感のため)
COLOR_MAIN = 0xF1C40F     # 金 — ハブ/通常
COLOR_WIN = 0x2ECC71      # 緑 — 勝ち
COLOR_LOSE = 0xE74C3C     # 赤 — 負け
COLOR_INFO = 0x3498DB     # 青 — 情報/ルール
COLOR_JACKPOT = 0xE91E63  # 桃 — ジャックポット
COLOR_ADMIN = 0x9B59B6    # 紫 — 管理


def money(cfg, amount: int) -> str:
    """123456 → '🪙 123,456 チップ' のような表示。"""
    return f"{cfg.currency_emoji} {amount:,} {cfg.currency_name}".strip()


def is_admin(bot: "CasinoBot", user: discord.abc.User) -> bool:
    return user.id in bot.cfg.admin_ids


def embed(title: str, desc: str = "", color: int = COLOR_MAIN) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


def parse_bet(raw: str) -> int:
    """ベット入力をパース。'1k'→1000, '1.5万'→15000, カンマ可。失敗で ValueError。"""
    s = raw.strip().replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("金額が空です。")
    mult = 1
    for suffix, m in (("万", 10000), ("k", 1000), ("K", 1000), ("m", 1_000_000)):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            mult = m
            break
    value = int(round(float(s) * mult))
    if value <= 0:
        raise ValueError("金額は正の数で入力してください。")
    return value


def validate_bet(bot: "CasinoBot", bet: int) -> str | None:
    """ベット額が min/max 範囲内か。問題なければ None、あればエラー文。"""
    lo = int(bot.db.setting("min_bet", 10))
    hi = int(bot.db.setting("max_bet", 100000))
    if bet < lo:
        return f"最低ベットは {lo:,} です。"
    if bet > hi:
        return f"最高ベットは {hi:,} です。"
    return None


# 取引ログ表示用の日本語ラベル。DB上の reason 文字列(英語)は互換性のため
# 変更せず、表示時のみここでマッピングする。未知の reason はそのまま英語表示。
TX_REASON_LABEL = {
    "initial_grant": "初期付与",
    "daily": "デイリー",
    "transfer_in": "送金受取", "transfer_out": "送金",
    "slot_bet": "スロット 賭",   "slot_win": "スロット 払戻",
    "slot_jackpot": "スロット JP獲得",
    "chinchiro_bet": "チンチロ 賭", "chinchiro_win": "チンチロ 払戻",
    "hilo_bet": "ハイロー 賭",   "hilo_win": "ハイロー 払戻",
    "blackjack_bet": "BJ 賭",     "blackjack_win": "BJ 払戻",
    "blackjack_double": "BJ ダブル", "blackjack_split": "BJ スプリット",
    "pvp_escrow": "PVP 預け",    "pvp_win": "PVP 払戻",
    "pvp_refund": "PVP 返金",
    "exchange_in": "両替 受取",  "exchange_escrow": "両替 預け",
    "exchange_refund": "両替 返金",
    "holding_tax": "保有税",
    "admin_give": "管理:付与",   "admin_take": "管理:没収",
    "admin_set": "管理:セット",
    "admin_undo": "管理:取消",
    "bug_compensation_daily": "バグ補填(daily)",
    "challenge_reward": "チャレンジ報酬",
    "omikuji_bonus": "おみくじボーナス",
    "global_jp_win": "🌟 全体JP獲得",
    "tournament_prize": "🏆 大会賞金",
}


def tx_reason_jp(reason: str) -> str:
    return TX_REASON_LABEL.get(reason, reason)


async def post_casino_log(bot, embed: discord.Embed | None = None,
                          content: str | None = None) -> None:
    """お喋りログチャンネルへ投稿。未設定なら何もしない。

    JP当選、連勝達成、ブースト開始、その他「盛り上げる出来事」を Bot が代弁する。
    `casino_log_channel_id` が運営用の `exchange_log_channel_id` と別なのは、
    プレイヤー向けに公開して見てもらうためのチャンネル想定だから(運営チャンネルとは別)。
    """
    ch_id = int(bot.db.setting("casino_log_channel_id", 0) or 0)
    if not ch_id:
        return
    ch = bot.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except (discord.NotFound, discord.Forbidden):
            return
    try:
        await ch.send(content=content, embed=embed)
    except discord.HTTPException:
        pass


async def dm_user(bot, user_id: int, embed: discord.Embed) -> bool:
    """ユーザーへDM。失敗(DM拒否設定など)しても True/False を返すだけで例外は呑む。"""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(embed=embed)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


def boost_multiplier(bot) -> float:
    """有効中ブーストの倍率を返す。無効/期限切れなら 1.0。

    PVE 各ゲームの払戻計算で `payout *= boost_multiplier(bot)` する。
    """
    import time as _t
    until = int(bot.db.setting("boost_until_ts", 0) or 0)
    if until <= 0 or _t.time() >= until:
        return 1.0
    return float(bot.db.setting("boost_multiplier", 1.0) or 1.0)


def boost_remaining_sec(bot) -> int:
    """ブースト残り秒数(0=ブースト無効)。"""
    import time as _t
    until = int(bot.db.setting("boost_until_ts", 0) or 0)
    return max(0, until - int(_t.time()))


async def self_limit_guard(
    interaction: discord.Interaction, bet: int
) -> bool:
    """ユーザーの自己設定上限を超えるベットを弾く。

    超過なら True を返してエラーメッセージを送る(各ゲームの _start から呼ぶ)。
    上限0は無制限扱い。
    """
    bot = interaction.client
    lim = await bot.db.get_user_limit(interaction.user.id)
    cap = int(lim.get("daily_bet_cap", 0) or 0)
    if cap <= 0:
        return False
    today = await bot.db.daily_bet_total(interaction.user.id)
    if today + bet > cap:
        remain = max(0, cap - today)
        e = embed(
            "🛡️ 自己制限に到達",
            f"今日の累計ベット **{today:,}** / 自己上限 **{cap:,}**\n"
            f"残り可能ベットは **{remain:,}** です。\n"
            "上限変更は /プロフィール → 🛡️制限 から(24時間クールダウンあり)。",
            color=COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
        return True
    return False


async def maintenance_guard(interaction: discord.Interaction) -> bool:
    """メンテモード中で管理者以外なら、エラーメッセージを送って True を返す。

    各ゲームの entry の最初に `if await common.maintenance_guard(interaction): return`
    で挿す。管理者(`ADMIN_IDS`)はメンテ中でも自由に触れる。
    """
    bot = interaction.client
    if not bot.db.setting("maintenance_mode", False):
        return False
    if is_admin(bot, interaction.user):
        return False
    e = embed(
        "🛠️ メンテナンス中",
        "現在カジノは一時停止しています。再開までしばらくお待ちください。",
        color=COLOR_INFO,
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)
    return True


async def report_error(bot, where: str, exc: BaseException) -> None:
    """例外を運営DMに送る共通ヘルパー。

    View や Modal の callback 内で `try/except` した時に呼ぶ。
    """
    import traceback as _tb
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    try:
        await bot.notify_admins(f"🚨 {where}", tb)
    except Exception:  # noqa: BLE001
        pass


async def respond_with(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
) -> discord.Message | None:
    """response 済みなら followup、未応答なら response.send_message で送る。

    PVE ゲームの `_start` を、初回(モーダル経由)と「もう一回」(既に response 済み)
    双方から呼べるようにするためのヘルパー。送信したメッセージを返す
    (followup の場合は Message、response.send_message の場合は None)。
    """
    kwargs: dict = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if ephemeral:
        kwargs["ephemeral"] = True
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    await interaction.response.send_message(**kwargs)
    return None


class PlayAgainView(discord.ui.View):
    """PVE 結果画面に貼る『もう一回(同額) / ベット変更』ボタン。

    on_start_cb は async (interaction, bet) を受ける、各ゲーム Cog の `_start`。
    既に response 済みの interaction が渡るので、_start 側は respond_with() を
    使ってフォロワー送信に対応していること。
    """

    def __init__(self, bot, user_id: int, last_bet: int, on_start_cb) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.last_bet = last_bet
        self._cb = on_start_cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のセッションは操作できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="もう一回(同額)", emoji="🔁",
                       style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, _: discord.ui.Button):
        # 自分のボタンを無効化(結果メッセージを編集)してから、次ゲームを開始
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self._cb(interaction, self.last_bet)
        self.stop()

    @discord.ui.button(label="ベット変更", emoji="✏️",
                       style=discord.ButtonStyle.primary)
    async def change(self, interaction: discord.Interaction, _: discord.ui.Button):
        # モーダルを開く。送信後 _cb が呼ばれる。
        await interaction.response.send_modal(
            BetModal(self.bot, "ベット変更", self._cb)
        )
        # 古い結果メッセージのボタンは timeout で自動的に無効化される

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class BetPresetView(discord.ui.View):
    """賭け額を素早く決めるためのプリセットボタン群。

    on_pick_cb は async (interaction, bet) を受ける、各ゲーム Cog の `_start`。
    起票者(user_id)のみ操作可。120秒で自動失効。
    HALF/MAX は現在残高に対する半分/全額、+100/+1k/+1万は固定額のプリセット。
    """

    def __init__(self, bot, user_id: int, on_pick_cb,
                 title: str = "ベット額を選択") -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self._cb = on_pick_cb
        self.title = title

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のセッションは操作できません。", ephemeral=True
            )
            return False
        return True

    async def _pick(self, interaction: discord.Interaction, bet: int) -> None:
        err = validate_bet(self.bot, bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        await self._cb(interaction, bet)
        self.stop()

    @discord.ui.button(label="+100", row=0, style=discord.ButtonStyle.secondary)
    async def b100(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._pick(interaction, 100)

    @discord.ui.button(label="+1,000", row=0, style=discord.ButtonStyle.secondary)
    async def b1k(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._pick(interaction, 1000)

    @discord.ui.button(label="+10,000", row=0, style=discord.ButtonStyle.secondary)
    async def b10k(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._pick(interaction, 10000)

    @discord.ui.button(label="HALF(残高の半分)", emoji="⚖️",
                       row=1, style=discord.ButtonStyle.primary)
    async def half(self, interaction: discord.Interaction, _: discord.ui.Button):
        bal = await self.bot.db.get_balance(interaction.user.id)
        await self._pick(interaction, max(1, bal // 2))

    @discord.ui.button(label="MAX(残高全部)", emoji="💯",
                       row=1, style=discord.ButtonStyle.danger)
    async def maxbet(self, interaction: discord.Interaction, _: discord.ui.Button):
        bal = await self.bot.db.get_balance(interaction.user.id)
        if bal <= 0:
            await interaction.response.send_message(
                "残高が0です。デイリーから始めましょう。", ephemeral=True
            )
            return
        # 最大ベットに丸める
        hi = int(self.bot.db.setting("max_bet", 100000))
        await self._pick(interaction, min(bal, hi))

    @discord.ui.button(label="金額を入力", emoji="✏️",
                       row=1, style=discord.ButtonStyle.success)
    async def custom(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.bot, self.title, self._cb))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


async def send_bet_panel(
    interaction: discord.Interaction, bot, on_pick_cb, *, title: str
) -> None:
    """各ゲームの `entry` から呼ぶ共通エントリ。ベットプリセット画面を ephemeral で送る。"""
    view = BetPresetView(bot, interaction.user.id, on_pick_cb, title=title)
    bal = await bot.db.get_balance(interaction.user.id)
    lo = int(bot.db.setting("min_bet", 10))
    hi = int(bot.db.setting("max_bet", 100000))
    e = embed(
        title,
        f"クイック選択するか「✏️ 金額を入力」で自由入力。\n"
        f"現在残高: **{bal:,}**  /  範囲: {lo:,} 〜 {hi:,}",
        color=COLOR_INFO,
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)


class BetModal(discord.ui.Modal):
    """ベット額入力モーダル。送信で on_submit_cb(interaction, amount) を呼ぶ。

    パネル化方針のため、各ゲームの開始はこのモーダル1枚に統一する。
    金額パース・範囲検証もここで行い、問題があればエラーを返す。
    """

    amount = discord.ui.TextInput(
        label="ベット額",
        placeholder="例: 100 / 1k / 1.5万",
        required=True,
        max_length=12,
    )

    def __init__(self, bot, title: str, on_submit_cb) -> None:
        super().__init__(title=title)
        self.bot = bot
        self._cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = parse_bet(str(self.amount.value))
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        err = validate_bet(self.bot, value)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        await self._cb(interaction, value)
