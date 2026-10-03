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

-- One row per LLM call (CLAUDE.md non-negotiable 5). `parsed` and `validation` are filled in
-- afterwards by the reader (T7); `status` is ok | error.
CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY,
    ts_utc         INTEGER NOT NULL,
    purpose        TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    provider       TEXT,
    prompt         TEXT    NOT NULL,             -- system + user text exactly as sent
    raw_response   TEXT,                         -- response body (error body on failure)
    parsed         TEXT,                         -- JSON of the parsed answer, if any
    validation     TEXT,                         -- outcome, e.g. ok | rejected: <reason>
    status         TEXT    NOT NULL,
    error          TEXT,
    latency_ms     INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       REAL
);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls (ts_utc);

-- Per-symbol facts the EA reports with every bars payload.
CREATE TABLE IF NOT EXISTS symbol_meta (
    symbol     TEXT PRIMARY KEY,
    digits     INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Lorant's own call on an alert/watch (Telegram buttons). Several rows per read are allowed;
-- the latest one counts.
CREATE TABLE IF NOT EXISTS feedback (
    id      INTEGER PRIMARY KEY,
    read_id INTEGER NOT NULL REFERENCES reads (id),
    choice  TEXT    NOT NULL,                    -- take | skip | unsure
    ts_utc  INTEGER NOT NULL
);

-- Small key/value store: Telegram offset, pause state, once-only markers, monitor state.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Hypothetical outcome of every alert/watch (outcomes.py). NOT real trades.
CREATE TABLE IF NOT EXISTS outcomes (
    read_id    INTEGER PRIMARY KEY REFERENCES reads (id),
    status     TEXT    NOT NULL,                 -- pending | no_entry | win | loss | expired
    r          REAL,                             -- result in R (NULL for pending / no_entry)
    entry_t    INTEGER,                          -- open time of the bar that triggered the entry
    exit_t     INTEGER,
    updated_at INTEGER NOT NULL
);

-- One row per evaluated bar: what the model said and what we made of it.
CREATE TABLE IF NOT EXISTS reads (
    id             INTEGER PRIMARY KEY,
    ts_utc         INTEGER NOT NULL,
    symbol         TEXT    NOT NULL,
    bar_time_utc   INTEGER NOT NULL,             -- open time of the evaluated M5 bar
    session        TEXT    NOT NULL,
    llm_call_id    INTEGER REFERENCES llm_calls (id),
    model          TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    htf_alignment  TEXT    NOT NULL,             -- computed by code
    day_type_hint  TEXT    NOT NULL,             -- computed by code
    atr            REAL,
    last_close     REAL,
    model_action   TEXT,                         -- what the model asked for
    action         TEXT    NOT NULL,             -- final: none | watch | alert
    push           INTEGER NOT NULL DEFAULT 0,
    grade          TEXT,
    setup          TEXT,                         -- JSON, prices rounded
    context        TEXT,                         -- JSON from the model
    reason         TEXT,
    validation     TEXT    NOT NULL,
    notified_at    INTEGER,                      -- set by the Telegram sender (T8)
    UNIQUE (symbol, bar_time_utc)
);
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


def insert_llm_call(conn: sqlite3.Connection, row: dict) -> int:
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with conn:
        cur = conn.execute(f"INSERT INTO llm_calls ({cols}) VALUES ({params})", row)
    return int(cur.lastrowid)


def update_llm_call(
    conn: sqlite3.Connection, call_id: int, parsed: str | None, validation: str
) -> None:
    """The reader records what it made of the answer."""
    with conn:
        conn.execute(
            "UPDATE llm_calls SET parsed = ?, validation = ? WHERE id = ?",
            (parsed, validation, call_id),
        )


def llm_spend_since(conn: sqlite3.Connection, since_utc: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls WHERE ts_utc >= ?", (since_utc,)
    ).fetchone()
    return float(row["s"])


def event_exists_since(conn: sqlite3.Connection, kind: str, since_utc: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE kind = ? AND ts_utc >= ? LIMIT 1", (kind, since_utc)
    ).fetchone()
    return row is not None


def upsert_symbol_meta(conn: sqlite3.Connection, symbol: str, digits: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO symbol_meta (symbol, digits, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (symbol) DO UPDATE SET digits = excluded.digits, "
            "updated_at = excluded.updated_at",
            (symbol, digits, int(time.time())),
        )


def get_digits(conn: sqlite3.Connection, symbol: str) -> int | None:
    row = conn.execute("SELECT digits FROM symbol_meta WHERE symbol = ?", (symbol,)).fetchone()
    return None if row is None else int(row["digits"])


def insert_read(conn: sqlite3.Connection, row: dict) -> int:
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with conn:
        cur = conn.execute(f"INSERT INTO reads ({cols}) VALUES ({params})", row)
    return int(cur.lastrowid)


def read_exists(conn: sqlite3.Connection, symbol: str, bar_time_utc: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reads WHERE symbol = ? AND bar_time_utc = ?", (symbol, bar_time_utc)
    ).fetchone()
    return row is not None


# ------------------------------------------------------------------ key/value, feedback
def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    with conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def kv_once(conn: sqlite3.Connection, key: str, value: str = "1") -> bool:
    """True the first time a key is claimed, False afterwards (atomic): 'send this only once'."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )
    return cur.rowcount == 1


def add_feedback(conn: sqlite3.Connection, read_id: int, choice: str, ts_utc: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO feedback (read_id, choice, ts_utc) VALUES (?, ?, ?)",
            (read_id, choice, ts_utc),
        )


def latest_heartbeats(conn: sqlite3.Connection, n: int = 2) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM heartbeats ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def load_bars_between(
    conn: sqlite3.Connection, symbol: str, tf: str, start_utc: int, end_utc: int
) -> pd.DataFrame:
    """Bars with start_utc <= open time < end_utc, oldest first (same shape as load_bars)."""
    rows = conn.execute(
        "SELECT t_utc, o, h, l, c, tv, sp FROM bars "
        "WHERE symbol=? AND tf=? AND t_utc>=? AND t_utc<? ORDER BY t_utc",
        (symbol, tf, start_utc, end_utc),
    ).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["t_utc", *"ohlc", "tv", "sp"])
    df.index = pd.to_datetime(df.pop("t_utc"), unit="s", utc=True)
    df.index.name = "t"
    return df
