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
    # 1. Безопасность
    if payload.key != settings.tv_webhook_secret:
        raise HTTPException(status_code=401, detail="bad key")

    # TV тикер → биржевой тикер
    symbol = settings.map_symbol(payload.symbol)

    # 2. Allowlist
    if not settings.allowed(symbol):
        raise HTTPException(status_code=403, detail="symbol not allowed")

    # 3. Нормализуем action
    act = (payload.action or "").upper().strip()
    if not act:
        return {"ok": True, "ignored": True, "reason": "empty_action"}

    # 4. Dedup по (action, symbol, time)
    r: Redis = request.app.state.redis
    uniq_event: str = (payload.time or "").strip()
    if not uniq_event:
        raise HTTPException(status_code=400, detail="time required for dedup")

    bar_index = payload.bar_index  # int | None
    if bar_index is not None:
        uniq_event = f"{uniq_event}:{bar_index}"

    k = dedup_key(act, symbol, uniq_event)
    ttl = ttl_for_action(act)
    if not await dedup_once(r, k, ttl):
        return {"ok": True, "dedup": True}

    bybit: BybitV5 = request.app.state.bybit

    # 5. Soft-exit по SOFT_EXIT_*
    if act in ("SOFT_EXIT_LONG", "SOFT_EXIT_SHORT"):
        res = await bybit.close_position_market_reduce_only(symbol)
        return {"ok": True, "bybit": res, "action": act, "symbol": symbol}

    # 5.1. Перенос стопа в безубыток (MOVE_SL_BE_*)
    if act in ("MOVE_SL_BE_LONG", "MOVE_SL_BE_SHORT"):
        # Новый уровень стопа обязателен
        sl = payload.sl
        if sl is None:
            raise HTTPException(status_code=400, detail="sl is required for MOVE_SL_BE_*")

        try:
            sl_f = float(sl)
        except ValueError:
            raise HTTPException(status_code=400, detail="sl must be numeric for MOVE_SL_BE_*")

        # Проверяем, что есть открытая позиция в нужную сторону
        cur_side, cur_size = await bybit.get_position_side_size(symbol)
        if cur_size <= 0:
            return {"ok": False, "error": "no_open_position_for_move_sl", "symbol": symbol}

        # Для LONG позиция должна быть Buy, для SHORT — Sell (односторонний режим)
        desired_side = "Buy" if act == "MOVE_SL_BE_LONG" else "Sell"
        if cur_side != desired_side:
            return {
                "ok": False,
                "error": "position_side_mismatch_for_move_sl",
                "expected": desired_side,
                "actual": cur_side,
                "symbol": symbol,
            }

        # Обновляем только стоп-лосс, TP оставляем как есть (None)
        tpsl_res = await bybit.set_trading_stop_full_linear(
            symbol=symbol,
            take_profit=None,
            stop_loss=str(sl_f),
            tp_trigger_by="LastPrice",
            sl_trigger_by="LastPrice",
            position_idx=0,
        )

        return {
            "ok": True,
            "action": act,
            "symbol": symbol,
            "new_sl": str(sl_f),
            "tpsl": tpsl_res,
        }

    # 6. Входы: ENTER_LONG / ENTER_SHORT из {{strategy.order.alert_message}}
    is_long_enter = act == "ENTER_LONG"
    is_short_enter = act == "ENTER_SHORT"

    if is_long_enter or is_short_enter:
        direction = "LONG" if is_long_enter else "SHORT"
        desired_side = "Buy" if direction == "LONG" else "Sell"

        # NEW: take SL/TP from payload
        sl = payload.sl
        tp = payload.tp
        if sl is None or tp is None:
            raise HTTPException(status_code=400, detail="sl and tp are required for ENTER_*")

        # sanity check "side correctness"
        try:
            sl_f = float(sl)
            tp_f = float(tp)
        except ValueError:
            raise HTTPException(status_code=400, detail="sl/tp must be numeric")

        last_price = payload.price  # optional; may be None
        if direction == "LONG" and not (sl_f < tp_f):
            raise HTTPException(status_code=400, detail="for LONG expected sl < tp")
        if direction == "SHORT" and not (sl_f > tp_f):
            raise HTTPException(status_code=400, detail="for SHORT expected sl > tp")

        cur_side, cur_size = await bybit.get_position_side_size(symbol)

        # flip: если позиция в другую сторону — закрыть и дождаться flat
        if cur_side and cur_size > 0 and cur_side != desired_side:
            close_res = await bybit.close_if_open(symbol)
            flat = await bybit.wait_flat(symbol, attempts=12, delay_sec=0.25)
            if not flat:
                return {"ok": False, "error": "position_not_flat_after_close", "close": close_res, "symbol": symbol}

        # если уже в нужную сторону — по настройке игнор (чтобы не усреднять)
        cur_side, cur_size = await bybit.get_position_side_size(symbol)
        if cur_side and cur_size > 0 and not settings.enter_if_position_open:
            return {"ok": True, "skipped": True, "reason": "position_already_open", "side": cur_side, "size": cur_size, "symbol": symbol}

        qty = settings.qty_for(symbol)
        qty = await bybit.normalize_qty(symbol, qty)

        open_res = await bybit.open_position_market(symbol, direction=direction, qty=qty)

        # wait until position is really open, then set TP/SL
        ok_pos, pos = await bybit.wait_position_open(symbol, desired_side=desired_side, attempts=12, delay_sec=0.25)
        if not ok_pos:
            return {
                "ok": False,
                "error": "position_not_open_after_entry_ack",
                "opened": open_res,
                "symbol": symbol,
            }

        tpsl_res = await bybit.set_trading_stop_full_linear(
            symbol=symbol,
            take_profit=str(tp),
            stop_loss=str(sl),
            tp_trigger_by="LastPrice",
            sl_trigger_by="LastPrice",
            position_idx=0,  # one-way
        )

        return {
            "ok": True,
            "opened": open_res,
            "tpsl": tpsl_res,
            "qty": qty,
            "direction": direction,
            "sl": str(sl),
            "tp": str(tp),
            "action": act,
            "symbol": symbol,
        }

    # 7. Всё остальное игнорируем
    return {"ok": True, "ignored": True, "action": act, "symbol": symbol}
