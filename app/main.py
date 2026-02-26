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


def _to_float(v: object, field: str) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be numeric")


@app.post("/tv/webhook")
async def tv_webhook(payload: TVPayload, request: Request):
    # 1. Безопасность
    if payload.key != settings.tv_webhook_secret:
        raise HTTPException(status_code=401, detail="bad key")

    raw_symbol = payload.symbol  # уже нормализован в TVPayload.normalize_symbol
    symbol = settings.map_symbol(raw_symbol)
    mult = settings.price_mult(raw_symbol)

    # 2. Allowlist (проверяем уже биржевой символ)
    if not settings.allowed(symbol):
        raise HTTPException(status_code=403, detail=f"symbol not allowed: {symbol}")

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
        return {
            "ok": True,
            "bybit": res,
            "action": act,
            "raw_symbol": raw_symbol,
            "symbol": symbol,
        }

    # 5.1. Перенос стопа в безубыток (MOVE_SL_BE_*)
    if act in ("MOVE_SL_BE_LONG", "MOVE_SL_BE_SHORT"):
        sl = payload.sl
        if sl is None:
            raise HTTPException(status_code=400, detail="sl is required for MOVE_SL_BE_*")

        sl_f = _to_float(sl, "sl") * mult

        cur_side, cur_size = await bybit.get_position_side_size(symbol)
        if cur_size <= 0:
            return {"ok": False, "error": "no_open_position_for_move_sl", "symbol": symbol}

        desired_side = "Buy" if act == "MOVE_SL_BE_LONG" else "Sell"
        if cur_side != desired_side:
            return {
                "ok": False,
                "error": "position_side_mismatch_for_move_sl",
                "expected": desired_side,
                "actual": cur_side,
                "raw_symbol": raw_symbol,
                "symbol": symbol,
            }

        tpsl_res = await bybit.set_trading_stop_full_linear(
            symbol=symbol,
            take_profit=None,
            stop_loss=str(sl_f),
            tp_trigger_by="LastPrice",
            sl_trigger_by="LastPrice",
            position_idx=0,
        )

        # не молчим, если Bybit отклонил запрос
        if (tpsl_res or {}).get("retCode") not in (0, "0", None):
            return {
                "ok": False,
                "error": "tpsl_failed",
                "tpsl": tpsl_res,
                "raw_symbol": raw_symbol,
                "symbol": symbol,
                "mult": mult,
                "sl_sent": str(sl_f),
            }

        return {
            "ok": True,
            "action": act,
            "raw_symbol": raw_symbol,
            "symbol": symbol,
            "mult": mult,
            "new_sl": str(sl_f),
            "tpsl": tpsl_res,
        }

    # 6. Входы: ENTER_LONG / ENTER_SHORT
    is_long_enter = act == "ENTER_LONG"
    is_short_enter = act == "ENTER_SHORT"

    if is_long_enter or is_short_enter:
        direction = "LONG" if is_long_enter else "SHORT"
        desired_side = "Buy" if direction == "LONG" else "Sell"

        sl = payload.sl
        tp = payload.tp
        if sl is None or tp is None:
            raise HTTPException(status_code=400, detail="sl and tp are required for ENTER_*")

        sl_raw = _to_float(sl, "sl")
        tp_raw = _to_float(tp, "tp")

        # sanity check по "сырому" TV (логика направления)
        if direction == "LONG" and not (sl_raw < tp_raw):
            raise HTTPException(status_code=400, detail="for LONG expected sl < tp")
        if direction == "SHORT" and not (sl_raw > tp_raw):
            raise HTTPException(status_code=400, detail="for SHORT expected sl > tp")

        # пересчёт под биржевой контракт (1000/10000)
        sl_sent = sl_raw * mult
        tp_sent = tp_raw * mult

        cur_side, cur_size = await bybit.get_position_side_size(symbol)

        # flip: если позиция в другую сторону — закрыть и дождаться flat
        if cur_side and cur_size > 0 and cur_side != desired_side:
            close_res = await bybit.close_if_open(symbol)
            flat = await bybit.wait_flat(symbol, attempts=12, delay_sec=0.25)
            if not flat:
                return {
                    "ok": False,
                    "error": "position_not_flat_after_close",
                    "close": close_res,
                    "raw_symbol": raw_symbol,
                    "symbol": symbol,
                }

        # если уже в нужную сторону — по настройке игнор
        cur_side, cur_size = await bybit.get_position_side_size(symbol)
        if cur_side and cur_size > 0 and not settings.enter_if_position_open:
            return {
                "ok": True,
                "skipped": True,
                "reason": "position_already_open",
                "side": cur_side,
                "size": cur_size,
                "raw_symbol": raw_symbol,
                "symbol": symbol,
            }

        qty = settings.qty_for(symbol)
        qty = await bybit.normalize_qty(symbol, qty)

        open_res = await bybit.open_position_market(symbol, direction=direction, qty=qty)

        ok_pos, pos = await bybit.wait_position_open(symbol, desired_side=desired_side, attempts=12, delay_sec=0.25)
        if not ok_pos:
            return {
                "ok": False,
                "error": "position_not_open_after_entry_ack",
                "opened": open_res,
                "raw_symbol": raw_symbol,
                "symbol": symbol,
            }

        tpsl_res = await bybit.set_trading_stop_full_linear(
            symbol=symbol,
            take_profit=str(tp_sent),
            stop_loss=str(sl_sent),
            tp_trigger_by="LastPrice",
            sl_trigger_by="LastPrice",
            position_idx=0,
        )

        if (tpsl_res or {}).get("retCode") not in (0, "0", None):
            return {
                "ok": False,
                "error": "tpsl_failed",
                "opened": open_res,
                "tpsl": tpsl_res,
                "raw_symbol": raw_symbol,
                "symbol": symbol,
                "mult": mult,
                "sl_raw": str(sl_raw),
                "tp_raw": str(tp_raw),
                "sl_sent": str(sl_sent),
                "tp_sent": str(tp_sent),
            }

        return {
            "ok": True,
            "opened": open_res,
            "tpsl": tpsl_res,
            "qty": qty,
            "direction": direction,
            "raw_symbol": raw_symbol,
            "symbol": symbol,
            "mult": mult,
            "sl": str(sl_raw),
            "tp": str(tp_raw),
            "sl_sent": str(sl_sent),
            "tp_sent": str(tp_sent),
            "action": act,
        }

    # 7. Всё остальное игнорируем
    return {"ok": True, "ignored": True, "action": act, "raw_symbol": raw_symbol, "symbol": symbol}
