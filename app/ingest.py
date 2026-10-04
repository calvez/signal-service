"""Storing what the EA sends, shared by the HTTP endpoints and the file spool (app/spool.py)."""

import time

from app import db
from app.config import Settings
from app.models import BarsPayload, HeartbeatPayload
from app.timeconv import server_to_utc


class UnknownSymbol(ValueError):
    pass


def store_bars(settings: Settings, payload: BarsPayload, now: int | None = None) -> list[dict]:
    """Upsert the bars (idempotent on symbol, tf, t_utc) and remember the symbol's digits.
    Returns the stored rows. Raises UnknownSymbol for a symbol that is not in config.yaml."""
    if payload.symbol not in settings.config.symbols:
        raise UnknownSymbol(f"unknown symbol {payload.symbol!r}")
    mode = settings.config.server_time_mode
    now = int(time.time()) if now is None else now
    rows = [
        {
            "symbol": payload.symbol,
            "tf": payload.timeframe,
            "t_server": b.t,
            "t_utc": server_to_utc(b.t, mode),
            "o": b.o,
            "h": b.h,
            "l": b.l,
            "c": b.c,
            "tv": b.tv,
            "sp": b.sp,
            "received_at": now,
        }
        for b in payload.bars
    ]
    conn = db.connect(settings.db_path)
    try:
        db.upsert_bars(conn, rows)
        db.upsert_symbol_meta(conn, payload.symbol, payload.digits)
    finally:
        conn.close()
    return rows


def read_candidate(settings: Settings, payload: BarsPayload, rows: list[dict]) -> int | None:
    """Open time (UTC) of the bar to run a market read on, or None. Only M5 bars of traded
    symbols qualify, and only the newest bar of a batch (a backfill is then skipped as stale)."""
    sym = settings.config.symbols.get(payload.symbol)
    if payload.timeframe != "M5" or sym is None or sym.role != "traded" or not rows:
        return None
    return max(r["t_utc"] for r in rows)


def store_heartbeat(settings: Settings, payload: HeartbeatPayload, received_at: int) -> None:
    row = payload.model_dump(exclude={"schema_version"})
    row["connected"] = int(row["connected"])
    row["trade_allowed"] = int(row["trade_allowed"])
    row["received_at"] = received_at
    conn = db.connect(settings.db_path)
    try:
        db.insert_heartbeat(conn, row)
    finally:
        conn.close()
