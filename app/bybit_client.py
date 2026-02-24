import asyncio
import math
import time
import hmac
import hashlib
import json
import httpx
from typing import Any
from .config import settings


def _sign(prehash: str, secret: str) -> str:
    return hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()


class BybitV5:
    def __init__(self) -> None:
        self.base_url = settings.bybit_base_url.rstrip("/")
        self.key = settings.bybit_api_key
        self.secret = settings.bybit_api_secret
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        params = params or {}
        url = self.base_url + path

        def _rl_meta(resp: httpx.Response) -> dict[str, Any]:
            return {
                "x_bapi_limit": resp.headers.get("X-Bapi-Limit"),
                "x_bapi_limit_status": resp.headers.get("X-Bapi-Limit-Status"),
                "x_bapi_limit_reset_ts": resp.headers.get("X-Bapi-Limit-Reset-Timestamp"),
            }

        if method.upper() == "GET":
            query = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
            prehash = ts + self.key + recv_window + query
            sign = _sign(prehash, self.secret)
            headers = {
                "X-BAPI-API-KEY": self.key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": sign,
            }
            r = await self.client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            data["_rl"] = _rl_meta(r)
            return data

        body = json.dumps(params, separators=(",", ":"))
        prehash = ts + self.key + recv_window + body
        sign = _sign(prehash, self.secret)
        headers = {
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sign,
        }
        r = await self.client.post(url, content=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        data["_rl"] = _rl_meta(r)
        return data

    async def get_position(self, symbol: str) -> dict:
        symbol = settings.map_symbol(symbol)
        data = await self._request(
            "GET",
            "/v5/position/list",
            {"category": "linear", "symbol": symbol},
        )
        lst = (data.get("result") or {}).get("list") or []
        return lst[0] if lst else {}

    @staticmethod
    def _side_size_from_pos(pos: dict) -> tuple[str, float]:
        side = (pos.get("side") or "")
        size = float(pos.get("size") or 0.0)
        return side, size

    async def get_position_side_size(self, symbol: str) -> tuple[str, float]:
        pos = await self.get_position(symbol)
        return self._side_size_from_pos(pos)

    async def wait_position_open(
        self,
        symbol: str,
        desired_side: str,
        attempts: int = 12,
        delay_sec: float = 0.25,
    ) -> tuple[bool, dict]:
        symbol = settings.map_symbol(symbol)
        for _ in range(attempts):
            pos = await self.get_position(symbol)
            side, size = self._side_size_from_pos(pos)
            if side == desired_side and size > 0:
                return True, pos
            await asyncio.sleep(delay_sec)
        return False, {}

    async def place_market(
        self,
        symbol: str,
        side: str,
        qty: str,
        reduce_only: bool = False,
    ) -> dict:
        symbol = settings.map_symbol(symbol)
        return await self._request(
            "POST",
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "reduceOnly": bool(reduce_only),
            },
        )

    async def open_position_market(self, symbol: str, direction: str, qty: str) -> dict:
        symbol = settings.map_symbol(symbol)
        side = "Buy" if direction == "LONG" else "Sell"
        return await self.place_market(symbol=symbol, side=side, qty=qty, reduce_only=False)

    async def close_position_market_reduce_only(self, symbol: str) -> dict:
        symbol = settings.map_symbol(symbol)
        pos = await self.get_position(symbol)
        if not pos:
            return {"ok": True, "skipped": True, "reason": "no_position_data"}

        pos_side, size = self._side_size_from_pos(pos)
        if pos_side == "" or size <= 0:
            return {"ok": True, "skipped": True, "reason": "no_open_position"}

        close_side = "Sell" if pos_side == "Buy" else "Buy"
        return await self.place_market(
            symbol=symbol,
            side=close_side,
            qty=str(size),
            reduce_only=True,
        )

    async def close_if_open(self, symbol: str) -> dict:
        symbol = settings.map_symbol(symbol)
        return await self.close_position_market_reduce_only(symbol)

    async def wait_flat(self, symbol: str, attempts: int = 10, delay_sec: float = 0.3) -> bool:
        symbol = settings.map_symbol(symbol)
        for _ in range(attempts):
            side, size = await self.get_position_side_size(symbol)
            if not side or size == 0:
                return True
            await asyncio.sleep(delay_sec)
        return False

    # set TP/SL for linear position (Full mode, Market only in Full)
    async def set_trading_stop_full_linear(
        self,
        symbol: str,
        take_profit: str | None,
        stop_loss: str | None,
        tp_trigger_by: str = "LastPrice",
        sl_trigger_by: str = "LastPrice",
        position_idx: int = 0,
    ) -> dict:
        symbol = settings.map_symbol(symbol)
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": int(position_idx),
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "tpTriggerBy": tp_trigger_by,
            "slTriggerBy": sl_trigger_by,
        }
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)

        return await self._request("POST", "/v5/position/trading-stop", params)

    async def get_instrument_filters(self, symbol: str) -> tuple[float, float]:
        symbol = settings.map_symbol(symbol)
        data = await self._request(
            "GET",
            "/v5/market/instruments-info",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            return 0.0, 0.0
        lot = (lst[0].get("lotSizeFilter") or {})
        min_qty = float(lot.get("minOrderQty") or 0.0)
        step = float(lot.get("qtyStep") or 0.0)
        return min_qty, step

    async def normalize_qty(self, symbol: str, qty: str) -> str:
        symbol = settings.map_symbol(symbol)
        q = float(qty)
        min_qty, step = await self.get_instrument_filters(symbol)
        if min_qty and q < min_qty:
            raise ValueError(f"qty {q} < minOrderQty {min_qty}")
        if step and step > 0:
            q = math.floor(q / step) * step
        if q <= 0:
            raise ValueError("qty normalized to 0")
        return str(q)
