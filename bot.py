"""カジノBot エントリポイント。

- DB を開き、Cog を読み込み、スラッシュコマンドを同期して起動する。
- DEV_GUILD_ID があればそのギルドに即時同期(開発用)。無ければグローバル同期。
- 永続 View(ハブパネル等)は on_ready で再登録し、再起動後もボタンを生かす。
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import Config, load_config
from core.external_currency import make_driver
from db.dao import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("casino")

# 読み込む Cog。順序は依存に影響しない。
# ゲーム以外の常設 Cog(必ず読む)
BASE_COGS = [
    "cogs.economy_cog",
    "cogs.hub",
    "cogs.help_cog",
    "cogs.exchange",
    "cogs.stats_cog",
    "cogs.challenges",
    "cogs.admin",
]


def _resolve_cogs(cfg) -> list[str]:
    """常設 Cog + 有効なゲーム Cog の一覧を返す。"""
    from config import ALL_GAMES
    cogs = list(BASE_COGS)
    for g in ALL_GAMES:
        if cfg.is_game_enabled(g):
            cogs.append(f"cogs.{g}")
    return cogs


class CasinoBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        # 全てスラッシュ＋パネルで完結するため特権インテントは使わない。
        # (members/message_content を有効にすると Developer Portal 側の
        #  特権インテント設定が必須になる。名前表示はキャッシュで足りる。)
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix="!casino-unused!", intents=intents)
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        # 外部通貨ドライバ。未設定なら NoneDriver で全機能が現状の手動承認のまま動く。
        self.currency_driver = make_driver(cfg)
        log.info("外部通貨ドライバ: %s (auto=%s)",
                 self.currency_driver.name, self.currency_driver.auto)

    async def setup_hook(self) -> None:
        await self.db.connect()
        for ext in _resolve_cogs(self.cfg):
            try:
                await self.load_extension(ext)
                log.info("Cog 読み込み: %s", ext)
            except Exception:
                log.exception("Cog 読み込み失敗: %s", ext)

        if self.cfg.dev_guild_id:
            guild = discord.Object(id=self.cfg.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("ギルド %s に %d コマンド同期", self.cfg.dev_guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("グローバルに %d コマンド同期(反映まで最大1時間)", len(synced))

    async def on_ready(self) -> None:
        log.info("ログイン: %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(
            activity=discord.Game(name="/カジノ でプレイ")
        )

    async def close(self) -> None:
        try:
            await self.currency_driver.close()
        except Exception:  # noqa: BLE001
            log.exception("currency_driver.close で例外")
        await self.db.close()
        await super().close()


async def main() -> None:
    cfg = load_config()
    bot = CasinoBot(cfg)
    async with bot:
        await bot.start(cfg.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("停止しました。")
