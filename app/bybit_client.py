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

        # One shared client = connection pooling / keep-alive [page:11]
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self.client.aclose()  # explicit close is supported [page:11]

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        params = params or {}
        url = self.base_url + path

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
            return r.json()

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
        return r.json()

    # ----------------------------
    # Positions (single source)
    # ----------------------------
    async def get_position(self, symbol: str) -> dict:
        data = await self._request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
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

    async def get_position_size(self, symbol: str) -> float:
        _, size = await self.get_position_side_size(symbol)
        return size

    # ----------------------------
    # Orders
    # ----------------------------
    async def place_market(self, symbol: str, side: str, qty: str, reduce_only: bool = False) -> dict:
        return await self._request("POST", "/v5/order/create", {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "reduceOnly": bool(reduce_only),
        })

    async def open_position_market(self, symbol: str, direction: str, qty: str) -> dict:
        side = "Buy" if direction == "LONG" else "Sell"
        return await self.place_market(symbol=symbol, side=side, qty=qty, reduce_only=False)

    async def close_position_market_reduce_only(self, symbol: str) -> dict:
        pos = await self.get_position(symbol)
        if not pos:
            return {"ok": True, "skipped": True, "reason": "no_position_data"}

        pos_side, size = self._side_size_from_pos(pos)
        if pos_side == "" or size <= 0:
            return {"ok": True, "skipped": True, "reason": "no_open_position"}

        close_side = "Sell" if pos_side == "Buy" else "Buy"
        return await self.place_market(symbol=symbol, side=close_side, qty=str(size), reduce_only=True)

    async def close_if_open(self, symbol: str) -> dict:
        return await self.close_position_market_reduce_only(symbol)

    async def wait_flat(self, symbol: str, attempts: int = 10, delay_sec: float = 0.3) -> bool:
        for _ in range(attempts):
            side, size = await self.get_position_side_size(symbol)
            if not side or size == 0:
                return True
            await asyncio.sleep(delay_sec)
        return False

    # ----------------------------
    # Qty normalization
    # ----------------------------
    async def get_instrument_filters(self, symbol: str) -> tuple[float, float]:
        data = await self._request("GET", "/v5/market/instruments-info", {
            "category": "linear",
            "symbol": symbol,
        })
        lst = (data.get("result") or {}).get("list") or []
        if not lst:
            return 0.0, 0.0
        lot = (lst[0].get("lotSizeFilter") or {})
        min_qty = float(lot.get("minOrderQty") or 0.0)
        step = float(lot.get("qtyStep") or 0.0)
        return min_qty, step

    async def normalize_qty(self, symbol: str, qty: str) -> str:
        q = float(qty)
        min_qty, step = await self.get_instrument_filters(symbol)
        if min_qty and q < min_qty:
            raise ValueError(f"qty {q} < minOrderQty {min_qty}")
        if step and step > 0:
            q = math.floor(q / step) * step
        if q <= 0:
            raise ValueError("qty normalized to 0")
        return str(q)
