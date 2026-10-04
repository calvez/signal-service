#!/usr/bin/env python3
"""Load the MT5 history export into data/history.db for backtests.

    sudo -u signal .venv/bin/python scripts/import_history.py \
        [--csv data/history_csv] [--db data/history.db]

Server times are converted with server_time_mode from config.yaml (the same rule as live).
Re-running is safe: bars are upserted.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.history import import_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/history_csv")
    ap.add_argument("--db", default="data/history.db")
    args = ap.parse_args()
    mode = load_settings().config.server_time_mode
    print(f"server_time_mode = {mode}")
    for symbol, tf, n, first, last in import_dir(args.csv, args.db, mode):
        f = datetime.fromtimestamp(first, tz=UTC)
        la = datetime.fromtimestamp(last, tz=UTC)
        print(f"{symbol:11} {tf:3} {n:>9,} bars  {f:%Y-%m-%d %H:%M} .. {la:%Y-%m-%d %H:%M} UTC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
