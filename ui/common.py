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
}


def tx_reason_jp(reason: str) -> str:
    return TX_REASON_LABEL.get(reason, reason)


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
