"""デイリーチャレンジ。

設計:
- ユーザー × 日付 で **決定論的に3個** のチャレンジを選ぶ(seed=user_id+日付)。
  → DB に「今日のお題」を保持する必要なし、計算で再現できる。
- 進捗は **tx_logs を集計** して見る方式。チャレンジは「reason に対する count/sum」と
  「target」だけで記述できる。新ゲーム追加してもチャレンジ側は触らなくて良い。
- 受取済みは `claimed_challenges` テーブルに刻む(同一日に二重受取を防ぐ)。
- 機能ON/OFFは `settings.challenges_enabled`。新規ユーザーは作成日以降のチャレンジ
  だけ対象、過去日は出さない(`/チャレンジ`は当日分のみ)。

チャレンジ定義は CHALLENGES に追記すれば自動的にローテに乗る(下の表)。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from ui import common


Kind = Literal["count_reason", "sum_neg_reason", "sum_pos_reason"]


@dataclass(frozen=True)
class Challenge:
    """1つのチャレンジ定義。

    kind:
      - count_reason  : tx_logs の reason in reasons の **件数** で測る
      - sum_neg_reason: tx_logs の reason in reasons の **負の合計の絶対値**(=賭けた額)
      - sum_pos_reason: tx_logs の reason in reasons の **正の合計**(=払戻し)
    """
    id: str
    title: str
    reasons: tuple[str, ...]
    kind: Kind
    target: int
    reward: int


# プールから seeded random で3個選ぶ。新規追加するときはここに足す。
CHALLENGES: list[Challenge] = [
    Challenge("slot_play_5",      "🎰 スロットを5回プレイ",
              ("slot_bet",),                "count_reason", 5,   500),
    Challenge("slot_play_15",     "🎰 スロットを15回プレイ",
              ("slot_bet",),                "count_reason", 15,  1500),
    Challenge("bj_play_3",        "🃏 ブラックジャックを3回プレイ",
              ("blackjack_bet",),           "count_reason", 3,   600),
    Challenge("bj_play_10",       "🃏 ブラックジャックを10回プレイ",
              ("blackjack_bet",),           "count_reason", 10,  1500),
    Challenge("hilo_play_3",      "📈 ハイローを3回プレイ",
              ("hilo_bet",),                "count_reason", 3,   500),
    Challenge("chinchiro_play_3", "🎲 チンチロを3回プレイ",
              ("chinchiro_bet",),           "count_reason", 3,   500),
    Challenge("pvp_play_2",       "⚔️ PVPゲームに2回参加",
              ("pvp_escrow",),              "count_reason", 2,   800),
    Challenge("bet_volume_5k",    "💰 合計5,000以上を賭ける",
              ("slot_bet", "chinchiro_bet", "hilo_bet",
               "blackjack_bet", "blackjack_double", "blackjack_split",
               "pvp_escrow"),
              "sum_neg_reason", 5000,                              700),
    Challenge("bet_volume_20k",   "💰 合計20,000以上を賭ける",
              ("slot_bet", "chinchiro_bet", "hilo_bet",
               "blackjack_bet", "blackjack_double", "blackjack_split",
               "pvp_escrow"),
              "sum_neg_reason", 20000,                             2000),
    Challenge("payout_2k",        "🎯 払戻しを合計2,000以上",
              ("slot_win", "chinchiro_win", "hilo_win",
               "blackjack_win", "pvp_win"),
              "sum_pos_reason", 2000,                              500),
    Challenge("payout_10k",       "🎯 払戻しを合計10,000以上",
              ("slot_win", "chinchiro_win", "hilo_win",
               "blackjack_win", "pvp_win"),
              "sum_pos_reason", 10000,                             1500),
    Challenge("jackpot_chase",    "💎 スロットJP を狙え(15回回す)",
              ("slot_bet",),                "count_reason", 15,   800),
]


def _today() -> str:
    """UTC基準のYYYY-MM-DD。日界の判定はサーバー時刻揺らぎを避けてUTCで統一。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def pick_today(user_id: int) -> list[Challenge]:
    """user_id + 日付で seeded に3個を選ぶ(決定論)。"""
    key = f"{user_id}:{_today()}".encode()
    h = hashlib.sha256(key).digest()
    # 32バイトの先頭からインデックスを取り、CHALLENGES の長さで mod。重複は除外。
    picks: list[Challenge] = []
    used: set[int] = set()
    i = 0
    while len(picks) < 3 and i < len(h):
        idx = h[i] % len(CHALLENGES)
        i += 1
        if idx in used:
            continue
        used.add(idx)
        picks.append(CHALLENGES[idx])
    return picks


async def _progress_for(db, user_id: int, c: Challenge) -> int:
    """指定チャレンジの**当日進捗値**を tx_logs から計算する。"""
    reasons_sql = ",".join("?" for _ in c.reasons)
    params = (user_id, *c.reasons)
    # 'today' は UTC の同日範囲
    where_date = "ts >= strftime('%Y-%m-%dT00:00:00.000Z','now')"
    if c.kind == "count_reason":
        sql = (f"SELECT COUNT(*) v FROM tx_logs WHERE user_id=? "
               f"AND reason IN ({reasons_sql}) AND {where_date}")
    elif c.kind == "sum_neg_reason":
        sql = (f"SELECT COALESCE(-SUM(delta),0) v FROM tx_logs WHERE user_id=? "
               f"AND reason IN ({reasons_sql}) AND delta < 0 AND {where_date}")
    else:  # sum_pos_reason
        sql = (f"SELECT COALESCE(SUM(delta),0) v FROM tx_logs WHERE user_id=? "
               f"AND reason IN ({reasons_sql}) AND delta > 0 AND {where_date}")
    row = await (await db.conn.execute(sql, params)).fetchone()
    return int(row["v"])


async def _already_claimed(db, user_id: int, cid: str) -> bool:
    cur = await db.conn.execute(
        "SELECT 1 FROM claimed_challenges WHERE user_id=? AND date=? AND challenge_id=?",
        (user_id, _today(), cid),
    )
    return (await cur.fetchone()) is not None


async def _mark_claimed(db, user_id: int, c: Challenge) -> None:
    await db.conn.execute(
        "INSERT INTO claimed_challenges (user_id, date, challenge_id, reward) "
        "VALUES (?, ?, ?, ?)",
        (user_id, _today(), c.id, c.reward),
    )
    await db.conn.commit()


# ───────────────────────── View ─────────────────────────
class ChallengePanel(discord.ui.View):
    def __init__(self, cog: "ChallengesCog", user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他人のチャレンジは操作できません。", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        embed = await self.cog.build_embed(self.user_id)
        # ボタンを再生成
        self.clear_items()
        await self.cog._add_claim_buttons(self, self.user_id)
        await interaction.response.edit_message(embed=embed, view=self)


# ───────────────────────── Cog ─────────────────────────
class ChallengesCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    def enabled(self) -> bool:
        return bool(self.bot.db.setting("challenges_enabled", True))

    async def build_embed(self, user_id: int) -> discord.Embed:
        db = self.bot.db
        e = common.embed(
            "🗓️ 今日のチャレンジ",
            "条件を満たしたら『受取』ボタンで報酬をゲット。\n"
            "(日付はUTC基準で切り替わります)",
            color=common.COLOR_INFO,
        )
        if not self.enabled():
            e.description = "🛑 デイリーチャレンジは現在停止中です。"
            return e
        for c in pick_today(user_id):
            progress = await _progress_for(db, user_id, c)
            done = progress >= c.target
            claimed = await _already_claimed(db, user_id, c.id)
            if claimed:
                status = "✅ 受取済"
            elif done:
                status = "🎁 達成! 受取可"
            else:
                status = f"進捗 **{min(progress, c.target):,} / {c.target:,}**"
            e.add_field(
                name=f"{c.title}  (報酬: {c.reward:,})",
                value=status,
                inline=False,
            )
        return e

    async def _add_claim_buttons(
        self, view: discord.ui.View, user_id: int
    ) -> None:
        """達成済み&未受取のチャレンジに対し『受取』ボタンを動的に追加。"""
        if not self.enabled():
            return
        db = self.bot.db
        for c in pick_today(user_id):
            if await _already_claimed(db, user_id, c.id):
                continue
            if await _progress_for(db, user_id, c) < c.target:
                continue
            view.add_item(_ClaimButton(self, c))

    async def entry(self, interaction: discord.Interaction) -> None:
        view = ChallengePanel(self, interaction.user.id)
        embed = await self.build_embed(interaction.user.id)
        await self._add_claim_buttons(view, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="チャレンジ", description="今日のデイリーチャレンジを表示")
    async def cmd(self, interaction: discord.Interaction) -> None:
        await self.entry(interaction)


class _ClaimButton(discord.ui.Button):
    def __init__(self, cog: ChallengesCog, ch: Challenge) -> None:
        super().__init__(
            label=f"受取: {ch.title[:60]} (+{ch.reward:,})",
            emoji="🎁",
            style=discord.ButtonStyle.success,
        )
        self._cog = cog
        self._ch = ch

    async def callback(self, interaction: discord.Interaction) -> None:
        db = self._cog.bot.db
        c = self._ch
        user_id = interaction.user.id
        # 再確認(連打 / 別端末から二重受取の阻止)
        if await _already_claimed(db, user_id, c.id):
            await interaction.response.send_message(
                "既に受取済みです。", ephemeral=True
            )
            return
        if await _progress_for(db, user_id, c) < c.target:
            await interaction.response.send_message(
                "達成条件を満たしていません。", ephemeral=True
            )
            return
        async with db.user_lock(user_id):
            await db.adjust_balance(user_id, c.reward, "challenge_reward")
            await _mark_claimed(db, user_id, c)
        # パネル更新
        view: ChallengePanel = self.view  # type: ignore[assignment]
        await view.refresh(interaction)


async def setup(bot) -> None:
    await bot.add_cog(ChallengesCog(bot))
