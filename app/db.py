"""SQLite storage (WAL mode). All times are UTC epoch seconds (INTEGER) unless a column says
otherwise; `t_server` is MT5's raw server-clock epoch, kept for audit.

Look at times in the shell with:  select datetime(t_utc, 'unixepoch') from bars limit 5;
"""

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT    NOT NULL,
    tf          TEXT    NOT NULL,
    t_server    INTEGER NOT NULL,
    t_utc       INTEGER NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    tv          INTEGER NOT NULL,
    sp          INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (symbol, tf, t_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS heartbeats (
    id                    INTEGER PRIMARY KEY,
    received_at           INTEGER NOT NULL,
    ea_version            TEXT    NOT NULL,
    account_login         INTEGER NOT NULL,
    server                TEXT    NOT NULL,
    company               TEXT    NOT NULL,
    balance               REAL    NOT NULL,
    equity                REAL    NOT NULL,
    connected             INTEGER NOT NULL,
    trade_allowed         INTEGER NOT NULL,
    positions             INTEGER NOT NULL,
    floating_pl           REAL    NOT NULL,
    currency              TEXT    NOT NULL,
    time_server           INTEGER NOT NULL,
    server_utc_offset_sec INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS heartbeats_received ON heartbeats (received_at);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    ts_utc INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    detail TEXT    NOT NULL DEFAULT '{}'    -- JSON
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts_utc);
"""

UPSERT_BAR = """
INSERT INTO bars (symbol, tf, t_server, t_utc, o, h, l, c, tv, sp, received_at)
VALUES (:symbol, :tf, :t_server, :t_utc, :o, :h, :l, :c, :tv, :sp, :received_at)
ON CONFLICT (symbol, tf, t_utc) DO UPDATE SET
    t_server = excluded.t_server,
    o = excluded.o, h = excluded.h, l = excluded.l, c = excluded.c,
    tv = excluded.tv, sp = excluded.sp, received_at = excluded.received_at
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection. Cheap, so callers open one per request/job and close it."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_bars(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert or replace bars on (symbol, tf, t_utc). One transaction; returns the row count."""
    rows = list(rows)
    with conn:
        conn.executemany(UPSERT_BAR, rows)
    return len(rows)


def insert_heartbeat(conn: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with conn:
        conn.execute(f"INSERT INTO heartbeats ({cols}) VALUES ({params})", row)


def log_event(conn: sqlite3.Connection, kind: str, detail: dict | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO events (ts_utc, kind, detail) VALUES (?, ?, ?)",
            (int(time.time()), kind, json.dumps(detail or {}, ensure_ascii=False)),
        )


def load_bars(
    conn: sqlite3.Connection, symbol: str, tf: str, until_utc: int | None = None, limit: int = 2000
) -> pd.DataFrame:
    """The most recent `limit` bars (open time <= until_utc), oldest first.

    Index: tz-aware UTC DatetimeIndex of the bar OPEN time. Columns: o h l c tv sp.
    """
    rows = conn.execute(
        "SELECT t_utc, o, h, l, c, tv, sp FROM bars WHERE symbol=? AND tf=? AND t_utc<=? "
        "ORDER BY t_utc DESC LIMIT ?",
        (symbol, tf, until_utc if until_utc is not None else 2**62, limit),
    ).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows][::-1], columns=["t_utc", *"ohlc", "tv", "sp"])
    df.index = pd.to_datetime(df.pop("t_utc"), unit="s", utc=True)
    df.index.name = "t"
    return df
