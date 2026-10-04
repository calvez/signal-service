#!/usr/bin/env python3
"""Backtest a strategy on the downloaded history (data/history.db).

    sudo -u signal .venv/bin/python scripts/backtest.py --strategy demo \\
        --from 2025-01-01 --to 2026-10-01 --split 2026-04-01 [--symbols GER40.cash,US100.cash] \\
        [--all-signals] [--no-m1] [--out data/backtests/demo.csv]

Prints a report (overall, per setup, per symbol, in/out of sample) and writes every signal
with its simulated outcome to the CSV. Results are SIMULATED and net of spread only.
"""

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest, db  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.strategies import get_strategy  # noqa: E402


def epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC, exclusive)")
    ap.add_argument("--split", help="YYYY-MM-DD: report in-sample before / out-of-sample after")
    ap.add_argument("--symbols", help="comma separated; default: all traded symbols")
    ap.add_argument("--db", default="data/history.db")
    ap.add_argument("--all-signals", action="store_true", help="ignore his trade rules")
    ap.add_argument("--no-m1", action="store_true", help="simulate on M5 instead of M1")
    ap.add_argument("--out", help="CSV of all signals (default data/backtests/<strategy>.csv)")
    args = ap.parse_args()

    cfg = load_settings().config
    strategy = get_strategy(args.strategy, cfg)
    symbols = (
        args.symbols.split(",")
        if args.symbols
        else [s for s, c in cfg.symbols.items() if c.role == "traded"]
    )
    conn = db.connect(args.db)
    trades = []
    for sym in symbols:
        t0 = time.time()
        res = backtest.run(cfg, conn, strategy, sym, epoch(args.start), epoch(args.end),
                           use_m1=not args.no_m1, all_signals=args.all_signals)  # fmt: skip
        trades += res.trades
        print(f"{sym}: {res.bars_evaluated} bars evaluated, {len(res.trades)} signals, "
              f"skipped {dict(res.skips)}, {time.time() - t0:.0f}s")  # fmt: skip
    trades.sort(key=lambda t: t.signal_utc)
    print()
    print(f"strategy {strategy.name} v{strategy.version} · {args.start} .. {args.end} · "
          f"{'all signals' if args.all_signals else 'his trade rules'} · "
          f"{'M5' if args.no_m1 else 'M1'} outcomes · SIMULATED, net of spread")  # fmt: skip
    mg = cfg.management
    adds = "no limit" if mg.max_adds is None else f"up to {mg.max_adds}"
    print(f"management: {mg.mode} · first unit risks {mg.risk_pct}% = 1R · adds: {adds} "
          f"(leverage cap {mg.max_leverage:g}:1), same size, every +{mg.add_every_r}R · "
          f"stop keeps open risk <= {mg.max_open_risk_r}R after adds · trail {mg.trail} · "
          f"exit on reversal bar: {mg.exit_on_reversal} · flat {mg.flat_before_close_min} min "
          f"before the close")  # fmt: skip
    print(backtest.report(trades, epoch(args.split) if args.split else None, cfg.ftmo))
    out = Path(args.out or f"data/backtests/{strategy.name}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    backtest.to_frame(trades).to_csv(out, index=False)
    print(f"\n{len(trades)} signals written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
