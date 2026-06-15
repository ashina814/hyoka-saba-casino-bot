"""テキサスホールデム(PVP)。

設計:
- 参加費 = バイイン(その場のスタック)。各自このスタック内でベットし、
  終了時に残りスタック+獲得ポットを残高へ戻す。ハンド中は残高を触らないので
  整合が取りやすい。レーキはポットから徴収(シンク)。
- ブラインドはバイインから算出(BB = buyin/20 目安)。
- ベットラウンド(プリフロップ/フロップ/ターン/リバー)を1ハンド進行。
- サイドポットにも対応(オールインの取り分を正しく分配)。
- 操作はボタン: フォールド / チェック・コール / レイズ / オールイン、手札は本人のみ表示。
"""
from __future__ import annotations

import discord
# (app_commands removed: no slash commands here anymore)
from discord.ext import commands

from core import economy, hand, match
from core.deck import Card, Deck
from ui import common

MIN_PLAYERS, MAX_PLAYERS = 2, 8
STREETS = ["preflop", "flop", "turn", "river"]


class HoldemState:
    def __init__(self, match_id: str, host_id: int, bet: int) -> None:
        self.match_id = match_id
        self.host_id = host_id
        self.bet = bet                 # = バイイン(スタック)
        self.players: list[int] = []   # 着席順
        self.started = False

        self.deck = Deck()
        self.hole: dict[int, list[Card]] = {}
        self.board: list[Card] = []
        self.stacks: dict[int, int] = {}
        self.committed: dict[int, int] = {}      # ハンド累計拠出
        self.street_bet: dict[int, int] = {}     # 現ストリート拠出
        self.folded: set[int] = set()
        self.allin: set[int] = set()
        self.acted: set[int] = set()             # 現ラウンドで行動済み

        self.street_idx = 0
        self.current_bet = 0
        self.min_raise = 0
        self.bb = 0
        self.dealer = 0                # players 内インデックス
        self.actor: int | None = None  # 現在手番の uid
        self.finished = False

    @property
    def pot(self) -> int:
        return sum(self.committed.values())

    def active(self) -> list[int]:
        """まだ降りていないプレイヤー(オールイン含む)。"""
        return [u for u in self.players if u not in self.folded]

    def can_act(self) -> list[int]:
        return [u for u in self.players if u not in self.folded and u not in self.allin]


class RaiseModal(discord.ui.Modal, title="レイズ"):
    amount = discord.ui.TextInput(label="レイズ後の合計ベット額", placeholder="例: 200")

    def __init__(self, cog: "HoldemCog", st: HoldemState) -> None:
        super().__init__()
        self.cog = cog
        self.st = st

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            target = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            await interaction.response.send_message("数値で入力してください。", ephemeral=True)
            return
        await self.cog.do_raise(interaction, self.st, target)


class HoldemView(discord.ui.View):
    def __init__(self, cog: "HoldemCog", st: HoldemState) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.st = st

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.st.actor:
            await interaction.response.send_message(
                "あなたの番ではありません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="フォールド", emoji="🏳️", style=discord.ButtonStyle.danger)
    async def fold(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self.cog.do_fold(interaction, self.st)

    @discord.ui.button(label="チェック / コール", emoji="✅",
                       style=discord.ButtonStyle.success)
    async def call(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self.cog.do_call(interaction, self.st)

    @discord.ui.button(label="レイズ", emoji="⬆️", style=discord.ButtonStyle.primary)
    async def raise_(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(RaiseModal(self.cog, self.st))

    @discord.ui.button(label="オールイン", emoji="🔥", style=discord.ButtonStyle.secondary)
    async def allin(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        st = self.st
        target = st.street_bet.get(interaction.user.id, 0) + st.stacks[interaction.user.id]
        await self.cog.do_raise(interaction, st, target, allin=True)

    @discord.ui.button(label="手札を見る", emoji="🎴", style=discord.ButtonStyle.secondary,
                       row=1)
    async def peek(self, interaction: discord.Interaction, _: discord.ui.Button):
        st = self.st
        uid = interaction.user.id
        if uid not in st.hole:
            await interaction.response.send_message("参加していません。", ephemeral=True)
            return
        cards = " ".join(str(c) for c in st.hole[uid])
        seven = st.hole[uid] + st.board
        score = hand.best_hand(seven) if len(seven) >= 5 else None
        desc = f"{cards}"
        if score:
            desc += f"\n現在の役: **{hand.describe(score)}**"
        await interaction.response.send_message(
            embed=common.embed("あなたの手札", desc, color=common.COLOR_INFO),
            ephemeral=True,
        )


class HoldemCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.matches: dict[str, HoldemState] = {}
        self._messages: dict[str, discord.Message] = {}
        self._views: dict[str, HoldemView] = {}

    # ── ロビー ──
    async def entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            common.BetModal(self.bot, "🃏 ホールデム — 場を立てる(参加費=スタック)", self._create)
        )

    async def _create(self, interaction: discord.Interaction, bet: int) -> None:
        if bet < 40:
            await interaction.response.send_message(
                "ホールデムはブラインド計算のためバイイン40以上にしてください。",
                ephemeral=True,
            )
            return
        mid = match.new_match_id("holdem")
        st = HoldemState(mid, interaction.user.id, bet)
        self.matches[mid] = st
        from cogs._lobby import LobbyView
        view = LobbyView(self, st, MIN_PLAYERS, MAX_PLAYERS, self.start_game)
        await interaction.response.send_message(
            embed=view.embed("🃏 テキサスホールデム"), view=view
        )
        self._messages[mid] = await interaction.original_response()

    # ── ハンド開始 ──
    async def start_game(self, interaction: discord.Interaction, st: HoldemState) -> None:
        st.started = True
        for uid in st.players:
            st.stacks[uid] = st.bet
            st.committed[uid] = 0
            st.street_bet[uid] = 0
            st.hole[uid] = st.deck.draw(2)

        st.bb = max(2, st.bet // 20)
        sb = max(1, st.bb // 2)
        n = len(st.players)
        st.dealer = 0
        if n == 2:
            sb_i, bb_i = 0, 1
        else:
            sb_i, bb_i = 1 % n, 2 % n
        self._post_blind(st, st.players[sb_i], sb)
        self._post_blind(st, st.players[bb_i], st.bb)
        st.current_bet = st.bb
        st.min_raise = st.bb
        st.acted = set()
        # プリフロップ最初の手番 = BB の次
        first = (bb_i + 1) % n
        st.actor = self._next_can_act(st, st.players[first], include_start=True)

        view = HoldemView(self, st)
        self._views[st.match_id] = view
        await interaction.response.edit_message(embed=self.table_embed(st), view=view)
        self._messages[st.match_id] = await interaction.original_response()

    def _post_blind(self, st: HoldemState, uid: int, amount: int) -> None:
        pay = min(amount, st.stacks[uid])
        st.stacks[uid] -= pay
        st.street_bet[uid] += pay
        st.committed[uid] += pay
        if st.stacks[uid] == 0:
            st.allin.add(uid)

    # ── 手番ユーティリティ ──
    def _idx(self, st: HoldemState, uid: int) -> int:
        return st.players.index(uid)

    def _next_can_act(self, st: HoldemState, start_uid: int, include_start: bool = False):
        """start_uid から時計回りで、行動可能(降りてない・オールインでない)な
        最初のプレイヤーを返す。いなければ None。"""
        n = len(st.players)
        start = self._idx(st, start_uid)
        for k in range(0 if include_start else 1, n + 1):
            uid = st.players[(start + k) % n]
            if uid not in st.folded and uid not in st.allin:
                return uid
        return None

    def _round_complete(self, st: HoldemState) -> bool:
        for uid in st.players:
            if uid in st.folded or uid in st.allin:
                continue
            if uid not in st.acted or st.street_bet[uid] != st.current_bet:
                return False
        return True

    # ── アクション ──
    async def do_fold(self, interaction, st: HoldemState) -> None:
        uid = interaction.user.id
        st.folded.add(uid)
        st.acted.add(uid)
        await self._after_action(interaction, st)

    async def do_call(self, interaction, st: HoldemState) -> None:
        uid = interaction.user.id
        need = st.current_bet - st.street_bet[uid]
        pay = min(need, st.stacks[uid])
        st.stacks[uid] -= pay
        st.street_bet[uid] += pay
        st.committed[uid] += pay
        if st.stacks[uid] == 0:
            st.allin.add(uid)
        st.acted.add(uid)
        await self._after_action(interaction, st)

    async def do_raise(self, interaction, st: HoldemState, target: int, allin: bool = False) -> None:
        uid = interaction.user.id
        if uid != st.actor:
            await interaction.response.send_message("あなたの番ではありません。", ephemeral=True)
            return
        cur = st.street_bet[uid]
        max_target = cur + st.stacks[uid]
        if target > max_target:
            target = max_target
        # 最低レイズ額の検証(オールインで足りない場合は許容)
        min_target = st.current_bet + st.min_raise
        is_allin = target == max_target
        if target <= st.current_bet:
            await interaction.response.send_message(
                f"現在のベット {st.current_bet} より高くしてください。", ephemeral=True
            )
            return
        if target < min_target and not is_allin:
            await interaction.response.send_message(
                f"最低レイズは合計 {min_target} です(オールインを除く)。", ephemeral=True
            )
            return
        pay = target - cur
        st.stacks[uid] -= pay
        st.street_bet[uid] = target
        st.committed[uid] += pay
        raise_size = target - st.current_bet
        st.min_raise = max(st.min_raise, raise_size)
        st.current_bet = target
        if st.stacks[uid] == 0:
            st.allin.add(uid)
        # レイズで他者の行動権が復活
        st.acted = {uid}
        await self._after_action(interaction, st)

    async def _after_action(self, interaction, st: HoldemState) -> None:
        # 1人だけ残ったら即決着
        if len(st.active()) == 1:
            await self._showdown(interaction, st, uncontested=True)
            return
        if self._round_complete(st):
            await self._advance_street(interaction, st)
        else:
            st.actor = self._next_can_act(st, st.actor)
            if st.actor is None:
                await self._advance_street(interaction, st)
            else:
                await self._render(interaction, st)

    async def _advance_street(self, interaction, st: HoldemState) -> None:
        # ストリート締め
        st.acted = set()
        st.street_bet = {u: 0 for u in st.players}
        st.current_bet = 0
        st.min_raise = st.bb

        # これ以上ベットできる人が1人以下なら最後まで配ってショーダウン
        while len(st.can_act()) <= 1 and st.street_idx < 3:
            st.street_idx += 1
            self._deal_board(st)
        if st.street_idx >= 3 and len(st.can_act()) <= 1:
            await self._showdown(interaction, st)
            return
        if len(st.can_act()) <= 1:
            await self._showdown(interaction, st)
            return

        if st.street_idx >= 3:
            await self._showdown(interaction, st)
            return

        st.street_idx += 1
        self._deal_board(st)
        # ポストフロップ最初の手番 = ディーラーの次の行動可能者
        st.actor = self._next_can_act(st, st.players[st.dealer], include_start=False)
        if st.actor is None:
            await self._showdown(interaction, st)
            return
        await self._render(interaction, st)

    def _deal_board(self, st: HoldemState) -> None:
        if st.street_idx == 1:           # flop
            st.board += st.deck.draw(3)
        elif st.street_idx in (2, 3):    # turn / river
            st.board += st.deck.draw(1)

    # ── 表示 ──
    def table_embed(self, st: HoldemState) -> discord.Embed:
        cfg = self.bot.cfg
        board = " ".join(str(c) for c in st.board) if st.board else "— 未公開 —"
        street_name = ["プリフロップ", "フロップ", "ターン", "リバー"][st.street_idx]
        e = common.embed(
            f"🃏 ホールデム — {street_name}",
            f"**ボード:** {board}",
            color=common.COLOR_MAIN,
        )
        lines = []
        for uid in st.players:
            tags = []
            if uid in st.folded:
                tags.append("🏳️降りた")
            if uid in st.allin:
                tags.append("🔥オールイン")
            if uid == st.actor:
                tags.append("👉手番")
            tag = " ".join(tags)
            lines.append(
                f"<@{uid}> — 💰{st.stacks.get(uid,0):,} / 賭{st.street_bet.get(uid,0):,} {tag}"
            )
        e.add_field(name="プレイヤー", value="\n".join(lines), inline=False)
        e.add_field(name="ポット", value=common.money(cfg, st.pot))
        e.add_field(name="コール額", value=f"{st.current_bet:,}")
        if st.actor:
            e.set_footer(text="自分の番になったらボタンで行動。手札は『手札を見る』で確認。")
        return e

    async def _render(self, interaction, st: HoldemState) -> None:
        view = self._views.get(st.match_id)
        embed = self.table_embed(st)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        except (discord.HTTPException, discord.NotFound):
            msg = self._messages.get(st.match_id)
            if msg:
                try:
                    await msg.edit(embed=embed, view=view)
                except discord.HTTPException:
                    pass

    # ── ショーダウン & 精算 ──
    async def _showdown(self, interaction, st: HoldemState, uncontested: bool = False) -> None:
        if st.finished:
            return
        st.finished = True
        db = self.bot.db
        cfg = self.bot.cfg

        # 全プレイヤー分のDB書き込みが入るので 3秒制限を回避するため defer する。
        # Modal からのレイズ経由など、既に response 済みなら何もしない。
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass

        # サイドポットを構築して勝者へ分配
        payouts = self._distribute(st)

        # 残高へ反映: 残りスタック + 獲得分
        results = []
        for uid in st.players:
            credit = st.stacks[uid] + payouts.get(uid, 0)
            async with db.user_lock(uid):
                if credit:
                    await db.adjust_balance(uid, credit, "pvp_win", st.match_id)
                await match.clear_active(db, uid)
            net = credit - st.bet
            row = await db.ensure_user(uid)
            won = payouts.get(uid, 0) > 0
            await db.set_win_streak(uid, int(row["win_streak"]) + 1 if won else 0)
            results.append((uid, net))

        board = " ".join(str(c) for c in st.board) if st.board else "—"
        e = common.embed("🃏 ホールデム — 決着", f"**ボード:** {board}", color=common.COLOR_WIN)
        if uncontested:
            winner = st.active()[0]
            e.description += f"\n\n他全員フォールド。<@{winner}> の勝ち。"
        else:
            for uid in st.players:
                if uid in st.folded:
                    continue
                seven = st.hole[uid] + st.board
                score = hand.best_hand(seven)
                cards = " ".join(str(c) for c in st.hole[uid])
                e.add_field(
                    name=self._name(uid),
                    value=f"{cards} → {hand.describe(score)}",
                    inline=False,
                )
        res_lines = "\n".join(
            f"<@{u}> {'📈 +' if net >= 0 else '📉 '}{net:,}" for u, net in results
        )
        e.add_field(name="収支", value=res_lines, inline=False)
        total_rake = economy.rake(db, st.pot)
        e.set_footer(text=f"ポット {st.pot:,} / レーキ {total_rake:,} を徴収")

        self.matches.pop(st.match_id, None)
        self._views.pop(st.match_id, None)
        msg = self._messages.pop(st.match_id, None)
        # この時点で defer or 何らかの response 済みのことが多いので edit_original_response 優先
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=e, view=None)
            else:
                await interaction.response.edit_message(embed=e, view=None)
        except (discord.HTTPException, discord.NotFound):
            if msg:
                try:
                    await msg.edit(embed=e, view=None)
                except discord.HTTPException:
                    pass

    def _distribute(self, st: HoldemState) -> dict[int, int]:
        """サイドポット対応の分配。各 uid の獲得額を返す。レーキは各ポットから控除。"""
        db = self.bot.db
        contrib = dict(st.committed)
        payouts: dict[int, int] = {u: 0 for u in st.players}

        # 拠出レベルごとにポット層を作る
        levels = sorted(set(v for v in contrib.values() if v > 0))
        prev = 0
        for lvl in levels:
            per = lvl - prev
            eligible = [u for u in st.players if contrib[u] >= lvl]
            pot_amount = per * len(eligible)
            prev = lvl
            if pot_amount <= 0:
                continue
            rake = economy.rake(db, pot_amount)
            net = pot_amount - rake
            contenders = [u for u in eligible if u not in st.folded]
            if not contenders:
                contenders = eligible  # 念のため
            best = max(hand.best_hand(st.hole[u] + st.board) for u in contenders)
            winners = [u for u in contenders
                       if hand.best_hand(st.hole[u] + st.board) == best]
            share = net // len(winners)
            for w in winners:
                payouts[w] += share
            # 端数は最初の勝者へ
            payouts[winners[0]] += net - share * len(winners)
        return payouts

    def _name(self, uid: int) -> str:
        u = self.bot.get_user(uid)
        return u.display_name if u else f"ユーザー{uid}"


async def setup(bot) -> None:
    await bot.add_cog(HoldemCog(bot))
