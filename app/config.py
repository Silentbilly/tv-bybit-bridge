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
    enter_if_position_open: bool = False

    # Dedup (from .env)
    dedup_ttl_enter_sec: int = 21600       # 6 hours
    dedup_ttl_exit_sec: int = 604800       # 7 days
    dedup_ttl_default_sec: int = 86400     # 24 hours
    dedup_prefix: str = "dedup:tv"

    def qty_for(self, symbol: str) -> str:
        m = json.loads(self.symbol_qty_map or "{}")
        return str(m.get(symbol, self.default_qty))

    def allowed(self, symbol: str) -> bool:
        if not self.symbol_whitelist:
            return True
        allowed = {s.strip() for s in self.symbol_whitelist.split(",") if s.strip()}
        return symbol in allowed

    class Config:
        env_file = ".env"


settings = Settings()
