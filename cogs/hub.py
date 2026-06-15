"""ハブパネル: `/カジノ` で全ゲームの入口ボタンを並べたパネルを設置する。

設計:
- View は永続(timeout=None + 固定 custom_id)。Bot 再起動後も
  on_ready の add_view(HubView(bot)) で押下を再ルーティングする。
- ボタンは `.env` の ENABLED_GAMES に合わせて **動的に組み立て** る。
  サーバーAでは「スロット/チンチロ/ハイロー/BJ のみ」、
  サーバーBでは「全部入り」のように、同じコードで構成だけ変えられる。
- ゲームの追加は GAME_BUTTONS と各Cogの entry() を足すだけで完結する。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common


# ───────────────────────── ボタン部品 ─────────────────────────
class _RouteButton(discord.ui.Button):
    """押されると指定 Cog の entry(or 指定メソッド)を呼ぶ汎用ボタン。"""

    def __init__(
        self, label: str, emoji: str, row: int, style: discord.ButtonStyle,
        custom_id: str, cog_name: str, method: str = "entry",
    ) -> None:
        super().__init__(
            label=label, emoji=emoji, row=row, style=style, custom_id=custom_id
        )
        self._cog_name = cog_name
        self._method = method

    async def callback(self, interaction: discord.Interaction) -> None:
        # ゲーム/両替/チャレンジ等のボタンは全てここを通る。
        # メンテモードはここで一括ブロック(管理機能は別経路なのでOK)。
        if await common.maintenance_guard(interaction):
            return
        bot = interaction.client
        cog = bot.get_cog(self._cog_name)
        if cog is None:
            await interaction.response.send_message(
                f"⚠️ {self._cog_name} は現在無効です。", ephemeral=True
            )
            return
        await getattr(cog, self._method)(interaction)


class _EconomyButton(discord.ui.Button):
    """残高 / デイリー / ランキングを送る、EconomyCog 専用ショートカット。"""

    def __init__(self, label: str, emoji: str, custom_id: str, action: str) -> None:
        super().__init__(
            label=label, emoji=emoji, row=2,
            style=discord.ButtonStyle.secondary, custom_id=custom_id,
        )
        self._action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("EconomyCog")
        if cog is None:
            await interaction.response.send_message(
                "経済機能が読み込まれていません。", ephemeral=True
            )
            return
        if self._action == "daily":
            e = await cog.claim_daily(interaction.user)
        elif self._action == "balance":
            e = await cog.build_balance_embed(interaction.user)
        else:  # rank
            e = await cog.build_leaderboard_embed()
        await interaction.response.send_message(embed=e, ephemeral=True)


# ───────────────────────── ゲーム定義 ─────────────────────────
# (game_key, label, emoji, cog_class_name, row, style, short_desc)
# row=0: PVE / row=1: PVP / row=2: 経済(固定) / row=3: 両替(固定)
GAME_BUTTONS: list[tuple[str, str, str, str, int, discord.ButtonStyle, str]] = [
    ("slot",      "スロット",       "🎰", "SlotCog",      0, discord.ButtonStyle.success,
     "3リールを揃えて配当。JP搭載"),
    ("chinchiro", "チンチロ",       "🎲", "ChinchiroCog", 0, discord.ButtonStyle.success,
     "親(Bot)と勝負"),
    ("hilo",      "ハイロー",       "📈", "HiloCog",      0, discord.ButtonStyle.success,
     "次のカードがHighかLowか"),
    ("blackjack", "ブラックジャック","🃏", "BlackjackCog", 0, discord.ButtonStyle.success,
     "21を狙え。対ディーラー"),
    ("chohan",    "丁半",           "🀄", "ChohanCog",    1, discord.ButtonStyle.primary,
     "丁か半か、1:1 (PVP)"),
    ("holdem",    "ホールデム",     "♠️", "HoldemCog",    1, discord.ButtonStyle.primary,
     "テキサスホールデム (PVP)"),
    ("draw",      "ドローポーカー","🎴", "DrawCog",      1, discord.ButtonStyle.primary,
     "5カードドロー (PVP)"),
]


# ───────────────────────── ハブ View (動的) ─────────────────────────
class HubView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        cfg = bot.cfg

        # ゲームボタン: enabled_games に含まれるものだけ追加
        for key, label, emoji, cog_name, row, style, _desc in GAME_BUTTONS:
            if not cfg.is_game_enabled(key):
                continue
            self.add_item(_RouteButton(
                label, emoji, row, style, f"hub:{key}", cog_name
            ))

        # 経済(常設)
        self.add_item(_EconomyButton("デイリー",   "🎁", "hub:daily",   "daily"))
        self.add_item(_EconomyButton("残高",       "💰", "hub:balance", "balance"))
        self.add_item(_EconomyButton("ランキング", "📊", "hub:rank",    "rank"))
        # ルール(常設、row=2 の残り1枠に並べる)
        self.add_item(_RouteButton(
            "ルール", "❓", 2, discord.ButtonStyle.secondary,
            "hub:rules", "HelpCog",
        ))
        # 両替・チャレンジ・統計系(row=3)
        self.add_item(_RouteButton(
            "両替", "💱", 3, discord.ButtonStyle.success,
            "hub:exchange", "ExchangeCog",
        ))
        self.add_item(_RouteButton(
            "チャレンジ", "🗓️", 3, discord.ButtonStyle.primary,
            "hub:challenges", "ChallengesCog",
        ))
        self.add_item(_RouteButton(
            "おみくじ", "🎴", 3, discord.ButtonStyle.secondary,
            "hub:omikuji", "OmikujiCog",
        ))
        self.add_item(_RouteButton(
            "大会", "🏆", 3, discord.ButtonStyle.danger,
            "hub:tournament", "TournamentCog",
        ))


async def hub_embed(bot) -> discord.Embed:
    cfg = bot.cfg
    e = common.embed(
        "🎰 カジノへようこそ",
        "下のボタンから遊べます。各ゲームの遊び方は **ルール** ボタンで確認できます。\n"
        "初回は自動で初期チップが配られます。毎日 **デイリー** を忘れずに！",
        color=common.COLOR_MAIN,
    )
    # ブースト中ならハブパネル冒頭に大きく告知
    boost = common.boost_multiplier(bot)
    if boost > 1.0:
        remain = common.boost_remaining_sec(bot)
        h, m = remain // 3600, (remain % 3600) // 60
        e.add_field(
            name=f"🚀 イベント開催中! 配当 ×{boost}",
            value=f"残り **{h}時間{m}分**。PVE 全ゲームで配当アップ中！",
            inline=False,
        )
    # 全体JP溜まり額
    try:
        gjp = await bot.db.global_jp_amount()
        if gjp > 0 and bot.db.setting("global_jp_enabled", True):
            e.add_field(
                name="🌟 全体ジャックポット",
                value=f"いま **{gjp:,}** 積み上がってます！全PVEから抽選！",
                inline=False,
            )
    except Exception:  # noqa: BLE001
        pass
    # 大会開催中
    try:
        t = await bot.db.current_tournament()
        if t:
            import time as _t
            remain = max(0, int(t["end_ts"]) - int(_t.time()))
            h, m = remain // 3600, (remain % 3600) // 60
            e.add_field(
                name=f"🏆 大会開催中: {t['name']}",
                value=f"賞金プール **{int(t['prize_pool']):,}** / "
                      f"残り **{h}時間{m}分**\n参加するだけで自動エントリー!",
                inline=False,
            )
    except Exception:  # noqa: BLE001
        pass
    # 有効ゲームだけ説明を並べる
    for key, label, emoji, _cog, _row, _style, desc in GAME_BUTTONS:
        if not cfg.is_game_enabled(key):
            continue
        e.add_field(name=f"{emoji} {label}", value=desc, inline=True)
    e.add_field(
        name="💱 両替", value="ゼニー ↔ カジノコイン(申請承認制)", inline=False
    )
    return e


class HubCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._view_registered = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._view_registered:
            self.bot.add_view(HubView(self.bot))  # 永続View再登録
            self._view_registered = True

    @app_commands.command(name="カジノ", description="カジノのメインパネルを表示")
    async def casino(self, interaction: discord.Interaction) -> None:
        if await common.maintenance_guard(interaction):
            return
        await interaction.response.send_message(
            embed=await hub_embed(self.bot), view=HubView(self.bot)
        )


async def setup(bot) -> None:
    await bot.add_cog(HubCog(bot))
