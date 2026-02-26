import json
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tv_webhook_secret: str
    bybit_api_key: str
    bybit_api_secret: str
    bybit_base_url: str = "https://api.bybit.com"
    redis_url: str = "redis://localhost:6379/0"

    # Trading sizing / allowlist
    default_qty: str = "0.5"
    symbol_qty_map: str = "{}"
    symbol_whitelist: str = ""

    # Разрешать ли вход, если позиция уже открыта
    enter_if_position_open: bool = False

    # Dedup (from .env)
    dedup_ttl_enter_sec: int = 21600       # 6 hours
    dedup_ttl_exit_sec: int = 604800       # 7 days
    dedup_ttl_default_sec: int = 86400     # 24 hours
    dedup_prefix: str = "dedup:tv"

    # Маппинг тикеров TV → Bybit, JSON-строка
    # пример: {"PEPEUSDT":"1000PEPEUSDT","BONKUSDT":"1000BONKUSDT"}
    tv_to_bybit_symbol_map: str = "{}"

    # Мультипликатор цены для TV → Bybit (для 1000/10000 контрактов), JSON-строка
    # пример: {"PEPEUSDT":1000,"BONKUSDT":1000}
    tv_to_bybit_price_mult_map: str = "{}"

    def qty_for(self, symbol: str) -> str:
        m = json.loads(self.symbol_qty_map or "{}")
        return str(m.get(symbol, self.default_qty))

    def allowed(self, symbol: str) -> bool:
        if not self.symbol_whitelist:
            return True
        allowed = {s.strip().upper() for s in self.symbol_whitelist.split(",") if s.strip()}
        return str(symbol or "").upper() in allowed

    def map_symbol(self, tv_symbol: str) -> str:
        """
        TV symbol (после нормализации) -> биржевой символ.
        Например: PEPEUSDT -> 1000PEPEUSDT.
        """
        try:
            m = json.loads(self.tv_to_bybit_symbol_map or "{}")
        except json.JSONDecodeError:
            m = {}

        s = str(tv_symbol or "").upper().strip()
        return str(m.get(s, s))

    def price_mult(self, tv_symbol: str) -> float:
        """
        Мультипликатор цены для тикеров, которые маппятся на 1000/10000 контракты.
        Если не задан — 1.
        """
        try:
            m = json.loads(self.tv_to_bybit_price_mult_map or "{}")
        except json.JSONDecodeError:
            m = {}

        s = str(tv_symbol or "").upper().strip()
        v = m.get(s, 1)
        try:
            return float(v)
        except (TypeError, ValueError):
            return 1.0

    class Config:
        env_file = ".env"


settings = Settings()
