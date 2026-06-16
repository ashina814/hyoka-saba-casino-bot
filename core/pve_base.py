"""PVEゲーム共通の「ベット引き落とし前処理」。

各PVEゲーム(スロット/チンチロ/ハイロー/ブラックジャック)の `_start` は
同じパターンを繰り返していた:

    1. 自己制限ガード(/プロフィール 🛡️制限 の上限チェック)
    2. user_lock を取り、凍結チェック → ベット引き落とし(adjust_balance)
       残高不足は InsufficientFunds で拒否
    3. (スロットのみ) ベットの一部をスロットJPへ積立
    4. 直近ベット記憶(set_last_bet) ← /プロフィール 連戦のdefault反映
    5. 全体JP積立&当選判定(global_jackpot.hook_pve_bet)

これを 1関数 `take_bet()` に集約し、各 cog からは Bool 戻り値で
継続判断するだけにする。失敗時のエラーメッセージは内部で送信済みなので、
呼び出し側は単に return すればよい。
"""
from __future__ import annotations

import discord

from core import global_jackpot as _gjp
from db.dao import InsufficientFunds
from ui import common as _common


async def take_bet(
    bot,
    interaction: discord.Interaction,
    user_id: int,
    bet: int,
    *,
    reason: str,
    game_key: str,
    extra_contrib: int = 0,
) -> bool:
    """ベット引き落とし + 周辺処理を一括実行。

    引数:
        bot: CasinoBot
        interaction: 失敗時のエラー応答先(ephemeral)
        user_id: プレイヤーID
        bet: 賭け額(正の整数)
        reason: tx_logs に記録する reason ('slot_bet' など)
        game_key: last_bets に記録する game ('slot' など)
        extra_contrib: スロットJP等への追加積立額(ゲーム固有のシンク)

    戻り値:
        True  … 引き落とし成功。呼び出し側はゲーム本体に進む
        False … 失敗。エラー応答は本関数内で済んでいるので、ただ return すればよい
    """
    # 1. 自己制限ガード(内部で respond_with 済み)
    if await _common.self_limit_guard(interaction, bet):
        return False

    db = bot.db
    async with db.user_lock(user_id):
        # 2a. 凍結チェック
        if await db.is_frozen(user_id):
            await _common.respond_with(
                interaction, content="🧊 あなたは凍結中です。", ephemeral=True,
            )
            return False
        # 2b. 残高引き落とし
        try:
            await db.adjust_balance(user_id, -bet, reason)
        except InsufficientFunds:
            await _common.respond_with(
                interaction, content="残高が足りません。", ephemeral=True,
            )
            return False
        # 3. ゲーム固有の追加積立(スロットJPなど)
        if extra_contrib > 0:
            await db.jackpot_add(extra_contrib)

    # 4. last_bet 記憶
    try:
        await db.set_last_bet(user_id, game_key, bet)
    except Exception:  # noqa: BLE001
        pass

    # 5. 全体JP積立&当選判定(横串)
    await _gjp.hook_pve_bet(bot, user_id, bet)
    return True
