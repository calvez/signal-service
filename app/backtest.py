"""Backtester: runs a strategy over history with exactly the live evaluation code.

For every closed M5 bar inside the symbol's session window:
    evaluate_bar (app/evaluation.py)  ->  strategy.candidates  ->  validate.check_setup
    ->  the trade is played on the bars that FOLLOW the signal (M1 when available, else M5):
        management.mode = pyramid       app/position.py (no fixed target, adds into a runner,
                                        stop follows the exposure and the M5 swings)
        management.mode = fixed_target  outcomes.simulate (stop or target)

The decision at bar t only ever sees bars <= t (evaluate_bar guarantees it); only the outcome
simulation looks forward, as it must.

Results are in R (1R = the first unit's risk = management.risk_pct % of the initial balance)
and in % of the account, NET of spread: the spread MT5 recorded on the signal bar is charged
once per unit (cost_r = spread x units / risk). Slippage and commission are not modelled.
The FTMO check sums the results per FTMO day (midnight Prague) against the daily loss limit
and the running total against the max loss limit.

Two modes:
  all_signals=True   every candidate is simulated on its own (raw edge of the rule)
  all_signals=False  his rules: one position at a time per symbol, at most
                     rules.max_trades_per_day entries per session day, and a
                     rules.cooldown_after_win_min pause after a winner
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import db, features, sessions
from app.config import AppConfig
from app.evaluation import D1_WINDOW, H1_WINDOW, M5_WINDOW, Skip, evaluate_bar
from app.outcomes import simulate
from app.position import PyramidRules, simulate_position
from app.validate import check_setup

M5_SEC = 300
SCRATCH_R = 0.25  # |result| up to this counts as a scratch (e.g. stopped at breakeven after an add)
ISO = "%Y-%m-%dT%H:%M:%SZ"
WARMUP = {"M5": 4 * 86400, "H1": 40 * 86400, "D1": 420 * 86400}


@dataclass
class Trade:
    symbol: str
    session: str
    signal_utc: int  # open time of the signal bar
    strategy: str
    setup: str
    direction: str
    with_trend: bool
    grade: str
    entry: float
    stop: float
    target: float | None  # None: runner, no fixed target
    validation: str  # "ok", "ok: ..." or "rejected: ..."
    status: str  # rejected | skipped_rules | no_entry | win | loss | expired | stopped | closed_eod
    r_gross: float | None = None
    cost_r: float | None = None
    r_net: float | None = None
    result_pct: float | None = None  # r_net x risk_pct: % of the initial balance
    units: int = 0  # pyramid: 1 + adds
    mfe_r: float | None = None
    entry_utc: int | None = None
    exit_utc: int | None = None
    evidence: dict = field(default_factory=dict)


@dataclass
class Result:
    trades: list[Trade]
    skips: Counter  # why bars were not evaluated (htf_conflict, ...)
    bars_evaluated: int


def _session_windows(cfg: AppConfig, session: str, start: int, end: int) -> list[tuple[int, int]]:
    d = sessions.local_date(cfg, session, start)
    last = sessions.local_date(cfg, session, end)
    wins = []
    while d <= last:
        w = sessions.session_window_utc(cfg, session, d)
        if w and w[1] > start and w[0] < end:
            wins.append(w)
        d += pd.Timedelta(days=1)
    return wins


def run(
    cfg: AppConfig,
    conn,
    strategy,
    symbol: str,
    start_utc: int,
    end_utc: int,
    use_m1: bool = True,
    all_signals: bool = False,
) -> Result:
    """Backtest one symbol on [start_utc, end_utc) from the bars in `conn` (history.db)."""
    session = cfg.symbols[symbol].session
    load = {
        tf: db.load_bars_between(conn, symbol, tf, start_utc - WARMUP[tf], end_utc + 86400)
        for tf in ("M5", "H1", "D1")
    }
    m5, h1, d1 = load["M5"], load["H1"], load["D1"]
    m1 = (
        db.load_bars_between(conn, symbol, "M1", start_utc, end_utc + 2 * 86400) if use_m1 else None
    )
    if m1 is not None and m1.empty:
        m1 = None
    digits = db.get_digits(conn, symbol) or 2
    point = 10**-digits
    m5_times = m5.index.as_unit("s").asi8  # epoch seconds, whatever unit pandas stored
    h1_times = h1.index.as_unit("s").asi8
    d1_times = d1.index.as_unit("s").asi8

    mg = cfg.management
    pyramid = PyramidRules(mg.risk_pct, mg.max_adds, mg.add_every_r, mg.add_size,
                           mg.max_open_risk_r, mg.trail)  # fmt: skip
    # Trailing levels: last CONFIRMED swing low/high, known when its confirming M5 bar closed.
    last = features.last_swings(features.confirmed_swings(m5, cfg.features.swing_confirm_bars))
    known_at = m5.index + pd.Timedelta(seconds=M5_SEC)
    trail = {
        "long": pd.Series(last["last_sl_price"].to_numpy(), index=known_at),
        "short": pd.Series(last["last_sh_price"].to_numpy(), index=known_at),
    }

    trades: list[Trade] = []
    htf_cache: dict = {}
    skips: Counter = Counter()
    evaluated = 0
    busy_until = 0  # his rules: no new entry while a position is open
    day_entries: Counter = Counter()
    cooldown_until = 0

    for win_start, win_end in _session_windows(cfg, session, start_utc, end_utc):
        lo = int(np.searchsorted(m5_times, win_start, side="left"))
        hi = int(np.searchsorted(m5_times, min(win_end, end_utc), side="left"))
        for i in range(lo, hi):
            t = int(m5_times[i])
            window = m5.iloc[max(0, i - M5_WINDOW + 1) : i + 1]
            # Hand over only the higher-timeframe bars evaluate_bar can use (it still filters
            # them itself); just a speed-up over passing the whole history every time.
            j = int(np.searchsorted(h1_times, t + M5_SEC, side="right"))
            k = int(np.searchsorted(d1_times, t + M5_SEC, side="right"))
            h1_win = h1.iloc[max(0, j - H1_WINDOW - 1) : j]
            d1_win = d1.iloc[max(0, k - D1_WINDOW - 1) : k]
            try:
                ev = evaluate_bar(cfg, symbol, t, window, h1_win, d1_win, digits, htf_cache)
            except Skip as skip:
                skips[str(skip).split(" ")[0]] += 1
                continue
            evaluated += 1
            for cand in strategy.candidates(ev):
                out = check_setup(cand.as_setup(), "alert", ev.expected(), cfg.rules)
                tr = Trade(
                    symbol, session, t, f"{strategy.name}:{strategy.version}", cand.setup,
                    cand.direction, cand.with_trend, cand.grade, cand.entry, cand.stop,
                    cand.target, out.summary, "rejected", evidence=dict(cand.evidence),
                )  # fmt: skip
                if out.action == "none":
                    trades.append(tr)
                    continue
                s = out.setup
                tr.entry, tr.stop, tr.target = s["entry"], s["stop"], s["target"]
                day = sessions.local_date(cfg, session, t)
                if not all_signals and (
                    t < busy_until
                    or t < cooldown_until
                    or day_entries[day] >= cfg.rules.max_trades_per_day
                ):
                    tr.status = "skipped_rules"
                    trades.append(tr)
                    continue
                horizon = sessions.cash_close_utc(cfg, session, day)
                bars, secs = (m1, 60) if m1 is not None else (m5, M5_SEC)
                lo_ts = pd.Timestamp(t + M5_SEC, unit="s", tz="UTC")
                hi_ts = pd.Timestamp(horizon, unit="s", tz="UTC")
                after = bars[(bars.index >= lo_ts) & (bars.index < hi_ts)]
                spread = float(m5["sp"].iloc[i]) * point
                risk = abs(s["entry"] - s["stop"])
                if mg.mode == "pyramid" or s["target"] is None:
                    p = simulate_position(s, t, after, horizon, pyramid, point,
                                          trail[s["direction"]], bar_seconds=secs)  # fmt: skip
                    tr.status, tr.r_gross, tr.entry_utc, tr.exit_utc = (
                        p.status, p.r, p.entry_t, p.exit_t)  # fmt: skip
                    tr.units, tr.mfe_r = p.units, p.mfe_r
                    done, size = p.status in ("stopped", "closed_eod"), p.size_total
                else:
                    o = simulate(s, t, after, horizon, now=2**62, bar_seconds=secs)
                    tr.status, tr.r_gross, tr.entry_utc, tr.exit_utc = (
                        o.status, o.r, o.entry_t, o.exit_t)  # fmt: skip
                    tr.units = 1 if o.entry_t else 0
                    done, size = o.status in ("win", "loss", "expired"), 1.0
                if done:
                    tr.cost_r = round(spread * size / risk, 3)
                    tr.r_net = round(tr.r_gross - tr.cost_r, 3)
                    tr.result_pct = round(tr.r_net * mg.risk_pct, 4)
                    day_entries[day] += 1
                    busy_until = (tr.exit_utc or horizon) + secs
                    if tr.r_net > 0:
                        cooldown_until = busy_until + cfg.rules.cooldown_after_win_min * 60
                trades.append(tr)
    return Result(trades, skips, evaluated)


# --------------------------------------------------------------------------- statistics
def stats(trades: list[Trade], ftmo=None) -> dict:
    """Counts, R and % statistics, net of spread. Expectancy is per ENTERED trade.
    With `ftmo` (config.ftmo): worst FTMO day and max drawdown in % of the initial balance,
    and how often the daily / max loss limits would have been broken."""
    entered = [t for t in trades if t.r_net is not None]
    r = [t.r_net for t in entered]
    gains = [x for x in r if x > 0]
    drops = [x for x in r if x <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for x in r:  # trades are in time order
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    count = Counter(t.status for t in trades)
    out = {
        "signals": len(trades),
        "rejected": count["rejected"],
        "skipped_rules": count["skipped_rules"],
        "no_entry": count["no_entry"],
        "entered": len(entered),
        "win": sum(x > SCRATCH_R for x in r),
        "scratch": sum(abs(x) <= SCRATCH_R for x in r),
        "loss": sum(x < -SCRATCH_R for x in r),
        "win_rate": round(sum(x > SCRATCH_R for x in r) / len(r), 3) if r else None,
        "total_r": round(sum(r), 2),
        "expectancy_r": round(sum(r) / len(r), 3) if r else None,
        "profit_factor": round(sum(gains) / -sum(drops), 2) if drops and sum(drops) < 0 else None,
        # runners: how much of the gross profit the 5 best trades made
        "top5_share": round(sum(sorted(gains, reverse=True)[:5]) / sum(gains), 2)
        if gains
        else None,
        "max_drawdown_r": round(max_dd, 2),
        "avg_cost_r": round(sum(t.cost_r for t in entered) / len(entered), 3) if entered else None,
        "avg_units": round(sum(t.units for t in entered) / len(entered), 2) if entered else None,
        "best_r": max(r) if r else None,
        "total_pct": round(sum(t.result_pct or 0.0 for t in entered), 2),
    }
    if ftmo is not None and entered:
        z = ZoneInfo(ftmo.day_reset_tz)
        per_day: Counter = Counter()
        for t in entered:
            day = datetime.fromtimestamp(t.exit_utc or t.signal_utc, tz=UTC).astimezone(z).date()
            per_day[day] += t.result_pct or 0.0
        eq, pk, dd, low = 0.0, 0.0, 0.0, 0.0
        for t in entered:
            eq += t.result_pct or 0.0
            pk, low = max(pk, eq), min(low, eq)
            dd = max(dd, pk - eq)
        out.update(
            worst_day_pct=round(min(per_day.values()), 2),
            daily_limit_breaches=sum(v <= -ftmo.daily_loss_pct for v in per_day.values()),
            max_drawdown_pct=round(dd, 2),
            max_loss_breached=low <= -ftmo.max_loss_pct,
        )
    return out


def report(trades: list[Trade], split_utc: int | None = None, ftmo=None) -> str:
    """Plain-text report: overall, by setup, by symbol, and in/out of sample if split."""

    def block(title: str, ts: list[Trade]) -> str:
        s = stats(ts)
        ex = "-" if s["expectancy_r"] is None else f"{s['expectancy_r']:+.3f}R"
        pf = "-" if s["profit_factor"] is None else f"{s['profit_factor']}"
        dd = s["max_drawdown_r"]
        wsl = f"{s['win']}/{s['scratch']}/{s['loss']}"
        return (
            f"{title:28} signals {s['signals']:5}  entered {s['entered']:4}  W/S/L {wsl:>9}  "
            f"exp {ex:>8}  total {s['total_r']:+8.2f}R ({s['total_pct']:+.1f}%)  PF {pf:>5}  "
            f"maxDD {dd:.1f}R  units {s['avg_units'] or 0:.1f}"
        )

    lines = [block("ALL", trades)]
    if ftmo is not None:
        f = stats(trades, ftmo)
        if "worst_day_pct" in f:
            lines.append(
                f"{'FTMO check':28} worst day {f['worst_day_pct']:+.2f}% "
                f"(limit -{ftmo.daily_loss_pct}%: {f['daily_limit_breaches']} breaches) · "
                f"max drawdown {f['max_drawdown_pct']:.2f}% (limit -{ftmo.max_loss_pct}%: "
                f"{'BREACHED' if f['max_loss_breached'] else 'ok'}) · "
                f"best trade {f['best_r']:+.1f}R · 5 best trades = "
                f"{(f['top5_share'] or 0):.0%} of gross profit"
            )
    for name, group in (("setup", "setup"), ("symbol", "symbol")):
        for key in sorted({getattr(t, group) for t in trades}):
            lines.append(block(f"  {name} {key}", [t for t in trades if getattr(t, group) == key]))
    if split_utc is not None:
        lines.append(
            block("IN-SAMPLE (before split)", [t for t in trades if t.signal_utc < split_utc])
        )
        lines.append(
            block("OUT-OF-SAMPLE (after)", [t for t in trades if t.signal_utc >= split_utc])
        )
    return "\n".join(lines)


def to_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = []
    for t in trades:
        d = asdict(t)
        for k in ("signal_utc", "entry_utc", "exit_utc"):
            v = d[k]
            d[k] = (
                ""
                if v is None
                else pd.Timestamp(v, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        d["evidence"] = str(d["evidence"])
        rows.append(d)
    return pd.DataFrame(rows)
