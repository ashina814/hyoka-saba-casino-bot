"""5カードドローポーカー(PVP)。

進行:
- ロビーで参加(エスクロー=参加費)。2〜6人。
- 主催者が配札 → 各自5枚。
- 各プレイヤーは「手札を見る」で自分だけに手札表示、交換したい札を選んで1回交換。
- 全員交換 or 主催者がショーダウン → 役最強が勝ち、ポットを獲得(レーキ控除)。同点は山分け。
"""
from __future__ import annotations

import discord
# (app_commands removed: no slash commands here anymore)
from discord.ext import commands

from core import economy, hand, match
from core.deck import Card, Deck
from ui import common

MIN_PLAYERS, MAX_PLAYERS = 2, 6


class DrawState:
    def __init__(self, match_id: str, host_id: int, bet: int) -> None:
        self.match_id = match_id
        self.host_id = host_id
        self.bet = bet
        self.players: list[int] = []
        self.started = False
        self.deck = Deck()
        self.hands: dict[int, list[Card]] = {}
        self.exchanged: dict[int, bool] = {}

    @property
    def pot(self) -> int:
        return self.bet * len(self.players)


# ───────────────────────── 手札交換(本人のみ) ─────────────────────────
class ExchangeView(discord.ui.View):
    def __init__(self, cog: "DrawCog", st: DrawState, uid: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.st = st
        self.uid = uid
        self.discard: set[int] = set()
        for i, card in enumerate(st.hands[uid]):
            self.add_item(self._CardButton(i, card))
        self.add_item(self._Confirm())

    class _CardButton(discord.ui.Button):
        def __init__(self, idx: int, card: Card) -> None:
            super().__init__(label=str(card), style=discord.ButtonStyle.secondary, row=0)
            self.idx = idx

        async def callback(self, interaction: discord.Interaction) -> None:
            view: "ExchangeView" = self.view  # type: ignore[assignment]
            if self.idx in view.discard:
                view.discard.discard(self.idx)
                self.style = discord.ButtonStyle.secondary
            else:
                view.discard.add(self.idx)
                self.style = discord.ButtonStyle.danger
            await interaction.response.edit_message(view=view)

    class _Confirm(discord.ui.Button):
        def __init__(self) -> None:
            super().__init__(label="交換を確定", emoji="🔄",
                             style=discord.ButtonStyle.success, row=1)

        async def callback(self, interaction: discord.Interaction) -> None:
            view: "ExchangeView" = self.view  # type: ignore[assignment]
            st = view.st
            hand_cards = st.hands[view.uid]
            for i in sorted(view.discard):
                hand_cards[i] = st.deck.draw(1)[0]
            st.exchanged[view.uid] = True
            for item in view.children:
                item.disabled = True
            cards = " ".join(str(c) for c in hand_cards)
            score = hand.best_hand(hand_cards)
            e = common.embed(
                "あなたの手札(交換後)",
                f"{cards}\n\n役: **{hand.describe(score)}**",
                color=common.COLOR_INFO,
            )
            await interaction.response.edit_message(embed=e, view=view)
            await view.cog.refresh_table(st)
            view.stop()


# ───────────────────────── テーブル(全員) ─────────────────────────
class TableView(discord.ui.View):
    def __init__(self, cog: "DrawCog", st: DrawState) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.st = st

    @discord.ui.button(label="手札を見る／交換", emoji="🎴",
                       style=discord.ButtonStyle.primary)
    async def view_hand(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        uid = interaction.user.id
        if uid not in st.hands:
            await interaction.response.send_message(
                "あなたはこのゲームに参加していません。", ephemeral=True
            )
            return
        if st.exchanged.get(uid):
            cards = " ".join(str(c) for c in st.hands[uid])
            await interaction.response.send_message(
                f"交換済みです。\n{cards}", ephemeral=True
            )
            return
        cards = " ".join(str(c) for c in st.hands[uid])
        e = common.embed(
            "あなたの手札",
            f"{cards}\n\n交換したい札を選んで『交換を確定』。交換しないならそのまま確定。",
            color=common.COLOR_INFO,
        )
        await interaction.response.send_message(
            embed=e, view=ExchangeView(self.cog, st, uid), ephemeral=True
        )

    @discord.ui.button(label="ショーダウン", emoji="🏆",
                       style=discord.ButtonStyle.success)
    async def showdown(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.st.host_id:
            await interaction.response.send_message(
                "主催者のみ操作できます。", ephemeral=True
            )
            return
        # DB書き込みが複数走るので3秒制限を回避するため先に defer する
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        await self.cog.showdown(interaction, self.st)
        self.stop()


class DrawCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.matches: dict[str, DrawState] = {}
        self._messages: dict[str, discord.Message] = {}

    # ── ロビー ──
    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            common.BetModal(self.bot, "🎴 ドローポーカー — 場を立てる", self._create)
        )

    async def _create(self, interaction: discord.Interaction, bet: int) -> None:
        mid = match.new_match_id("draw")
        st = DrawState(mid, interaction.user.id, bet)
        self.matches[mid] = st
        from cogs._lobby import LobbyView  # 遅延 import で循環回避
        view = LobbyView(self, st, MIN_PLAYERS, MAX_PLAYERS, self.start_game)
        await interaction.response.send_message(
            embed=view.embed("🎴 ドローポーカー"), view=view
        )
        self._messages[mid] = await interaction.original_response()

    # ── 進行 ──
    async def start_game(self, interaction: discord.Interaction, st) -> None:
        st.started = True
        for uid in st.players:
            st.hands[uid] = st.deck.draw(5)
            st.exchanged[uid] = False
        view = TableView(self, st)
        await interaction.response.edit_message(embed=self.table_embed(st), view=view)
        self._messages[st.match_id] = await interaction.original_response()

    def table_embed(self, st: DrawState) -> discord.Embed:
        cfg = self.bot.cfg
        lines = []
        for uid in st.players:
            mark = "✅交換済" if st.exchanged.get(uid) else "🤔考え中"
            lines.append(f"<@{uid}> — {mark}")
        e = common.embed(
            "🎴 ドローポーカー",
            "各自『手札を見る／交換』で札を入れ替え。全員終わったら主催者がショーダウン。",
            color=common.COLOR_MAIN,
        )
        e.add_field(name="参加者", value="\n".join(lines), inline=False)
        e.add_field(name="ポット", value=common.money(cfg, st.pot))
        return e

    async def refresh_table(self, st: DrawState) -> None:
        msg = self._messages.get(st.match_id)
        if msg:
            try:
                await msg.edit(embed=self.table_embed(st))
            except discord.HTTPException:
                pass

    async def showdown(self, interaction: discord.Interaction, st: DrawState) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        scored = [(uid, hand.best_hand(st.hands[uid])) for uid in st.players]
        best = max(s for _, s in scored)
        winners = [uid for uid, s in scored if s == best]

        pot = st.pot
        rake = economy.rake(db, pot)
        distributable = pot - rake
        share = distributable // len(winners)

        for uid in st.players:
            async with db.user_lock(uid):
                if uid in winners:
                    await db.adjust_balance(uid, share, "pvp_win", st.match_id)
                await match.clear_active(db, uid)
            row = await db.ensure_user(uid)
            await db.set_win_streak(uid, int(row["win_streak"]) + 1 if uid in winners else 0)

        e = common.embed("🎴 ショーダウン", color=common.COLOR_WIN)
        for uid, s in sorted(scored, key=lambda x: x[1], reverse=True):
            cards = " ".join(str(c) for c in st.hands[uid])
            win_mark = " 🏆" if uid in winners else ""
            e.add_field(
                name=f"{self._name(uid)}{win_mark}",
                value=f"{cards}\n{hand.describe(s)}",
                inline=False,
            )
        e.set_footer(
            text=f"勝者 {len(winners)}名 / 1人 +{share:,}(ポット {pot:,}・レーキ {rake:,})"
        )
        msg = self._messages.pop(st.match_id, None)
        self.matches.pop(st.match_id, None)
        # defer 済みなら edit_original_response、未応答なら response.edit_message、
        # それでもダメなら保持していたメッセージを直接 edit する
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=e, view=None)
            else:
                await interaction.response.edit_message(embed=e, view=None)
        except (discord.HTTPException, discord.NotFound):
            if msg:
                await msg.edit(embed=e, view=None)

    def _name(self, uid: int) -> str:
        u = self.bot.get_user(uid)
        return u.display_name if u else f"ユーザー{uid}"


async def setup(bot) -> None:
    await bot.add_cog(DrawCog(bot))
