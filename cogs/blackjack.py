"""ブラックジャック(対CPU)。

ルール(標準):
- 1デッキ。配り直すたびに新デッキを切る(カウンティング対策不要)。
- プレイヤー2枚、ディーラー2枚(うち1枚伏せ)。
- A は 1 または 11 で有利な方を採用(ソフトハンド対応)。
- ナチュラルブラックジャック(最初の2枚で21): **1.5倍配当**(3:2)。
  ただしディーラーも BJ なら Push。
- Hit / Stand / Double Down(初手のみ、ベット2倍で1枚のみ追加) /
  Split(同ランクのときベット同額追加して2ハンドに分ける)。
- ディーラーは 17 以上で Stand(Soft 17 は Stand)。

設計:
- セッションは Cog 内 dict で保持。1ユーザー同時1セッションのみ。
- View は永続化しない(ハンド中限定 timeout=180)。
- Split 中は手番が複数になるので current_hand を持って進行。
"""
from __future__ import annotations

import math

import discord
# (app_commands removed: no slash commands here anymore)
from discord.ext import commands

from core.deck import CARD_BACK, Card, Deck, card_emoji, hand_emoji
from db.dao import InsufficientFunds
from ui import common


# ───────────────────────── ロジック ─────────────────────────
def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """ハンド合計と「ソフト(A=11 を採用)かどうか」を返す。

    A は最初 11 として加算、合計が 21 を超えたら 10 ずつ引いて 1 にダウングレード。
    """
    total = 0
    aces = 0
    for c in cards:
        if c.rank == 14:
            aces += 1
            total += 11
        elif c.rank >= 11:    # J/Q/K
            total += 10
        else:
            total += c.rank
    soft = False
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    soft = (aces > 0 and total <= 21)
    return total, soft


def is_blackjack(cards: list[Card]) -> bool:
    if len(cards) != 2:
        return False
    v, _ = hand_value(cards)
    return v == 21


def hand_label(cards: list[Card]) -> str:
    v, soft = hand_value(cards)
    return f"{hand_emoji(cards)}  (**{'ソフト ' if soft else ''}{v}**)"


# ───────────────────────── セッション ─────────────────────────
class BJHand:
    def __init__(self, cards: list[Card], bet: int) -> None:
        self.cards = cards
        self.bet = bet
        self.stood = False
        self.doubled = False
        self.busted = False

    @property
    def value(self) -> int:
        return hand_value(self.cards)[0]


class BJSession:
    def __init__(self, bet: int) -> None:
        self.deck = Deck()
        self.bet = bet
        self.player_hands: list[BJHand] = [BJHand(self.deck.draw(2), bet)]
        self.dealer: list[Card] = self.deck.draw(2)
        self.current = 0
        self.finished = False
        self.reveal_dealer = False

    def active_hand(self) -> BJHand | None:
        if self.current >= len(self.player_hands):
            return None
        return self.player_hands[self.current]

    def advance_hand(self) -> None:
        """次の未確定ハンドへ進める。なければ finished の準備状態に。"""
        self.current += 1
        while self.current < len(self.player_hands):
            h = self.player_hands[self.current]
            if h.busted or h.stood:
                self.current += 1
            else:
                return

    def all_done(self) -> bool:
        return all(h.busted or h.stood for h in self.player_hands)


# ───────────────────────── ビュー ─────────────────────────
class BJView(discord.ui.View):
    def __init__(self, cog: "BlackjackCog", session: BJSession, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.s = session
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のセッションは操作できません。", ephemeral=True
            )
            return False
        return True

    def refresh_buttons(self) -> None:
        """現在のアクティブハンドに合わせてボタンの enable/disable を更新。"""
        h = self.s.active_hand()
        for item in self.children:
            item.disabled = True
        if h is None:
            return
        # Hit / Stand は常に有効
        for item in self.children:
            if item.custom_id_local in ("hit", "stand"):
                item.disabled = False
        # Double / Split は初手(2枚)時のみ
        is_initial = len(h.cards) == 2 and not h.doubled
        for item in self.children:
            if item.custom_id_local == "double" and is_initial:
                item.disabled = False
            if item.custom_id_local == "split" and is_initial \
                    and h.cards[0].rank == h.cards[1].rank \
                    and len(self.s.player_hands) < 4:
                item.disabled = False

    @discord.ui.button(label="ヒット(引く)", emoji="➕",
                       style=discord.ButtonStyle.success)
    async def hit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.action(interaction, self, "hit")

    @discord.ui.button(label="スタンド(止める)", emoji="✋",
                       style=discord.ButtonStyle.primary)
    async def stand(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.action(interaction, self, "stand")

    @discord.ui.button(label="ダブル(倍掛け)", emoji="2️⃣",
                       style=discord.ButtonStyle.secondary)
    async def double(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.action(interaction, self, "double")

    @discord.ui.button(label="スプリット(分割)", emoji="✂️",
                       style=discord.ButtonStyle.secondary)
    async def split(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.action(interaction, self, "split")


# Button に「自分が何のアクションか」を持たせるための小細工
for _name, _local in (("hit", "hit"), ("stand", "stand"),
                      ("double", "double"), ("split", "split")):
    pass  # 下で属性付与する


# ───────────────────────── Cog ─────────────────────────
class BlackjackCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.sessions: dict[int, BJSession] = {}

    # ── 入口 ──
    async def entry(self, interaction: discord.Interaction) -> None:
        if interaction.user.id in self.sessions:
            await interaction.response.send_message(
                "あなたは既にハンド進行中です。終わらせてから次へ。",
                ephemeral=True,
            )
            return
        await common.send_bet_panel(
            interaction, self.bot, self._start, title="🃏 ブラックジャック — ベット"
        )

    # ── ハンド開始 ──
    async def _start(self, interaction: discord.Interaction, bet: int) -> None:
        db = self.bot.db
        user = interaction.user
        async with db.user_lock(user.id):
            if await db.is_frozen(user.id):
                await common.respond_with(
                    interaction, content="🧊 凍結中です。", ephemeral=True
                )
                return
            try:
                await db.adjust_balance(user.id, -bet, "blackjack_bet")
            except InsufficientFunds:
                await common.respond_with(
                    interaction, content="残高が足りません。", ephemeral=True
                )
                return

        s = BJSession(bet)
        self.sessions[user.id] = s

        # ナチュラルチェック(両者BJ → Push、プレイヤーのみBJ → 1.5倍即勝利、
        # ディーラーのみBJ → 即敗北)
        player_bj = is_blackjack(s.player_hands[0].cards)
        dealer_bj = is_blackjack(s.dealer)
        if player_bj or dealer_bj:
            s.player_hands[0].stood = True
            s.reveal_dealer = True
            s.finished = True
            msg = await common.respond_with(interaction, embed=self._table_embed(s))
            if msg is None:
                msg = await interaction.original_response()
            await self._settle_msg(msg, user.id, s,
                                   natural_player=player_bj,
                                   natural_dealer=dealer_bj)
            return

        view = BJView(self, s, user.id)
        # Button インスタンスに custom_id_local を後付け
        for item, key in zip(view.children, ["hit", "stand", "double", "split"]):
            item.custom_id_local = key  # type: ignore[attr-defined]
        view.refresh_buttons()
        msg = await common.respond_with(
            interaction, embed=self._table_embed(s), view=view
        )
        if msg is None:
            msg = await interaction.original_response()
        # アクション側からも結果メッセージを参照できるように保持
        s.message = msg  # type: ignore[attr-defined]

    # ── 表示 ──
    def _table_embed(self, s: BJSession, footer: str = "") -> discord.Embed:
        cfg = self.bot.cfg
        e = common.embed("🃏 ブラックジャック", color=common.COLOR_MAIN)
        # ディーラー
        if s.reveal_dealer:
            dv, _ = hand_value(s.dealer)
            dealer_str = f"{hand_emoji(s.dealer)}  (**{dv}**)"
        else:
            dealer_str = f"{card_emoji(s.dealer[0])} {CARD_BACK}  (?)"
        e.add_field(name="🤖 ディーラー", value=dealer_str, inline=False)

        # プレイヤー(複数ハンドあり得る)
        for i, h in enumerate(s.player_hands):
            mark = "👉 " if i == s.current and not (h.busted or h.stood) else ""
            tag = ""
            if h.busted:
                tag = " 💥バースト"
            elif h.stood:
                tag = " ✋止め"
            if h.doubled:
                tag += " 2️⃣倍掛"
            title = f"{mark}🧑 ハンド{i+1}{tag} (賭 {h.bet:,})"
            e.add_field(name=title, value=hand_label(h.cards), inline=False)

        if footer:
            e.set_footer(text=footer)
        return e

    # ── アクション ──
    async def action(
        self, interaction: discord.Interaction, view: BJView, kind: str
    ) -> None:
        s = view.s
        h = s.active_hand()
        if h is None or s.finished:
            await interaction.response.send_message("手番がありません。", ephemeral=True)
            return

        if kind == "hit":
            h.cards.append(s.deck.draw(1)[0])
            v = h.value
            if v > 21:
                h.busted = True
                s.advance_hand()
        elif kind == "stand":
            h.stood = True
            s.advance_hand()
        elif kind == "double":
            if len(h.cards) != 2 or h.doubled:
                await interaction.response.send_message(
                    "Double は初手のみ可能です。", ephemeral=True
                )
                return
            # 追加ベットを引き落とす
            db = self.bot.db
            async with db.user_lock(view.user_id):
                try:
                    await db.adjust_balance(view.user_id, -h.bet, "blackjack_double")
                except InsufficientFunds:
                    await interaction.response.send_message(
                        "Double 分の残高が足りません。", ephemeral=True
                    )
                    return
            h.bet *= 2
            h.doubled = True
            h.cards.append(s.deck.draw(1)[0])
            if h.value > 21:
                h.busted = True
            else:
                h.stood = True
            s.advance_hand()
        elif kind == "split":
            if len(h.cards) != 2 or h.cards[0].rank != h.cards[1].rank \
                    or len(s.player_hands) >= 4:
                await interaction.response.send_message(
                    "Split できません。", ephemeral=True
                )
                return
            db = self.bot.db
            async with db.user_lock(view.user_id):
                try:
                    await db.adjust_balance(view.user_id, -h.bet, "blackjack_split")
                except InsufficientFunds:
                    await interaction.response.send_message(
                        "Split 分の残高が足りません。", ephemeral=True
                    )
                    return
            c1, c2 = h.cards
            h.cards = [c1, s.deck.draw(1)[0]]
            new_hand = BJHand([c2, s.deck.draw(1)[0]], h.bet)
            s.player_hands.append(new_hand)

        # 全ハンド終了ならディーラーターン → 精算
        if s.all_done():
            await self._dealer_play(s)
            s.finished = True
            for item in view.children:
                item.disabled = True
            # 進行画面は中継せず、直接「結果+もう一回」に上書きする方が分かりやすい
            await interaction.response.defer()
            msg = getattr(s, "message", None) or await interaction.original_response()
            await self._settle_msg(msg, view.user_id, s)
            view.stop()
            return

        view.refresh_buttons()
        await interaction.response.edit_message(
            embed=self._table_embed(s), view=view
        )

    async def _dealer_play(self, s: BJSession) -> None:
        s.reveal_dealer = True
        while True:
            v, soft = hand_value(s.dealer)
            if v >= 17:  # Stand on Soft 17
                return
            s.dealer.append(s.deck.draw(1)[0])

    # ── 精算(メッセージ直接編集版) ──
    async def _settle_msg(
        self, msg, user_id: int, s: BJSession,
        natural_player: bool = False, natural_dealer: bool = False,
    ) -> None:
        db = self.bot.db
        cfg = self.bot.cfg
        dv, _ = hand_value(s.dealer)
        dealer_bust = dv > 21

        lines: list[str] = []
        total_credit = 0
        any_win = False
        for i, h in enumerate(s.player_hands):
            pv = h.value
            res = ""
            credit = 0
            if natural_player and natural_dealer:
                credit = h.bet
                res = "引き分け(両者ナチュラル)"
            elif natural_player:
                credit = h.bet + int(math.floor(h.bet * 1.5))
                res = f"🎉 ナチュラル! 1.5倍配当 +{credit - h.bet:,}"
                any_win = True
            elif natural_dealer:
                credit = 0
                res = "💀 ディーラーがナチュラル"
            elif h.busted:
                credit = 0
                res = "💥 バースト"
            elif dealer_bust:
                credit = h.bet * 2
                res = f"🎯 勝ち(ディーラーがバースト) +{h.bet:,}"
                any_win = True
            elif pv > dv:
                credit = h.bet * 2
                res = f"🎯 勝ち +{h.bet:,}"
                any_win = True
            elif pv < dv:
                credit = 0
                res = "🚫 負け"
            else:
                credit = h.bet
                res = "🤝 引き分け"
            total_credit += credit
            lines.append(f"ハンド{i+1}({pv}): {res}")

        async with db.user_lock(user_id):
            if total_credit:
                await db.adjust_balance(user_id, total_credit, "blackjack_win")
            new_balance = await db.get_balance(user_id)
            row = await db.ensure_user(user_id)
            await db.set_win_streak(
                user_id, int(row["win_streak"]) + 1 if any_win else 0
            )

        # 総ベット = sum(h.bet)、収支 = total_credit - 総ベット
        total_bet = sum(h.bet for h in s.player_hands)
        net = total_credit - total_bet
        color = (
            common.COLOR_WIN if net > 0
            else common.COLOR_LOSE if net < 0
            else common.COLOR_INFO
        )
        e = common.embed("🃏 ブラックジャック — 結果", color=color)
        e.description = (
            f"🤖 ディーラー: {hand_label(s.dealer)}"
            + (" 💥バースト" if dealer_bust else "")
        )
        for i, h in enumerate(s.player_hands):
            e.add_field(
                name=f"🧑 ハンド{i+1} (賭 {h.bet:,})",
                value=hand_label(h.cards),
                inline=False,
            )
        e.add_field(name="判定", value="\n".join(lines), inline=False)
        e.add_field(name="総ベット", value=common.money(cfg, total_bet))
        e.add_field(name="払戻", value=common.money(cfg, total_credit))
        e.add_field(name="収支", value=("📈 +" if net >= 0 else "📉 ") + f"{net:,}")
        e.add_field(name="残高", value=common.money(cfg, new_balance), inline=False)

        # 後始末: セッション削除
        self.sessions.pop(user_id, None)

        # 元メッセージを「結果 + もう一回」に上書き
        again = common.PlayAgainView(self.bot, user_id, s.bet, self._start)
        try:
            await msg.edit(embed=e, view=again)
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    await bot.add_cog(BlackjackCog(bot))
