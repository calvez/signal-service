#!/usr/bin/env python3
"""Backtest of Lorant's spec (docs/spec-brooks-ea.md) with the campaign engine — §10 matrix.

    sudo -u signal .venv/bin/python scripts/backtest_campaign.py --variant A \\
        --from 2019-01-01 --to 2026-10-01 --split 2024-01-01 \\
        [--symbols GER40.cash,US100.cash,US30.cash] [--no-htf] [--set key=value ...]

Variants (§10): A = trailing + pyramiding (default), B = trailing without pyramiding,
C = fixed 2R take profit without pyramiding. --set overrides any `brooks:` input.
Results are SIMULATED, in R per campaign and % of the balance, net of spread.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backtest_campaign as bt  # noqa: E402
from app import db  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.strategies.brooks_h2 import BrooksH2  # noqa: E402

VARIANTS = {
    "A": {"exit_mode": "trail", "enable_pyramiding": True},
    "B": {"exit_mode": "trail", "enable_pyramiding": False},
    "C": {"exit_mode": "fixed_tp", "enable_pyramiding": False},
}


def epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=VARIANTS, default="A")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--split", help="YYYY-MM-DD: in-sample before, out-of-sample after")
    ap.add_argument("--symbols")
    ap.add_argument("--no-htf", action="store_true", help="switch his H1/D1 rule off")
    ap.add_argument("--no-m1", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="brooks.<key>=<value>")
    ap.add_argument("--db", default="data/history.db")
    ap.add_argument("--out")
    args = ap.parse_args()

    cfg = load_settings().config
    over = dict(VARIANTS[args.variant])
    for item in args.set:
        k, v = item.split("=", 1)
        cur = getattr(cfg.brooks, k)
        over[k] = (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v)
    brooks = cfg.brooks.model_copy(update=over)
    rules = cfg.rules.model_copy(update={"require_htf_alignment": not args.no_htf})
    cfg = cfg.model_copy(update={"brooks": brooks, "rules": rules})
    symbols = args.symbols.split(",") if args.symbols else [
        s for s, c in cfg.symbols.items() if c.role == "traded"]  # fmt: skip

    t0 = time.time()
    conn = db.connect(args.db)
    start, end = epoch(args.start), epoch(args.end)
    res = bt.run(cfg, conn, BrooksH2(brooks), symbols, start, end, use_m1=not args.no_m1)
    df = bt.campaign_rows(res.campaigns, brooks.risk_per_trade_pct)
    htf = "off" if args.no_htf else "on"
    took = time.time() - t0
    print(
        f"variant {args.variant} · {', '.join(symbols)} · {args.start}..{args.end} · "
        f"H1/D1 rule {htf} · overrides {over} · {res.bars} session bars in {took:.0f}s · "
        "SIMULATED, net of spread"
    )
    print("bars not evaluated:", dict(res.skips.most_common(8)))

    def show(title, part):
        print(f"\n== {title}")
        print(json.dumps(bt.stats(part, cfg), indent=1, default=str))

    show("ALL", df)
    if args.split and not df.empty:
        cut = args.split
        show(f"IN-SAMPLE (< {cut})", df[df["signal_utc"] < cut])
        show(f"OUT-OF-SAMPLE (>= {cut})", df[df["signal_utc"] >= cut])
    for sym in symbols:
        if not df.empty and (df["symbol"] == sym).any():
            show(sym, df[df["symbol"] == sym])
    out = Path(args.out or f"data/backtests/campaign_{args.variant}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\n{len(df)} campaigns written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
