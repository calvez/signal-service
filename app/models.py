"""Request/response models for the EA protocol (docs/protocol.md §1-3)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Bar(_Strict):
    t: int = Field(gt=0, description="bar open time, raw MT5 server epoch")
    o: float
    h: float
    l: float  # noqa: E741 — matches the wire format
    c: float
    tv: int = Field(ge=0)
    sp: int = Field(ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> "Bar":
        if self.h < max(self.o, self.c, self.l) or self.l > min(self.o, self.c, self.h):
            raise ValueError("inconsistent OHLC (high/low do not bound open/close)")
        return self


class BarsPayload(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    source: Literal["mt5"]
    account_login: int
    server: str
    symbol: str
    timeframe: Literal["M5", "H1", "D1"]
    digits: int = Field(ge=0, le=8)
    server_utc_offset_sec: int
    bars: list[Bar] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HeartbeatPayload(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    ea_version: str
    account_login: int
    server: str
    company: str
    balance: float
    equity: float
    connected: bool
    trade_allowed: bool
    positions: int = Field(ge=0)
    floating_pl: float
    currency: str
    time_server: int
    server_utc_offset_sec: int

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
