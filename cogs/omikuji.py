"""おみくじ。1日1回引ける運勢(フレーバー+小ボーナス)。

設計:
- ユーザー × 日付(UTC) で決定論的に結果を選ぶ。同じ日に2回押しても同じ。
- omikuji_claimed テーブルで「今日引いたか」を判定。
  → 受取済みなら結果だけ表示、未受取ならボーナス付与+結果記録。
- 大吉だけ +300 の小ボーナス(経済中立、シンク影響は微小)。
  他はフレーバーのみで景品なし。
- ハブパネルに🎴ボタンとして配置。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import discord
from discord.ext import commands

from ui import common


# (label, weight, bonus, message)
# weight は相対重み。合計で正規化される。
RESULTS: list[tuple[str, int, int, str]] = [
    ("大吉", 5,  300, "🌟 今日のあなたに **大いなる幸運**。きっと良い日になる！"),
    ("中吉", 12, 0,   "✨ 程よい運気。挑戦してみると良いかも。"),
    ("小吉", 18, 0,   "🍀 落ち着いて過ごせば穏やかな1日。"),
    ("吉",   25, 0,   "🌿 普通の日。地道に積み上げを。"),
    ("末吉", 20, 0,   "🌱 後半に上向く運気。焦らずに。"),
    ("凶",   15, 0,   "🌧️ ひと休みも大事。深追いは禁物。"),
    ("大凶", 5,  0,   "⚠️ ベットは控えめに。明日に期待しよう。"),
]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _pick(user_id: int) -> tuple[str, int, str]:
    """user_id + 今日の日付で決定論的に1個選ぶ。(label, bonus, message)"""
    key = f"omikuji:{user_id}:{_today()}".encode()
    h = int.from_bytes(hashlib.sha256(key).digest()[:4], "big")
    total = sum(w for _, w, _, _ in RESULTS)
    r = h % total
    acc = 0
    for label, w, bonus, msg in RESULTS:
        acc += w
        if r < acc:
            return label, bonus, msg
    return RESULTS[-1][0], RESULTS[-1][2], RESULTS[-1][3]


class OmikujiCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def entry(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        user = interaction.user
        cur = await db.conn.execute(
            "SELECT result, bonus FROM omikuji_claimed WHERE user_id=? AND date=?",
            (user.id, _today()),
        )
        already = await cur.fetchone()
        label, bonus, msg = _pick(user.id)

        if already is None:
            # 初回: ボーナス付与+記録
            if bonus:
                async with db.user_lock(user.id):
                    await db.adjust_balance(user.id, bonus, "omikuji_bonus")
            await db.conn.execute(
                "INSERT INTO omikuji_claimed (user_id, date, result, bonus) "
                "VALUES (?, ?, ?, ?)",
                (user.id, _today(), label, bonus),
            )
            await db.conn.commit()
            # 大吉なら称号
            if label == "大吉":
                from core import badges as _badges
                await _badges.on_omikuji_oo(self.bot, user.id)
            e = self._embed(label, bonus, msg, just_drawn=True)
            await interaction.response.send_message(embed=e, ephemeral=True)
        else:
            # 同日: 結果再表示のみ
            e = self._embed(label, int(already["bonus"]), msg, just_drawn=False)
            e.set_footer(text="本日は既にこの結果を受け取り済みです。明日また引けます。")
            await interaction.response.send_message(embed=e, ephemeral=True)

    def _embed(self, label: str, bonus: int, msg: str,
               just_drawn: bool) -> discord.Embed:
        colors = {
            "大吉": common.COLOR_JACKPOT,
            "中吉": common.COLOR_WIN,
            "小吉": common.COLOR_INFO,
            "吉":   common.COLOR_INFO,
            "末吉": common.COLOR_INFO,
            "凶":   common.COLOR_LOSE,
            "大凶": common.COLOR_LOSE,
        }
        e = common.embed(
            f"🎴 今日のおみくじ — **{label}**",
            msg,
            color=colors.get(label, common.COLOR_MAIN),
        )
        if bonus and just_drawn:
            e.add_field(
                name="ボーナス",
                value=common.money(self.bot.cfg, bonus) + " を受け取りました！",
                inline=False,
            )
        return e


async def setup(bot) -> None:
    await bot.add_cog(OmikujiCog(bot))
