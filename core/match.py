"""PVP 共通ユーティリティ: マッチID生成と賭け金エスクロー。

PVP では参加時にベットを預かり(エスクロー)、勝敗で再分配する。
途中解散・Bot再起動時は返金する。active_match で二重参加を防ぐ。
"""
from __future__ import annotations

import secrets

from db.dao import Database, InsufficientFunds

_RNG = secrets.SystemRandom()


def new_match_id(game: str) -> str:
    return f"{game}-{_RNG.randrange(16**6):06x}"


async def escrow_take(db: Database, user_id: int, bet: int, match_id: str) -> bool:
    """参加者からベットを預かる。成功で True。残高不足/凍結で False。

    呼び出し側で user_lock を取得済みであること。
    """
    if await db.is_frozen(user_id):
        return False
    try:
        await db.adjust_balance(user_id, -bet, "pvp_escrow", match_id)
    except InsufficientFunds:
        return False
    await db.set_active_match(user_id, match_id)
    return True


async def escrow_refund(db: Database, user_id: int, bet: int, match_id: str) -> None:
    async with db.user_lock(user_id):
        await db.adjust_balance(user_id, bet, "pvp_refund", match_id)
        await db.set_active_match(user_id, None)


async def clear_active(db: Database, user_id: int) -> None:
    await db.set_active_match(user_id, None)
