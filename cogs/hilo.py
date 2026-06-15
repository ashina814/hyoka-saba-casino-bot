"""ハイロー(PVE)。

ルール:
- ベットして1枚目の基準カードが提示される。
- 「High(次は基準より上)」「Low(次は基準より下)」を選んで予想。
- 同じランク(同点)は Push: そのラウンドは無効、賭け額そのまま継続。
- 当たれば現在配当に **倍率** が掛かる(連続成功で雪だるま式)。
- 「Hold(確定)」を押すとその時点の配当で精算してゲーム終了。
- 外れると全没収。

実装上の要点:
- 公平性: 倍率は確率に応じて計算する。基準カードが A なら High はほぼ不可能なので
  わざと「自動 Pass(賭けに含めない)」とはせず、ユーザーが選ぶことに委ね、
  代わりに**配当倍率を確率の逆数 × (1 - house_edge)** で動的に算出する。
  これでハウスエッジを管理パネルから一元管理できる。
"""
from __future__ import annotations

import discord
# (app_commands removed: no slash commands here anymore)
from discord.ext import commands

from core.deck import Card, Deck, RANK_LABEL, card_emoji
from db.dao import InsufficientFunds
from ui import common


def _hilo_multiplier(base_rank: int, direction: str, house_edge: float) -> float:
    """基準ランクと方向から、当たり時の倍率を返す。

    52枚のうち、自分以外51枚に対して High/Low/Equal の枚数を数える。
    倍率 = (51 / win枚数) * (1 - house_edge) で公正な期待値に house_edge を乗せる。
    """
    # 各ランク 4枚。基準ランクと同じランクは 3枚(自分以外)
    if direction == "high":
        win = sum(4 for r in range(2, 15) if r > base_rank)
    else:  # low
        win = sum(4 for r in range(2, 15) if r < base_rank)
    if win == 0:
        return 0.0
    return (51.0 / win) * (1.0 - house_edge)


class HiloSession:
    """1ラウンド分の状態。User単位で保持。"""

    def __init__(self, bet: int) -> None:
        self.deck = Deck()
        self.bet = bet
        self.payout = bet      # 現時点の払い戻し(当初はベット額)
        self.current: Card = self.deck.draw(1)[0]
        self.streak = 0
        self.finished = False


class HiloView(discord.ui.View):
    def __init__(self, cog: "HiloCog", session: HiloSession, user_id: int) -> None:
        super().__init__(timeout=120)
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

    @discord.ui.button(label="ハイ (上)", emoji="⬆️",
                       style=discord.ButtonStyle.success)
    async def high(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.guess(interaction, self, "high")

    @discord.ui.button(label="ロー (下)", emoji="⬇️",
                       style=discord.ButtonStyle.primary)
    async def low(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.guess(interaction, self, "low")

    @discord.ui.button(label="確定して受け取る", emoji="💰",
                       style=discord.ButtonStyle.secondary)
    async def hold(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.hold(interaction, self)


class HiloCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def entry(self, interaction: discord.Interaction) -> None:
        await common.send_bet_panel(
            interaction, self.bot, self._start, title="📈 ハイロー — ベット"
        )

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
                await db.adjust_balance(user.id, -bet, "hilo_bet")
            except InsufficientFunds:
                await common.respond_with(
                    interaction, content="残高が足りません。", ephemeral=True
                )
                return
        # 全体JP積立&当選判定(横串)
        from core import global_jackpot as _gjp
        await _gjp.hook_pve_bet(self.bot, user.id, bet)
        session = HiloSession(bet)
        view = HiloView(self, session, user.id)
        await common.respond_with(
            interaction,
            embed=self._embed(session, "High か Low を選んでください"),
            view=view,
        )

    def _embed(self, s: HiloSession, msg: str, color: int = common.COLOR_MAIN) -> discord.Embed:
        cfg = self.bot.cfg
        edge = float(self.bot.db.setting("hilo_house_edge", 0.05))
        m_high = _hilo_multiplier(s.current.rank, "high", edge)
        m_low = _hilo_multiplier(s.current.rank, "low", edge)
        e = common.embed("📈 ハイロー", color=color)
        e.description = (
            f"# {card_emoji(s.current)}\n"
            f"**基準: {RANK_LABEL[s.current.rank]}{s.current.suit}**"
        )
        e.add_field(name="ベット", value=common.money(cfg, s.bet))
        e.add_field(name="現在配当", value=common.money(cfg, s.payout))
        e.add_field(name="連続成功", value=f"🔥 {s.streak}")
        e.add_field(
            name="次の倍率(当たり時)",
            value=f"ハイ **×{m_high:.2f}** / ロー **×{m_low:.2f}**\n"
                  "(同じランクが出たら無効=引き分け扱いで続行)",
            inline=False,
        )
        e.set_footer(text=msg)
        return e

    async def guess(
        self, interaction: discord.Interaction, view: HiloView, direction: str
    ) -> None:
        s = view.s
        if s.finished:
            await interaction.response.send_message("もう終了しています。", ephemeral=True)
            return
        if not s.deck.cards:
            # 起こりにくいが念のため
            await self._cashout(interaction, view, "デッキ切れで確定")
            return

        edge = float(self.bot.db.setting("hilo_house_edge", 0.05))
        mult = _hilo_multiplier(s.current.rank, direction, edge)
        nxt = s.deck.draw(1)[0]

        # 同ランク = 引き分け(継続、配当変化なし、基準は新しいカードへ)
        if nxt.rank == s.current.rank:
            s.current = nxt
            await interaction.response.edit_message(
                embed=self._embed(s, "🤝 同じランク。引き分けで続行(賭け額そのまま)",
                                  color=common.COLOR_INFO),
                view=view,
            )
            return

        is_win = (direction == "high" and nxt.rank > s.current.rank) or \
                 (direction == "low" and nxt.rank < s.current.rank)
        if is_win:
            # 運営ブーストがあれば倍率に乗算
            boost = common.boost_multiplier(self.bot)
            s.payout = int(s.payout * mult * boost)
            s.streak += 1
            s.current = nxt
            await interaction.response.edit_message(
                embed=self._embed(
                    s,
                    f"🎯 当たり！次は {card_emoji(nxt)} / "
                    f"配当 {s.payout:,} に上昇 🔥{s.streak}連勝",
                    color=common.COLOR_WIN,
                ),
                view=view,
            )
        else:
            # ハズレ: 全没収。結果Embedに「もう一回」ボタンを貼る
            s.finished = True
            e = common.embed("📈 ハイロー — 撃沈", color=common.COLOR_LOSE)
            dir_jp = "ハイ" if direction == "high" else "ロー"
            e.description = (
                f"# {card_emoji(s.current)}  →  {card_emoji(nxt)}\n"
                f"予想: **{dir_jp}** … 外れ"
            )
            new_balance = await self.bot.db.get_balance(view.user_id)
            e.add_field(name="ベット", value=common.money(self.bot.cfg, s.bet))
            e.add_field(name="収支", value=f"📉 -{s.bet:,}")
            e.add_field(name="残高", value=common.money(self.bot.cfg, new_balance))
            await self.bot.db.set_win_streak(view.user_id, 0)
            again = common.PlayAgainView(self.bot, view.user_id, s.bet, self._start)
            await interaction.response.edit_message(embed=e, view=again)
            view.stop()

    async def hold(self, interaction: discord.Interaction, view: HiloView) -> None:
        await self._cashout(interaction, view, "確定")

    async def _cashout(self, interaction, view: HiloView, label: str) -> None:
        s = view.s
        if s.finished:
            return
        s.finished = True
        for item in view.children:
            item.disabled = True
        db = self.bot.db
        async with db.user_lock(view.user_id):
            new_balance = await db.adjust_balance(view.user_id, s.payout, "hilo_win")
            row = await db.ensure_user(view.user_id)
            streak = int(row["win_streak"]) + 1 if s.payout > s.bet else 0
            await db.set_win_streak(view.user_id, streak)

        net = s.payout - s.bet
        color = common.COLOR_WIN if net >= 0 else common.COLOR_INFO
        e = common.embed(f"📈 ハイロー — {label}", color=color)
        e.description = f"# {card_emoji(s.current)}\n基準: {RANK_LABEL[s.current.rank]}{s.current.suit}"
        e.add_field(name="ベット", value=common.money(self.bot.cfg, s.bet))
        e.add_field(name="払戻", value=common.money(self.bot.cfg, s.payout))
        e.add_field(name="収支", value=("📈 +" if net >= 0 else "📉 ") + f"{net:,}")
        e.add_field(name="残高", value=common.money(self.bot.cfg, new_balance))
        e.add_field(name="連勝", value=f"🔥 {s.streak}", inline=False)
        # 称号判定(連勝とベット)
        from core import badges as _badges
        if streak > 0:
            await _badges.on_streak(self.bot, view.user_id, streak)
        await _badges.on_bet(self.bot, view.user_id)
        again = common.PlayAgainView(self.bot, view.user_id, s.bet, self._start)
        await interaction.response.edit_message(embed=e, view=again)
        view.stop()


async def setup(bot) -> None:
    await bot.add_cog(HiloCog(bot))
