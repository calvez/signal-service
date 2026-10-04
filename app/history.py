"""Import of the MT5 history export (mt5/HistoryDump.mq5 -> data/history_csv/*.csv) into a
separate SQLite file for backtests (data/history.db, same `bars` schema as the live database).

Kept apart from the live database on purpose: it is large (M1 bars), can always be downloaded
again, and must not slow down or bloat the live service and its nightly backup.
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from app import db
from app.timeconv import server_to_utc

CHUNK = 50_000


def read_export(path: Path) -> tuple[str, str, int, pd.DataFrame]:
    """(symbol, timeframe, digits, rows) of one exported CSV file."""
    with open(path, encoding="ascii") as fh:
        meta = dict(part.split("=", 1) for part in fh.readline().lstrip("# ").split())
    df = pd.read_csv(path, skiprows=1)
    return meta["symbol"], meta["tf"], int(meta["digits"]), df


def to_utc(t_server: np.ndarray, mode: str) -> np.ndarray:
    """Vectorised timeconv.server_to_utc. The server's UTC offset only changes on whole hours
    (DST switches), so it is computed once per distinct hour and applied to every bar in it."""
    hours = (t_server // 3600) * 3600
    uniq = np.unique(hours)
    offsets = {int(h): int(h) - server_to_utc(int(h), mode) for h in uniq}
    return t_server - np.array([offsets[int(h)] for h in hours], dtype=np.int64)


def import_file(conn: sqlite3.Connection, path: Path, mode: str) -> tuple[str, str, int, int, int]:
    """Upsert one export file. Returns (symbol, tf, rows, first_utc, last_utc)."""
    symbol, tf, digits, df = read_export(path)
    t_server = df["t_server"].to_numpy(dtype=np.int64)
    df["t_utc"] = to_utc(t_server, mode)
    for i in range(0, len(df), CHUNK):  # chunks: an M1 file has millions of rows
        part = df.iloc[i : i + CHUNK]
        rows = [
            {"symbol": symbol, "tf": tf, "t_server": int(ts), "t_utc": int(tu), "o": float(o),
             "h": float(h), "l": float(lo), "c": float(c), "tv": int(tv), "sp": int(sp),
             "received_at": 0}
            for ts, tu, o, h, lo, c, tv, sp in zip(
                part["t_server"], part["t_utc"], part["o"], part["h"], part["l"], part["c"],
                part["tv"], part["sp"], strict=True)
        ]  # fmt: skip
        db.upsert_bars(conn, rows)
    db.upsert_symbol_meta(conn, symbol, digits)
    return symbol, tf, len(df), int(df["t_utc"].min()), int(df["t_utc"].max())


def import_dir(csv_dir: str | Path, db_path: str, mode: str) -> list[tuple]:
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        return [import_file(conn, p, mode) for p in sorted(Path(csv_dir).glob("*.csv"))]
    finally:
        conn.close()
