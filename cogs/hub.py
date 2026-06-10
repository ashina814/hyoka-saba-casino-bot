"""ハブパネル: `/カジノ` で全ゲームの入口ボタンを並べたパネルを設置する。

View は永続(timeout=None・custom_id 付き)で、再起動後もボタンが動くよう
on_ready 相当(setup の add_view)で再登録する。各ボタンは対応する Cog の
入口メソッドを呼ぶだけにして、ゲームロジックは各 Cog に委ねる。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common


class HubView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _call(self, interaction: discord.Interaction, cog_name: str, method: str):
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            await interaction.response.send_message(
                f"{cog_name} が読み込まれていません。", ephemeral=True
            )
            return
        await getattr(cog, method)(interaction)

    # ── 1段目: PVE ──
    @discord.ui.button(label="スロット", emoji="🎰", row=0,
                       style=discord.ButtonStyle.success, custom_id="hub:slot")
    async def slot(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._call(interaction, "SlotCog", "entry")

    @discord.ui.button(label="チンチロ", emoji="🎲", row=0,
                       style=discord.ButtonStyle.success, custom_id="hub:chinchiro")
    async def chinchiro(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._call(interaction, "ChinchiroCog", "entry")

    # ── 2段目: PVP ──
    @discord.ui.button(label="丁半", emoji="🀄", row=1,
                       style=discord.ButtonStyle.primary, custom_id="hub:chohan")
    async def chohan(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._call(interaction, "ChohanCog", "entry")

    @discord.ui.button(label="ホールデム", emoji="🃏", row=1,
                       style=discord.ButtonStyle.primary, custom_id="hub:holdem")
    async def holdem(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._call(interaction, "HoldemCog", "entry")

    @discord.ui.button(label="ドローポーカー", emoji="🎴", row=1,
                       style=discord.ButtonStyle.primary, custom_id="hub:draw")
    async def draw(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._call(interaction, "DrawCog", "entry")

    # ── 3段目: 経済・情報 ──
    @discord.ui.button(label="デイリー", emoji="🎁", row=2,
                       style=discord.ButtonStyle.secondary, custom_id="hub:daily")
    async def daily(self, interaction: discord.Interaction, _: discord.ui.Button):
        cog = self.bot.get_cog("EconomyCog")
        e = await cog.claim_daily(interaction.user)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="残高", emoji="💰", row=2,
                       style=discord.ButtonStyle.secondary, custom_id="hub:balance")
    async def balance(self, interaction: discord.Interaction, _: discord.ui.Button):
        cog = self.bot.get_cog("EconomyCog")
        e = await cog.build_balance_embed(interaction.user)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="ランキング", emoji="📊", row=2,
                       style=discord.ButtonStyle.secondary, custom_id="hub:rank")
    async def rank(self, interaction: discord.Interaction, _: discord.ui.Button):
        cog = self.bot.get_cog("EconomyCog")
        e = await cog.build_leaderboard_embed()
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="ルール", emoji="❓", row=2,
                       style=discord.ButtonStyle.secondary, custom_id="hub:rules")
    async def rules(self, interaction: discord.Interaction, _: discord.ui.Button):
        cog = self.bot.get_cog("HelpCog")
        await cog.entry(interaction)


def hub_embed(bot) -> discord.Embed:
    e = common.embed(
        "🎰 カジノへようこそ",
        "下のボタンから遊べます。各ゲームの遊び方は **ルール** ボタンで確認できます。\n"
        "初回は自動で初期チップが配られます。毎日 **デイリー** を忘れずに！",
        color=common.COLOR_MAIN,
    )
    e.add_field(name="🎰 スロット (PVE)", value="3リールを揃えて配当。JP搭載", inline=True)
    e.add_field(name="🎲 チンチロ (PVE)", value="親(Bot)と勝負", inline=True)
    e.add_field(name="⚂ 丁半 (PVP)", value="丁か半か、1:1", inline=True)
    e.add_field(name="🃏 ホールデム (PVP)", value="テキサスホールデム", inline=True)
    e.add_field(name="🎴 ドローポーカー (PVP)", value="5カードドロー", inline=True)
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
        await interaction.response.send_message(
            embed=hub_embed(self.bot), view=HubView(self.bot)
        )


async def setup(bot) -> None:
    await bot.add_cog(HubCog(bot))
