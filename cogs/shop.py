"""🛒 ショップ + 🎁 ガチャ。

設計:
- ショップは「コレクション系の称号購入」中心。1人1個、再購入不可。
  チップを使う場所を作って、インフレを抑える(=シンク)。
- ガチャは1回 N チップで、レアリティ別の称号/アイテムを引く。
  ダブり許容で count を増やしていく(コレクション要素)。
- どちらも `settings.shop_enabled` / `gacha_enabled` で運営から ON/OFF 可。
- ショップ商品とガチャアイテムはコード内 dict 定義(将来 DB化も可能)。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import discord
from discord.ext import commands

from ui import common

_RNG = secrets.SystemRandom()


# ───────────────────────── 商品定義 ─────────────────────────
@dataclass(frozen=True)
class ShopItem:
    """旧コード固定構造体。新規ロジックは DB の Row を直接使い、
    この dataclass は後方互換の View 側で使われるのみ。"""
    id: str
    label: str
    emoji: str
    price: int
    description: str


# DB 未初期化時の初期商品(冪等シード)。/管理 から後で編集可能。
DEFAULT_SHOP_ITEMS: list[dict] = [
    {"id": "title_rookie",     "label": "新人ハンター",    "emoji": "🔰", "price": 2000,
     "description": "カジノに来たての証"},
    {"id": "title_gambler",    "label": "ギャンブラー",    "emoji": "🎲", "price": 5000,
     "description": "それなりの修羅場をくぐった"},
    {"id": "title_night_owl",  "label": "夜の住人",       "emoji": "🦉", "price": 8000,
     "description": "深夜カジノの常連"},
    {"id": "title_lucky_star", "label": "幸運の星",       "emoji": "⭐", "price": 10000,
     "description": "運だけは誰にも負けない"},
    {"id": "title_card_master","label": "カードマスター",  "emoji": "🃏", "price": 15000,
     "description": "ポーカー/BJ系の達人"},
    {"id": "title_highroller", "label": "ハイローラー",    "emoji": "💎", "price": 20000,
     "description": "大金を躊躇なく賭ける度胸の称号"},
    {"id": "title_legend",     "label": "伝説",          "emoji": "👑", "price": 2000000,
     "description": "もはや畏怖の対象"},
]


def _row_to_item(row) -> ShopItem:
    return ShopItem(
        id=row["id"], label=row["label"], emoji=row["emoji"],
        price=int(row["price"]), description=row["description"],
    )


# ───────────────────────── ガチャアイテム定義 ─────────────────────────
@dataclass(frozen=True)
class GachaItem:
    id: str
    label: str
    emoji: str
    rarity: str
    weight: int


# 重み合計で確率が決まる。レアほど weight 小さい。
GACHA_ITEMS: list[GachaItem] = [
    # コモン
    GachaItem("g_chip",       "ハズレ(チップ)",  "🪙", "C",  500),
    GachaItem("g_clover",     "三つ葉",          "🍀", "C",  300),
    GachaItem("g_dice",       "サイコロ",        "🎲", "C",  200),
    # アンコモン
    GachaItem("g_diamond",    "ダイヤ",          "💎", "U",  150),
    GachaItem("g_seven",      "セブン",          "7️⃣", "U",  100),
    GachaItem("g_star",       "スター",          "⭐", "U",   80),
    # レア
    GachaItem("g_crown",      "クラウン",        "👑", "R",   40),
    GachaItem("g_phoenix",    "鳳凰",            "🦅", "R",   20),
    # SSR
    GachaItem("g_unicorn",    "ユニコーン",      "🦄", "SSR", 8),
    GachaItem("g_dragon",     "ドラゴン",        "🐉", "SSR", 2),
]
GACHA_BY_ID = {it.id: it for it in GACHA_ITEMS}
_TOTAL_WEIGHT = sum(it.weight for it in GACHA_ITEMS)

RARITY_COLOR = {
    "C":   common.COLOR_INFO,
    "U":   common.COLOR_WIN,
    "R":   common.COLOR_JACKPOT,
    "SSR": common.COLOR_JACKPOT,
}
RARITY_LABEL = {"C": "コモン", "U": "アンコモン", "R": "レア", "SSR": "SSR"}


def _gacha_roll() -> GachaItem:
    r = _RNG.randint(1, _TOTAL_WEIGHT)
    acc = 0
    for it in GACHA_ITEMS:
        acc += it.weight
        if r <= acc:
            return it
    return GACHA_ITEMS[-1]


def _probability(it: GachaItem) -> float:
    return it.weight / _TOTAL_WEIGHT * 100


# ───────────────────────── ショップ View ─────────────────────────
class ShopView(discord.ui.View):
    def __init__(
        self, cog: "ShopCog", user_id: int, owned: set[str],
        items: list[ShopItem],
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        # 未所有のものだけ「購入」ボタンを並べる(最大10、Discord制限内に収める)
        candidates = [it for it in items if it.id not in owned][:10]
        for it in candidates:
            self.add_item(self._BuyButton(it))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のショップは操作できません。", ephemeral=True
            )
            return False
        return True

    class _BuyButton(discord.ui.Button):
        def __init__(self, item: ShopItem) -> None:
            super().__init__(
                label=f"{item.label} {item.price:,}",
                emoji=item.emoji, style=discord.ButtonStyle.success,
            )
            self.item = item

        async def callback(self, interaction: discord.Interaction) -> None:
            view: ShopView = self.view  # type: ignore[assignment]
            await view.cog.handle_buy(interaction, view, self.item)


# ───────────────────────── ガチャ View ─────────────────────────
class GachaView(discord.ui.View):
    def __init__(self, cog: "ShopCog", user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のガチャは操作できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🎁 1回引く", style=discord.ButtonStyle.success)
    async def pull_one(self, interaction: discord.Interaction,
                       _: discord.ui.Button):
        await self.cog.handle_pull(interaction, self, times=1)

    @discord.ui.button(label="🎁🎁 10連", style=discord.ButtonStyle.primary)
    async def pull_ten(self, interaction: discord.Interaction,
                       _: discord.ui.Button):
        await self.cog.handle_pull(interaction, self, times=10)

    @discord.ui.button(label="📦 コレクション", style=discord.ButtonStyle.secondary)
    async def inventory(self, interaction: discord.Interaction,
                        _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.inventory_embed(interaction.user.id),
            ephemeral=True,
        )


# ───────────────────────── Cog ─────────────────────────
class ShopCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """起動時に初期商品を冪等シード(既存DBは触らない)。"""
        try:
            inserted = await self.bot.db.seed_shop_items(DEFAULT_SHOP_ITEMS)
            if inserted:
                import logging
                logging.getLogger("casino.shop").info(
                    "ショップ初期商品を %d 件投入", inserted
                )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("casino.shop").exception("ショップ初期化失敗")

    async def _load_items(self) -> list[ShopItem]:
        rows = await self.bot.db.list_shop_items(only_enabled=True)
        return [_row_to_item(r) for r in rows]

    # ── ショップ ──
    async def shop_entry(self, interaction: discord.Interaction) -> None:
        if not self.bot.db.setting("shop_enabled", True):
            await interaction.response.send_message(
                "🛑 ショップは現在停止中です。", ephemeral=True
            )
            return
        bal = await self.bot.db.get_balance(interaction.user.id)
        owned = await self.bot.db.shop_owned(interaction.user.id)
        items = await self._load_items()
        await interaction.response.send_message(
            embed=self._shop_embed(bal, owned, items),
            view=ShopView(self, interaction.user.id, owned, items),
            ephemeral=True,
        )

    def _shop_embed(
        self, balance: int, owned: set[str], items: list[ShopItem],
    ) -> discord.Embed:
        e = common.embed(
            "🛒 ショップ",
            f"現在残高: **{balance:,}**\n"
            "称号を購入してプロフィールを華やかに(1人1個、再購入不可)。\n"
            "**チップを使う場所=シンク**として、長く遊ぶための機能です。",
            color=common.COLOR_MAIN,
        )
        owned_lines = []
        for it in items:
            if it.id in owned:
                owned_lines.append(f"✅ {it.emoji} {it.label}")
            else:
                owned_lines.append(
                    f"▫️ {it.emoji} **{it.label}** ({it.price:,}) — _{it.description}_"
                )
        if not owned_lines:
            owned_lines = ["(商品が登録されていません)"]
        e.add_field(name="商品", value="\n".join(owned_lines), inline=False)
        e.set_footer(text="購入は下のボタンから。所有済みは表示されません。")
        return e

    async def handle_buy(
        self, interaction: discord.Interaction, view: ShopView, item: ShopItem
    ) -> None:
        if not self.bot.db.setting("shop_enabled", True):
            await interaction.response.send_message(
                "🛑 ショップは現在停止中です。", ephemeral=True
            )
            return
        db = self.bot.db
        user_id = interaction.user.id
        # 取引前に DB で「販売中」「価格が変わっていないか」を再確認
        latest = await db.get_shop_item(item.id)
        if latest is None or not int(latest["enabled"]):
            await interaction.response.send_message(
                "⚠️ この商品はもう販売されていません。", ephemeral=True
            )
            return
        price = int(latest["price"])
        owned = await db.shop_owned(user_id)
        if item.id in owned:
            await interaction.response.send_message(
                "既に所有しています。", ephemeral=True
            )
            return
        async with db.user_lock(user_id):
            try:
                await db.adjust_balance(user_id, -price, "shop_buy")
            except Exception:  # InsufficientFunds 等
                await interaction.response.send_message(
                    f"残高が足りません(必要: {price:,})。", ephemeral=True
                )
                return
            await db.shop_buy(user_id, item.id, price)
        # 称号としても付与(プロフィールの🏅称号タブに反映)
        await db.award_badge(user_id, f"shop_{item.id}")
        await interaction.response.send_message(
            embed=common.embed(
                f"🎉 {item.emoji} {item.label} を購入！",
                f"**{price:,}** を支払いました。\n"
                "プロフィールの 🏅称号 タブで表示されます。",
                color=common.COLOR_WIN,
            ),
            ephemeral=True,
        )
        # ショップパネルも更新
        new_owned = await db.shop_owned(user_id)
        bal = await db.get_balance(user_id)
        items = await self._load_items()
        try:
            await interaction.message.edit(  # type: ignore[union-attr]
                embed=self._shop_embed(bal, new_owned, items),
                view=ShopView(self, user_id, new_owned, items),
            )
        except (discord.HTTPException, AttributeError):
            pass

    # ── ガチャ ──
    async def gacha_entry(self, interaction: discord.Interaction) -> None:
        if not self.bot.db.setting("gacha_enabled", True):
            await interaction.response.send_message(
                "🛑 ガチャは現在停止中です。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=self._gacha_embed(),
            view=GachaView(self, interaction.user.id),
            ephemeral=True,
        )

    def _gacha_embed(self) -> discord.Embed:
        price = int(self.bot.db.setting("gacha_price", 500))
        e = common.embed(
            "🎁 ガチャ",
            f"1回 **{price:,}** チップ。10連は10倍。\n"
            "引いたアイテムはコレクションされます(ダブり可)。",
            color=common.COLOR_JACKPOT,
        )
        # 確率表示(レアリティ別)
        by_rarity: dict[str, list[GachaItem]] = {}
        for it in GACHA_ITEMS:
            by_rarity.setdefault(it.rarity, []).append(it)
        for rarity in ("SSR", "R", "U", "C"):
            items = by_rarity.get(rarity, [])
            if not items:
                continue
            lines = []
            for it in items:
                lines.append(f"{it.emoji} {it.label}  `{_probability(it):.1f}%`")
            e.add_field(
                name=f"【{RARITY_LABEL[rarity]}】",
                value="\n".join(lines),
                inline=False,
            )
        return e

    async def handle_pull(
        self, interaction: discord.Interaction, view: GachaView, times: int
    ) -> None:
        if not self.bot.db.setting("gacha_enabled", True):
            await interaction.response.send_message(
                "🛑 ガチャは現在停止中です。", ephemeral=True
            )
            return
        db = self.bot.db
        user_id = interaction.user.id
        price = int(db.setting("gacha_price", 500))
        cost = price * times
        async with db.user_lock(user_id):
            try:
                await db.adjust_balance(user_id, -cost, "gacha_pull")
            except Exception:
                await interaction.response.send_message(
                    f"残高が足りません(必要: {cost:,})。", ephemeral=True
                )
                return
        # 抽選
        results = [_gacha_roll() for _ in range(times)]
        for it in results:
            await db.gacha_add(user_id, it.id)
        # 結果表示。最高レアリティで色決定
        rank = {"C": 0, "U": 1, "R": 2, "SSR": 3}
        best = max(results, key=lambda x: rank[x.rarity])
        color = RARITY_COLOR[best.rarity]
        title = (f"🎉 1連結果！" if times == 1
                 else f"🎉 10連結果！")
        e = common.embed(title, f"消費: **{cost:,}**", color=color)
        # SSRが出たら派手にお喋りログへ
        ssr_items = [it for it in results if it.rarity == "SSR"]
        if ssr_items:
            for it in ssr_items:
                announce = common.embed(
                    f"✨ SSR ガチャ大当たり! {it.emoji} {it.label}",
                    f"<@{user_id}> が **{it.label}** を引き当て！",
                    color=common.COLOR_JACKPOT,
                )
                await common.post_casino_log(self.bot, embed=announce)
        # 詳細
        if times == 1:
            it = results[0]
            e.add_field(
                name=f"{it.emoji} {it.label}",
                value=f"レアリティ: **{RARITY_LABEL[it.rarity]}**",
                inline=False,
            )
        else:
            # 集計
            counts: dict[str, int] = {}
            for it in results:
                counts[it.id] = counts.get(it.id, 0) + 1
            lines = []
            for it_id, c in sorted(
                counts.items(),
                key=lambda kv: rank[GACHA_BY_ID[kv[0]].rarity],
                reverse=True,
            ):
                it = GACHA_BY_ID[it_id]
                lines.append(f"{it.emoji} {it.label} ({RARITY_LABEL[it.rarity]}) ×{c}")
            e.add_field(name="獲得", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def inventory_embed(self, user_id: int) -> discord.Embed:
        rows = await self.bot.db.gacha_inventory(user_id)
        e = common.embed(
            "📦 ガチャコレクション",
            f"獲得種類: **{len(rows)} / {len(GACHA_ITEMS)}**",
            color=common.COLOR_INFO,
        )
        if not rows:
            e.description = "まだ何も引いていません。"
            return e
        by_rarity: dict[str, list[str]] = {}
        for r in rows:
            it = GACHA_BY_ID.get(r["item_id"])
            if not it:
                continue
            by_rarity.setdefault(it.rarity, []).append(
                f"{it.emoji} {it.label} ×{int(r['count'])}"
            )
        for rarity in ("SSR", "R", "U", "C"):
            items = by_rarity.get(rarity, [])
            if items:
                e.add_field(
                    name=f"【{RARITY_LABEL[rarity]}】",
                    value="\n".join(items),
                    inline=False,
                )
        return e


async def setup(bot) -> None:
    await bot.add_cog(ShopCog(bot))
