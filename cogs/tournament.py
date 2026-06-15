"""大会モード。3種類のスコアリング (収支 / 連勝 / JP獲得額)。

設計:
- 同時開催は1個まで(管理しやすさ優先)。
- スコアは `tx_logs` を期間で絞って集計するので、参加登録は不要。
  期間中に1度でも該当アクションをした人は自動的にランキング対象。
- 終了は @tasks.loop(minutes=1) が end_ts を見て発動。賞金は 50/30/20 で分配。
- 開始/終了/中間ランキングは お喋りログに自動投稿。
- 開始時の prize_pool は **運営が積む額**(これは新規発行なので
  運用は管理者の判断で控えめに / 既存の没収プールから捻出など)。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from ui import common


# 種別
KIND_PROFIT = "profit"      # 期間内の収支(payout-bet)
KIND_STREAK = "streak"      # 期間内の最高連勝
KIND_JACKPOT = "jackpot"    # 期間内のJP獲得額

KIND_LABEL = {
    KIND_PROFIT: "💰 収支大会",
    KIND_STREAK: "🔥 連勝大会",
    KIND_JACKPOT: "💎 JP獲得大会",
}

DEFAULT_NAME = {
    KIND_PROFIT: "黄金週末杯",
    KIND_STREAK: "炎の連戦",
    KIND_JACKPOT: "一攫千金ナイト",
}

GAME_REASON_BET = (
    "slot_bet", "chinchiro_bet", "hilo_bet",
    "blackjack_bet", "blackjack_double", "blackjack_split",
    "pvp_escrow",
)
GAME_REASON_WIN = (
    "slot_win", "chinchiro_win", "hilo_win",
    "blackjack_win", "pvp_win", "slot_jackpot",
)


def _fmt_remain(sec: int) -> str:
    if sec <= 0:
        return "終了済"
    h, m = sec // 3600, (sec % 3600) // 60
    if h >= 24:
        d = h // 24
        return f"あと約 {d}日{h % 24}時間"
    return f"あと {h}時間{m}分"


_TS_FMT = "strftime('%Y-%m-%dT%H:%M:%fZ', ?, 'unixepoch')"


async def score_profit(db, start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """期間内の収支(プラス順) TOP10。"""
    reasons_in = ",".join(f"'{r}'" for r in GAME_REASON_BET + GAME_REASON_WIN)
    cur = await db.conn.execute(
        f"SELECT user_id, COALESCE(SUM(delta),0) score FROM tx_logs "
        f"WHERE reason IN ({reasons_in}) "
        f"AND ts >= {_TS_FMT} AND ts < {_TS_FMT} "
        f"GROUP BY user_id ORDER BY score DESC LIMIT 10",
        (start_ts, end_ts),
    )
    return [(int(r["user_id"]), int(r["score"])) for r in await cur.fetchall()]


async def score_jackpot(db, start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """期間内のJP獲得額 TOP10。"""
    cur = await db.conn.execute(
        f"SELECT user_id, COALESCE(SUM(delta),0) score FROM tx_logs "
        f"WHERE reason = 'slot_jackpot' "
        f"AND ts >= {_TS_FMT} AND ts < {_TS_FMT} "
        f"GROUP BY user_id ORDER BY score DESC LIMIT 10",
        (start_ts, end_ts),
    )
    return [(int(r["user_id"]), int(r["score"])) for r in await cur.fetchall()]


async def score_streak(db, start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """期間内の最高連勝。tx_logs から行進的に再構成。"""
    cur = await db.conn.execute(
        f"SELECT user_id, reason FROM tx_logs "
        f"WHERE ts >= {_TS_FMT} AND ts < {_TS_FMT} "
        f"ORDER BY user_id, id ASC",
        (start_ts, end_ts),
    )
    rows = await cur.fetchall()
    best: dict[int, int] = {}
    current: dict[int, int] = {}
    for r in rows:
        uid = int(r["user_id"])
        reason = r["reason"]
        if reason in GAME_REASON_WIN:
            current[uid] = current.get(uid, 0) + 1
            if current[uid] > best.get(uid, 0):
                best[uid] = current[uid]
        elif reason in GAME_REASON_BET:
            # ベットしただけでは負けではないが、収支確定待ちなのでカウント変えない
            pass
        else:
            # 異質なreason → 連勝リセットは行わない(慎重に)
            pass
    return sorted(
        ((uid, s) for uid, s in best.items() if s > 0),
        key=lambda x: -x[1],
    )[:10]


SCORER = {
    KIND_PROFIT: score_profit,
    KIND_JACKPOT: score_jackpot,
    KIND_STREAK: score_streak,
}


# 賞金分配率(1位/2位/3位)
PRIZE_SPLIT = (0.50, 0.30, 0.20)


# ───────────────────────── UI ─────────────────────────
class TournamentInfoView(discord.ui.View):
    """大会パネル(プレイヤー向け、ハブから開く)。"""

    def __init__(self, cog: "TournamentCog") -> None:
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(label="ランキング更新", emoji="🔄",
                       style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=await self.cog.info_embed(), view=self
        )


class TournamentStartModal(discord.ui.Modal, title="🏆 大会を開催"):
    name = discord.ui.TextInput(
        label="大会名(空欄で既定名)",
        required=False, max_length=40,
    )
    hours = discord.ui.TextInput(
        label="期間(時間)", placeholder="例: 48", required=True, max_length=4,
    )
    prize = discord.ui.TextInput(
        label="賞金プール(全額。1位50%/2位30%/3位20%で分配)",
        placeholder="例: 50000", required=True, max_length=10,
    )

    def __init__(self, cog: "TournamentCog", kind: str) -> None:
        super().__init__()
        self.cog = cog
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            hours = int(str(self.hours.value))
            prize = int(str(self.prize.value).replace(",", ""))
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 期間と賞金は数字で。", ephemeral=True
            )
            return
        if hours <= 0 or prize <= 0:
            await interaction.response.send_message(
                "⚠️ 期間と賞金は正の数で。", ephemeral=True
            )
            return

        # 既存大会のチェック(同時開催1まで)
        existing = await self.cog.bot.db.current_tournament()
        if existing:
            await interaction.response.send_message(
                f"⚠️ 既に開催中の大会があります: 「{existing['name']}」",
                ephemeral=True,
            )
            return

        name = (str(self.name.value).strip() or DEFAULT_NAME[self.kind])[:40]
        now_ts = int(time.time())
        end_ts = now_ts + hours * 3600
        tour_id = await self.cog.bot.db.start_tournament(
            name, self.kind, prize, now_ts, end_ts, interaction.user.id
        )
        await self.cog.bot.db.log_admin(
            interaction.user.id, "tournament_start", None,
            f"id={tour_id} kind={self.kind} hours={hours} prize={prize}",
        )

        # お喋りログ + 結果
        e = common.embed(
            f"🏆 大会開催！ — {name}",
            f"{KIND_LABEL[self.kind]} を {hours}時間 開催！\n"
            f"賞金プール **{prize:,}**(1位50% / 2位30% / 3位20%)\n"
            f"終了まで {_fmt_remain(end_ts - now_ts)}",
            color=common.COLOR_JACKPOT,
        )
        await common.post_casino_log(self.cog.bot, embed=e)
        await interaction.response.send_message(
            f"✅ 大会「{name}」を開催しました(id={tour_id})。", ephemeral=True
        )


class TournamentStartChoiceView(discord.ui.View):
    """『3種から選んでモーダル開く』入口View(管理者向け)。"""

    def __init__(self, cog: "TournamentCog") -> None:
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(label="💰 収支大会(黄金週末杯)",
                       style=discord.ButtonStyle.success)
    async def profit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            TournamentStartModal(self.cog, KIND_PROFIT)
        )

    @discord.ui.button(label="🔥 連勝大会(炎の連戦)",
                       style=discord.ButtonStyle.danger)
    async def streak(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            TournamentStartModal(self.cog, KIND_STREAK)
        )

    @discord.ui.button(label="💎 JP獲得大会(一攫千金ナイト)",
                       style=discord.ButtonStyle.primary)
    async def jackpot(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            TournamentStartModal(self.cog, KIND_JACKPOT)
        )


# ───────────────────────── Cog ─────────────────────────
class TournamentCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._end_loop.is_running():
            self._end_loop.start()

    def cog_unload(self) -> None:
        try:
            self._end_loop.cancel()
        except Exception:  # noqa: BLE001
            pass

    @tasks.loop(minutes=1)
    async def _end_loop(self) -> None:
        try:
            t = await self.bot.db.current_tournament()
            if t and int(t["end_ts"]) <= int(time.time()):
                await self._finalize(t)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("casino.tournament").exception("end loop")

    @_end_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── 大会終了 + 賞配布 ──
    async def _finalize(self, t) -> None:
        kind = t["kind"]
        ranking = await SCORER[kind](self.bot.db, int(t["start_ts"]), int(t["end_ts"]))
        winners_payouts: list[dict] = []
        prize = int(t["prize_pool"])
        for i, (uid, score) in enumerate(ranking[:3]):
            share = int(prize * PRIZE_SPLIT[i])
            if share <= 0:
                continue
            async with self.bot.db.user_lock(uid):
                await self.bot.db.adjust_balance(
                    uid, share, "tournament_prize", ref=str(t["id"])
                )
            winners_payouts.append(
                {"rank": i + 1, "user_id": uid, "score": score, "prize": share}
            )
            # 1位は称号獲得
            if i == 0:
                from core import badges as _badges
                await _badges.on_tournament_winner(self.bot, uid)
        await self.bot.db.finish_tournament(int(t["id"]), json.dumps(winners_payouts))

        # お喋りログに結果発表
        e = common.embed(
            f"🏆 大会終了 — {t['name']}",
            KIND_LABEL[kind] + "の結果発表！" if winners_payouts
            else KIND_LABEL[kind] + "に参加者なし。賞金はそのままです。",
            color=common.COLOR_JACKPOT,
        )
        if winners_payouts:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for w in winners_payouts:
                lines.append(
                    f"{medals[w['rank'] - 1]} <@{w['user_id']}>  "
                    f"スコア **{w['score']:,}**  賞金 +{w['prize']:,}"
                )
            e.add_field(name="結果", value="\n".join(lines), inline=False)
        await common.post_casino_log(self.bot, embed=e)

    # ── プレイヤー向け情報 ──
    async def info_embed(self) -> discord.Embed:
        t = await self.bot.db.current_tournament()
        if not t:
            return common.embed(
                "🏆 大会",
                "現在開催中の大会はありません。次回をお楽しみに！",
                color=common.COLOR_INFO,
            )
        now_ts = int(time.time())
        remain = max(0, int(t["end_ts"]) - now_ts)
        kind = t["kind"]
        e = common.embed(
            f"🏆 開催中: {t['name']}",
            f"{KIND_LABEL[kind]}  /  {_fmt_remain(remain)}\n"
            f"賞金プール **{int(t['prize_pool']):,}** "
            f"(1位50% / 2位30% / 3位20%)",
            color=common.COLOR_JACKPOT,
        )
        ranking = await SCORER[kind](self.bot.db, int(t["start_ts"]), int(t["end_ts"]))
        if ranking:
            medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
            lines = [
                f"{medals[i]} <@{uid}>  スコア **{score:,}**"
                for i, (uid, score) in enumerate(ranking)
            ]
            e.add_field(name="現在のランキング(TOP10)",
                        value="\n".join(lines), inline=False)
        else:
            e.add_field(name="現在のランキング",
                        value="まだ誰も参加していません。",
                        inline=False)
        return e

    # ── ハブから開く入口 ──
    async def entry(self, interaction: discord.Interaction) -> None:
        view = TournamentInfoView(self)
        await interaction.response.send_message(
            embed=await self.info_embed(), view=view, ephemeral=True
        )


async def setup(bot) -> None:
    await bot.add_cog(TournamentCog(bot))
