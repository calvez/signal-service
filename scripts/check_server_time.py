#!/usr/bin/env python3
"""Which SERVER_TIME_MODE matches the FTMO server? (task T2)

Reads recent heartbeats from the signal database and, for each candidate rule, counts how many
heartbeats it explains (rule offset == offset reported by the EA). It also checks the EA's
`time_server` against the real clock: (time_server - received_at) should be the true offset.

Run it on a normal day, and again between 25 Oct and 1 Nov 2026 (EU DST has ended, US DST
has not) -- that gap is where `ny_plus_7` and the European zones differ.

    uv run python scripts/check_server_time.py [--db data/signal.db] [--last 500]
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.timeconv import offset_matches  # noqa: E402

CANDIDATES = [
    "ny_plus_7",
    "iana:Europe/Prague",
    "iana:Europe/Berlin",
    "iana:Europe/Athens",
    "iana:Europe/Nicosia",
    "iana:Europe/London",
    "iana:UTC",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/signal.db")
    ap.add_argument("--last", type=int, default=500, help="how many recent heartbeats to use")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT received_at, time_server, server_utc_offset_sec FROM heartbeats "
        "ORDER BY id DESC LIMIT ?",
        (args.last,),
    ).fetchall()
    if not rows:
        print("No heartbeats stored yet.")
        return 1

    reported = Counter(r[2] for r in rows)
    print(f"{len(rows)} heartbeats. Offsets reported by the EA (s): {dict(reported)}")

    # Independent check: server clock minus real clock, rounded to 15 min.
    observed = Counter(round((ts - rec) / 900) * 900 for rec, ts, _ in rows)
    print(f"Observed (time_server - received_at, 15-min rounded): {dict(observed)}")
    print()

    fixed = [f"fixed:{o}" for o in reported]
    perfect = []
    for mode in CANDIDATES + fixed:
        hits = sum(offset_matches(off, ts, mode) for _, ts, off in rows)
        print(f"  {mode:22} explains {hits}/{len(rows)}")
        if hits == len(rows):
            perfect.append(mode)
    print()
    if perfect:
        print(f"Rules that explain every heartbeat: {', '.join(perfect)}")
        print("Several matches just mean the data does not tell them apart yet (no DST change")
        print("in the sample). Re-run during 25 Oct - 1 Nov 2026 to separate ny_plus_7 from the")
        print("European zones.")
    else:
        print("No candidate explains every heartbeat yet; collect more data or add a candidate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
