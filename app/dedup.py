from __future__ import annotations
from redis.asyncio import Redis
from .config import settings


def dedup_key(action: str, symbol: str, event_id: str) -> str:
    action = (action or "").upper().strip()
    symbol = (symbol or "").upper().strip()
    event_id = (event_id or "").strip()
    return f"{settings.dedup_prefix}:{action}:{symbol}:{event_id}"


def ttl_for_action(action: str) -> int:
    action = (action or "").upper()
    if action.startswith("ENTER_"):
        return int(settings.dedup_ttl_enter_sec)
    if action.startswith("SOFT_EXIT_"):
        return int(settings.dedup_ttl_exit_sec)
    return int(settings.dedup_ttl_default_sec)


async def dedup_once(r: Redis, key: str, ttl_sec: int) -> bool:
    ok = await r.set(name=key, value="1", nx=True, ex=int(ttl_sec))
    return bool(ok)
