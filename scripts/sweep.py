#!/usr/bin/env python3
"""Which filter settings produce how many setups? (docs/spec-brooks-ea.md tuning)

    sudo -u signal .venv/bin/python scripts/sweep.py --from 2025-01-01 --to 2026-10-01 \\
        --symbols GER40.cash [--preset relax] [--set key=value ...]

The expensive part (evaluate_bar per session bar) runs ONCE; every configuration is then
applied to the same cached evaluations, so a dozen variants cost barely more than one. Reports
setups per trading session, which is what Lorant's target of 2-3 trades per session needs.

This only counts SETUPS (entry signals). Whether they make money is the backtest's job.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest_campaign as bt  # noqa: E402
from app import db, sessions  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.evaluation import D1_WINDOW, H1_WINDOW, M5_WINDOW, Skip, evaluate_bar  # noqa: E402
from app.strategies.brooks_h2 import BrooksH2  # noqa: E402

M5_SEC = 300

# Named variants. Each is a set of overrides on top of config.yaml `brooks:`.
PRESETS: dict[str, dict] = {
    "spec": {},
    # the signal-bar quality rule of §3, one notch softer
    "sb_soft": {"sb_body_min": 0.4, "sb_close_pos_min": 0.6, "sb_tail_max": 0.3},
    # allow H1 entries as well as H2 (§2 input AllowH1)
    "h1": {"allow_h1": True},
    # the trading-range filter of §2, less strict
    "range_soft": {"range_overlap_count": 8},
    # no "room to 2R" requirement
    "no_room": {"min_target_r": 0.0},
    # drop the "higher swing high" part of the trend test by shortening the slope window
    "trend_soft": {"ema_slope_bars": 3},
    # everything above together
    "relax": {
        "sb_body_min": 0.4,
        "sb_close_pos_min": 0.6,
        "sb_tail_max": 0.3,
        "allow_h1": True,
        "range_overlap_count": 8,
        "min_target_r": 0.0,
    },  # fmt: skip
    # relax, but keep the 2R room rule
    "relax_room": {
        "sb_body_min": 0.4,
        "sb_close_pos_min": 0.6,
        "sb_tail_max": 0.3,
        "allow_h1": True,
        "range_overlap_count": 8,
    },  # fmt: skip
}


def epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def collect(cfg, conn, symbol: str, start: int, end: int) -> list:
    """Every session bar's Evaluation, once (the expensive step)."""
    d = bt.load(conn, cfg, symbol, start, end, use_m1=False)
    out, cache = [], {}
    day = sessions.local_date(cfg, d.session, start)
    last = sessions.local_date(cfg, d.session, end)
    idx: list[int] = []
    while day <= last:
        win = sessions.session_window_utc(cfg, d.session, day)
        if win:
            lo = int(np.searchsorted(d.m5_t, max(win[0], start)))
            hi = int(np.searchsorted(d.m5_t, min(win[1], end)))
            idx += list(range(lo, hi))
        day += __import__("pandas").Timedelta(days=1)
    for i in idx:
        t = int(d.m5_t[i])
        j = int(np.searchsorted(d.h1_t, t + M5_SEC, side="right"))
        k = int(np.searchsorted(d.d1_t, t + M5_SEC, side="right"))
        try:
            out.append(evaluate_bar(
                cfg, symbol, t,
                d.m5.iloc[max(0, i - M5_WINDOW + 1) : i + 1],
                d.h1.iloc[max(0, j - H1_WINDOW - 1) : j],
                d.d1.iloc[max(0, k - D1_WINDOW - 1) : k],
                d.digits, cache,
            ))  # fmt: skip
        except Skip:
            pass
    return out


def count(evals: list, brooks) -> tuple[int, Counter, Counter]:
    """(setups, per direction+type, failures per rule)."""
    strat = BrooksH2(brooks)
    kinds: Counter = Counter()
    fails: Counter = Counter()
    n = 0
    for ev in evals:
        if not strat.timing_ok(ev)[0]:
            fails["timing"] += 1
            continue
        for direction in ("long", "short"):
            ok, e = strat.check(ev, direction)
            if ok:
                n += 1
                kinds[f"{direction} {e['setup']}"] += 1
            else:
                for rule in e["failed"]:
                    fails[rule] += 1
    return n, kinds, fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--symbols", default="GER40.cash")
    ap.add_argument("--presets", default=",".join(PRESETS))
    ap.add_argument("--set", action="append", default=[], help="applied to EVERY preset")
    ap.add_argument("--db", default="data/history.db")
    args = ap.parse_args()

    cfg = load_settings().config
    cfg = cfg.model_copy(update={"rules": cfg.rules.model_copy(
        update={"require_htf_alignment": False})})  # fmt: skip
    base = {}
    for item in args.set:
        k, v = item.split("=", 1)
        cur = getattr(cfg.brooks, k)
        base[k] = (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    conn = db.connect(args.db)
    start, end = epoch(args.start), epoch(args.end)

    for symbol in args.symbols.split(","):
        evals = collect(cfg, conn, symbol, start, end)
        days = len({ev.session_start for ev in evals})
        print(f"\n{symbol}  {args.start}..{args.end}  {len(evals)} session bars, {days} sessions"
              f"  (always-on overrides: {base or 'none'})")  # fmt: skip
        print(f"  {'preset':12} {'setups':>7} {'per session':>12}   breakdown")
        for name in args.presets.split(","):
            brooks = cfg.brooks.model_copy(update={**PRESETS[name], **base})
            n, kinds, fails = count(evals, brooks)
            per = n / days if days else 0
            top = ", ".join(f"{k}={v}" for k, v in fails.most_common(3))
            print(f"  {name:12} {n:7} {per:12.2f}   {dict(kinds)}  | top rejects: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
