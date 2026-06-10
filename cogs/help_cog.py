"""ルール説明。初心者向けに各ゲームの遊び方を Embed で表示する。

`/ルール` と、ハブの『ルール』ボタン(entry)から、ゲーム選択メニューで開く。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common

# 役の強さ表(ポーカー共通)
_POKER_HANDS = (
    "**役の強さ(強い順)**\n"
    "1. ロイヤルフラッシュ — 同じスートの 10-J-Q-K-A\n"
    "2. ストレートフラッシュ — 同じスートの連番5枚\n"
    "3. フォーカード — 同じ数字4枚\n"
    "4. フルハウス — スリーカード＋ワンペア\n"
    "5. フラッシュ — 同じスート5枚\n"
    "6. ストレート — 連番5枚(A-2-3-4-5 も可)\n"
    "7. スリーカード — 同じ数字3枚\n"
    "8. ツーペア — ペア2組\n"
    "9. ワンペア — 同じ数字2枚\n"
    "10. ハイカード — 役なし(一番強い札で勝負)"
)

_TERMS = (
    "**用語**\n"
    "・**チェック**: 賭けずに次へ(誰も賭けていない時のみ)\n"
    "・**コール**: 相手のベットに同額を出して続行\n"
    "・**レイズ**: ベット額を上げる\n"
    "・**フォールド**: 降りる。賭けたチップは戻らない\n"
    "・**オールイン**: 手持ち全額を賭ける\n"
    "・**ブラインド**: 最初に強制で出す賭け金(SB/BB)\n"
    "・**ポット**: 全員の賭け金の山。勝者が取る\n"
    "・**レーキ**: ポットから引かれる手数料"
)

RULES: dict[str, tuple[str, str]] = {
    "slot": (
        "🎰 スロット (PVE)",
        "ベットして3つのリールを回し、絵柄が揃うと配当！\n\n"
        "**配当(3つ揃い)**\n"
        "🍒×3=5倍 / 🍋×3=8倍 / 🔔×3=12倍 / ⭐×3=25倍 / 7️⃣×3=75倍\n"
        "🍒が2つでも 2倍。\n"
        "💎×3 で **ジャックポット**(積み上がった大金を総取り)！\n\n"
        "倍率は実際のハウスエッジ設定に合わせて自動調整されます。",
    ),
    "chinchiro": (
        "🎲 チンチロ (PVE)",
        "親(Bot)と勝負。お椀に3つのサイコロを振って役を作ります。\n\n"
        "**役(強い順)**\n"
        "・ピンゾロ(1-1-1)= 5倍\n"
        "・ゾロ目(同じ目3つ)= 3倍\n"
        "・シゴロ(4-5-6)= 2倍\n"
        "・目(2つ同じ＋1つ)= その目で勝負、1倍\n"
        "・ヒフミ(1-2-3)= 2倍払いの大負け\n"
        "・目なし = 振り直し(最大3回)\n\n"
        "役が強い方の勝ち。同じ『目』なら数字が大きい方が勝ち。",
    ),
    "chohan": (
        "⚂ 丁半 (PVP)",
        "サイコロ2つの合計が **偶数=丁**、**奇数=半**。\n\n"
        "・丁か半のどちらかに同額ベットして参加。\n"
        "・主催者が締め切ると勝負。当てた側が、外した側の賭け金を山分け。\n"
        "・片側に誰もいなければ不成立で全額返金。\n"
        "・配当からレーキ(手数料)が引かれます。",
    ),
    "holdem": (
        "🃏 テキサスホールデム (PVP)",
        "各自2枚の手札＋場の共有5枚で最強の5枚役を作ります。\n\n"
        "**流れ**\n"
        "1. 参加費=スタックを持ち込み、ブラインドを支払う\n"
        "2. プリフロップ → フロップ(3枚) → ターン(1枚) → リバー(1枚)\n"
        "3. 各段階でベット(チェック/コール/レイズ/フォールド/オールイン)\n"
        "4. 最後まで残った人で役比べ、ポットを獲得\n\n"
        f"{_POKER_HANDS}\n\n{_TERMS}",
    ),
    "draw": (
        "🎴 5カードドロー (PVP)",
        "最初に5枚配られ、不要な札を1回だけ交換して役を作ります。\n\n"
        "**流れ**\n"
        "1. 参加費を払って参加\n"
        "2. 配札 → いらない札を選んで交換\n"
        "3. 全員終わったら役比べ、最強がポット獲得(同点は山分け)\n\n"
        f"{_POKER_HANDS}",
    ),
}

_LABELS = {
    "slot": ("スロット", "🎰"),
    "chinchiro": ("チンチロ", "🎲"),
    "chohan": ("丁半", "🀄"),
    "holdem": ("ホールデム", "🃏"),
    "draw": ("ドローポーカー", "🎴"),
}


def rule_embed(game: str) -> discord.Embed:
    title, body = RULES[game]
    return common.embed(title, body, color=common.COLOR_INFO)


class RuleSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=_LABELS[g][0], value=g, emoji=_LABELS[g][1])
            for g in RULES
        ]
        super().__init__(placeholder="ルールを見たいゲームを選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=rule_embed(self.values[0]), view=self.view
        )


class RuleView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(RuleSelect())


class HelpCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def entry(self, interaction: discord.Interaction) -> None:
        e = common.embed(
            "❓ ルール",
            "下のメニューからゲームを選ぶと遊び方が表示されます。",
            color=common.COLOR_INFO,
        )
        await interaction.response.send_message(embed=e, view=RuleView(), ephemeral=True)

    @app_commands.command(name="ルール", description="各ゲームの遊び方を表示")
    @app_commands.describe(ゲーム="特定のゲームを直接指定(省略でメニュー)")
    @app_commands.choices(
        ゲーム=[
            app_commands.Choice(name=_LABELS[g][0], value=g) for g in RULES
        ]
    )
    async def rules(
        self,
        interaction: discord.Interaction,
        ゲーム: app_commands.Choice[str] | None = None,
    ) -> None:
        if ゲーム is None:
            await self.entry(interaction)
            return
        await interaction.response.send_message(
            embed=rule_embed(ゲーム.value), view=RuleView(), ephemeral=True
        )


async def setup(bot) -> None:
    await bot.add_cog(HelpCog(bot))
