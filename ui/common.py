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
