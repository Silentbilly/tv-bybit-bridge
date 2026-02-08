from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from redis.asyncio import Redis

from .config import settings
from .schemas import TVPayload
from .dedup import dedup_once, dedup_key, ttl_for_action
from .bybit_client import BybitV5


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bybit = BybitV5()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield
    finally:
        await app.state.bybit.aclose()
        await app.state.redis.aclose()


app = FastAPI(title="TradingView → Bybit (entries + soft-exit)", lifespan=lifespan)


@app.post("/tv/webhook")
async def tv_webhook(payload: TVPayload, request: Request):
    # 1. Безопасность и allowlist
    if payload.key != settings.tv_webhook_secret:
        raise HTTPException(status_code=401, detail="bad key")

    if not settings.allowed(payload.symbol):
        raise HTTPException(status_code=403, detail="symbol not allowed")

    # 2. Dedup по (action, symbol, bar_index|time)
    r: Redis = request.app.state.redis
    uniq_event = str(payload.bar_index) if payload.bar_index is not None else (payload.time or "")
    if not uniq_event:
        raise HTTPException(status_code=400, detail="bar_index or time required for dedup")

    k = dedup_key(payload.action, payload.symbol, uniq_event)
    ttl = ttl_for_action(payload.action)
    if not await dedup_once(r, k, ttl):
        return {"ok": True, "dedup": True}

    bybit: BybitV5 = request.app.state.bybit

    # Нормализуем action для удобства
    act = (payload.action or "").upper().strip()

    # 3. Soft-exit (как было)
    if act in ("SOFT_EXIT_LONG", "SOFT_EXIT_SHORT"):
        res = await bybit.close_position_market_reduce_only(payload.symbol)
        return {"ok": True, "bybit": res}

    # 4. Входы: поддерживаем ENTER_* и BUY/SELL из {{strategy.order.action}}
    #    - ENTER_LONG / BUY  -> LONG
    #    - ENTER_SHORT / SELL -> SHORT
    is_long_enter = act in ("ENTER_LONG", "BUY")
    is_short_enter = act in ("ENTER_SHORT", "SELL")

    if is_long_enter or is_short_enter:
        direction = "LONG" if is_long_enter else "SHORT"
        desired_side = "Buy" if direction == "LONG" else "Sell"

        cur_side, cur_size = await bybit.get_position_side_size(payload.symbol)

        # flip: если позиция в другую сторону — закрыть и дождаться flat
        if cur_side and cur_size > 0 and cur_side != desired_side:
            close_res = await bybit.close_if_open(payload.symbol)
            flat = await bybit.wait_flat(payload.symbol, attempts=12, delay_sec=0.25)
            if not flat:
                return {
                    "ok": False,
                    "error": "position_not_flat_after_close",
                    "close": close_res,
                }

        # если уже в нужную сторону — по настройке игнор (чтобы не усреднять)
        cur_side, cur_size = await bybit.get_position_side_size(payload.symbol)
        if cur_side and cur_size > 0 and not settings.enter_if_position_open:
            return {
                "ok": True,
                "skipped": True,
                "reason": "position_already_open",
                "side": cur_side,
                "size": cur_size,
            }

        qty = settings.qty_for(payload.symbol)
        qty = await bybit.normalize_qty(payload.symbol, qty)
        open_res = await bybit.open_position_market(payload.symbol, direction=direction, qty=qty)
        return {"ok": True, "opened": open_res, "qty": qty, "direction": direction}

    # 5. Всё остальное игнорируем
    return {"ok": True, "ignored": True, "action": act}
