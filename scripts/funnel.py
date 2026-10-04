#!/usr/bin/env python3
"""How many bars survive each filter of a strategy, applied one after the other (diagnostics).

    sudo -u signal .venv/bin/python scripts/funnel.py --from 2024-01-01 --to 2026-10-01 \\
        [--symbols GER40.cash] [--no-htf]

Per direction (long and short counted separately). Uses the backtest's evaluation, so the
H1/D1 rule and session windows apply unless --no-htf.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest, db  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.strategies.brooks_h2 import BrooksH2  # noqa: E402

ORDER = ["timing", "signal_bar", "setup", "trend", "pullback", "not_range", "no_flip", "room"]


def epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--symbols")
    ap.add_argument("--no-htf", action="store_true", help="switch the H1/D1 rule off")
    ap.add_argument("--db", default="data/history.db")
    ap.add_argument("--set", action="append", default=[], help="brooks.<key>=<value> override")
    args = ap.parse_args()
    cfg = load_settings().config
    if args.no_htf:
        cfg = cfg.model_copy(
            update={"rules": cfg.rules.model_copy(update={"require_htf_alignment": False})}
        )
    over = {}
    for item in args.set:
        k, v = item.split("=", 1)
        cur = getattr(cfg.brooks, k)
        over[k] = (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    brooks = cfg.brooks.model_copy(update=over)
    strat = BrooksH2(brooks)
    print(f"overrides: {over or 'none'}")
    symbols = args.symbols.split(",") if args.symbols else [
        s for s, c in cfg.symbols.items() if c.role == "traded"]  # fmt: skip
    conn = db.connect(args.db)

    for sym in symbols:
        survive: Counter = Counter()
        kinds: Counter = Counter()

        class Probe:
            name, version = "funnel", "0"

            def candidates(self, ev, survive=survive, kinds=kinds):
                for d in ("long", "short"):
                    survive["evaluated"] += 1
                    if not strat.timing_ok(ev)[0]:
                        continue
                    survive["timing"] += 1
                    ok, e = strat.check(ev, d)
                    failed = set(e["failed"])
                    for step in ORDER[1:]:
                        if step in failed:
                            break
                        survive[step] += 1
                    if ok:
                        kinds[(d, e["setup"])] += 1
                return []

        res = backtest.run(
            cfg, conn, Probe(), sym, epoch(args.start), epoch(args.end), use_m1=False
        )
        print(f"\n{sym}  {args.start}..{args.end}  H1/D1 rule: {'off' if args.no_htf else 'on'}  "
              f"(bars skipped before the strategy: {dict(res.skips)})")  # fmt: skip
        prev = survive["evaluated"]
        print(f"  {'evaluated (bar x direction)':30} {prev:7}")
        for step in ORDER:
            n = survive[step]
            share = f"{n / prev:.0%}" if prev else "-"
            print(f"  + {step:28} {n:7}  ({share} of the step before)")
            prev = n
        print(f"  setups: {dict(kinds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
