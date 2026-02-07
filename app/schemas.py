from pydantic import BaseModel, Field, field_validator


class TVPayload(BaseModel):
    key: str
    action: str
    symbol: str
    qty: str | float | None = None
    time: str | None = None
    bar_index: str | int | None = None
    price: str | float | None = None

    @field_validator("key", mode="before")
    @classmethod
    def normalize_key(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip().upper()

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, v: object) -> str:
        """
        Нормализуем типичные варианты:
        - "BYBIT:SOLUSDT" -> "SOLUSDT"
        - "SOLUSDT.P" -> "SOLUSDT"
        """
        if v is None:
            return ""
        s = str(v).strip().upper()

        if ":" in s:
            s = s.split(":", 1)[1]

        # убрать суффикс после точки (часто у perpetual/маркированных тикеров)
        if "." in s:
            s = s.split(".", 1)[0]

        return s

    @field_validator("bar_index", mode="before")
    @classmethod
    def normalize_bar_index(cls, v: object) -> int | None:
        if v is None:
            return None
        # уже int
        if isinstance(v, int):
            return v
        # float (в т.ч. 123.0)
        if isinstance(v, float):
            return int(v)

        s = str(v).strip()
        if not s:
            return None

        # "123" -> 123
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)

        # "123.0" -> 123
        try:
            f = float(s)
            return int(f)
        except ValueError:
            return None

    @field_validator("time", mode="before")
    @classmethod
    def normalize_time(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    # можно расширить позже: qty, exchange, strategy_id и т.д.
