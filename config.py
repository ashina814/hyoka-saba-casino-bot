"""環境変数の読み込みと、起動時の不変設定。

ゲームのチューニング値(ハウスエッジ・レーキ率・daily 額など)は
ここには置かず、DB の settings テーブルで管理し、管理パネルから変更する。
ここに置くのは「起動時に決まる・実行中に変えない」ものだけ。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


# 全ゲーム一覧(キーは Cog の suffix。bot.py 側で cogs.<key> を解決する)。
# `.env` の ENABLED_GAMES が空なら全て有効、指定があればその集合だけ有効化する。
ALL_GAMES: tuple[str, ...] = (
    "slot",
    "chinchiro",
    "chohan",
    "holdem",
    "draw",
    "hilo",
    "blackjack",
)


@dataclass(frozen=True)
class Config:
    token: str
    dev_guild_id: int | None
    admin_ids: set[int]
    db_path: str
    currency_name: str
    currency_emoji: str
    enabled_games: frozenset[str]    # 空集合の代わりに ALL_GAMES が反映済み

    @property
    def currency(self) -> str:
        """表示用: '🪙 チップ' のような結合済み文字列。"""
        return f"{self.currency_emoji} {self.currency_name}".strip()

    def is_game_enabled(self, name: str) -> bool:
        return name in self.enabled_games


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。.env を作成してトークンを記入してください。"
        )

    dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
    dev_guild_id = int(dev_guild_raw) if dev_guild_raw.isdigit() else None

    # ENABLED_GAMES: カンマ区切り(例: "slot,chinchiro,hilo,blackjack")
    # 空または未設定なら ALL_GAMES = 全有効。未知のゲーム名は黙って無視。
    raw_games = os.getenv("ENABLED_GAMES", "").strip()
    if raw_games:
        wanted = {g.strip() for g in raw_games.split(",") if g.strip()}
        enabled = frozenset(g for g in ALL_GAMES if g in wanted)
    else:
        enabled = frozenset(ALL_GAMES)

    return Config(
        token=token,
        dev_guild_id=dev_guild_id,
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS")),
        db_path=os.getenv("DB_PATH", "casino.db").strip() or "casino.db",
        currency_name=os.getenv("CURRENCY_NAME", "チップ").strip() or "チップ",
        currency_emoji=os.getenv("CURRENCY_EMOJI", "🪙").strip(),
        enabled_games=enabled,
    )
