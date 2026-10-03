#!/usr/bin/env python3
"""Export reads + his feedback + simulated outcomes to CSV for analysis.

    uv run python scripts/export_csv.py [--db data/signal.db] [--days 7] [--out reads.csv]

The `sim_*` columns are HYPOTHETICAL results from app/outcomes.py, not real trades.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import outcomes  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/signal.db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="reads.csv")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    n = outcomes.write_csv(conn, now - args.days * 86400, now + 1, args.out)
    print(f"{n} rows written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
