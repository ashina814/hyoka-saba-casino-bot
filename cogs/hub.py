"""ハブパネル: `/カジノ` で主要動線をまとめたメインパネル + 「✨もっと」サブパネル。

設計:
- メインViewは永続(timeout=None + 固定 custom_id)。/カジノで設置されたパネルが
  再起動後も生き続けるよう、on_ready で add_view(HubView(bot)) 再登録する。
- メインには「頻繁に使うもの」だけ並べる:
    ゲーム(PVE/PVP、ENABLED_GAMES で動的フィルタ)
    + 経済(デイリー/ランキング/ルール)
    + 「✨もっと」サブパネル入口
- 「✨もっと」はメインを永続のまま、ephemeral で MoreView を出す:
    両替/チャレンジ/おみくじ/大会/殿堂
  ephemeral なので timeout で自然消去、戻るボタン不要。
- 残高表示はハブから外し、/プロフィール(残高/履歴/統計/称号/制限のタブ式)に統一。
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
    """デイリー / ランキングを送る、EconomyCog 専用ショートカット。"""

    def __init__(self, label: str, emoji: str, custom_id: str, action: str,
                 row: int = 2) -> None:
        super().__init__(
            label=label, emoji=emoji, row=row,
            style=discord.ButtonStyle.secondary, custom_id=custom_id,
        )
        self._action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if await common.maintenance_guard(interaction):
            return
        cog = interaction.client.get_cog("EconomyCog")
        if cog is None:
            await interaction.response.send_message(
                "経済機能が読み込まれていません。", ephemeral=True
            )
            return
        if self._action == "daily":
            e = await cog.claim_daily(interaction.user)
        else:  # rank
            e = await cog.build_leaderboard_embed()
        await interaction.response.send_message(embed=e, ephemeral=True)


class _MoreButton(discord.ui.Button):
    """『✨もっと』ボタン。押すと ephemeral で MoreView を開く。"""

    def __init__(self) -> None:
        super().__init__(
            label="もっと", emoji="✨", row=2,
            style=discord.ButtonStyle.primary, custom_id="hub:more",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await common.maintenance_guard(interaction):
            return
        await interaction.response.send_message(
            embed=common.embed(
                "✨ もっと",
                "両替 / チャレンジ / おみくじ / 大会 / 殿堂 から選んでください。",
                color=common.COLOR_INFO,
            ),
            view=MoreView(),
            ephemeral=True,
        )


# ───────────────────────── ゲーム定義 ─────────────────────────
# (game_key, label, emoji, cog_class_name, row, style, short_desc)
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


# ───────────────────────── ハブ View (動的、永続) ─────────────────────────
class HubView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        cfg = bot.cfg

        # row 0 / row 1: ゲームボタン(有効なものだけ)
        for key, label, emoji, cog_name, row, style, _desc in GAME_BUTTONS:
            if not cfg.is_game_enabled(key):
                continue
            self.add_item(_RouteButton(
                label, emoji, row, style, f"hub:{key}", cog_name
            ))

        # row 2: よく使う動線4つに圧縮
        self.add_item(_EconomyButton("デイリー", "🎁", "hub:daily", "daily"))
        self.add_item(_EconomyButton("ランキング", "📊", "hub:rank", "rank"))
        self.add_item(_RouteButton(
            "ルール", "❓", 2, discord.ButtonStyle.secondary,
            "hub:rules", "HelpCog",
        ))
        self.add_item(_MoreButton())


# ───────────────────────── 「✨ もっと」サブパネル(ephemeral) ─────────────────────────
class MoreView(discord.ui.View):
    """両替/チャレンジ/おみくじ/大会/殿堂 を集約した ephemeral サブパネル。

    timeout で自然消去するので、永続化や custom_id ベース再登録は不要。
    各ボタンは _RouteButton(custom_id付き)を使うが、ephemeral 上なので
    timeout後は反応しない(=ハブから開き直してもらう)。
    """

    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(_RouteButton(
            "両替", "💱", 0, discord.ButtonStyle.success,
            "more:exchange", "ExchangeCog",
        ))
        self.add_item(_RouteButton(
            "チャレンジ", "🗓️", 0, discord.ButtonStyle.primary,
            "more:challenges", "ChallengesCog",
        ))
        self.add_item(_RouteButton(
            "おみくじ", "🎴", 0, discord.ButtonStyle.secondary,
            "more:omikuji", "OmikujiCog",
        ))
        self.add_item(_RouteButton(
            "大会", "🏆", 1, discord.ButtonStyle.danger,
            "more:tournament", "TournamentCog",
        ))
        self.add_item(_RouteButton(
            "殿堂", "🏛️", 1, discord.ButtonStyle.secondary,
            "more:hall", "HallCog",
        ))


# ───────────────────────── Embed ─────────────────────────
async def hub_embed(bot) -> discord.Embed:
    cfg = bot.cfg
    e = common.embed(
        "🎰 カジノへようこそ",
        "下のボタンから遊べます。各ゲームの遊び方は **❓ルール** から。\n"
        "残高/履歴/称号などは **/プロフィール** で確認できます。\n"
        "**✨もっと** から両替・チャレンジ・おみくじ・大会・殿堂へ。",
        color=common.COLOR_MAIN,
    )
    # ブースト中なら冒頭に告知
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
    # 有効ゲームの短い説明
    for key, label, emoji, _cog, _row, _style, desc in GAME_BUTTONS:
        if not cfg.is_game_enabled(key):
            continue
        e.add_field(name=f"{emoji} {label}", value=desc, inline=True)
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
