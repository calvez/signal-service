"""Pyramiding position simulator: no fixed target, add to a runner, move the stop as the
exposure grows. Everything here is SIMULATED.

How one position is played (long; short is the mirror image):

  1. ENTRY      Buy-stop at `entry` within the entry window (the next 3 M5 bars), as in
                outcomes.simulate. The first unit risks `entry - stop` points = 1R, and is
                sized so that 1R = rules.risk_pct % of the initial balance.
  2. ADDS       Each time price trades another `add_every_r` R above the first entry
                (1R, 2R, ... measured from the first entry), a further unit of
                `add_size` x the first unit is bought at that price; `max_adds` None = no limit.
  3. STOP       After every add the common stop is raised so that the WHOLE position never
                loses more than `max_open_risk_r` R if it is hit:
                    stop >= (sum(q_i * e_i) - max_open_risk_r * R) / sum(q_i)
                Exposure up -> stop tighter. With `trail = swing` the stop also follows the
                last CONFIRMED M5 swing low (minus one tick) once it is above the stop.
                The stop only ever moves in the trade's favour.
  4. EXIT       The whole position, at the first of:
                - the stop
                - a REVERSAL BAR against the position (`exit_on_reversal`): at the close of
                  that M5 bar. Which bars count is decided by the caller (`reversal_exits`,
                  see backtest.reversal_exits); only bars that close after the entry count.
                - the last close before the horizon: flat before the end of the day.
  5. ORDER IN A BAR (we only know high and low, so always the worse case first):
                reversal exit (the reversal bar closed before this bar) -> trail update (the
                swing level is known before the bar starts) -> stop check -> adds ->
                risk-based stop update -> stop check again (an add in a bar that also reached
                the raised stop is stopped out in that bar).

Result in R of the first unit's risk; result_pct = R x risk_pct. Spread is charged per unit by
the caller. No slippage, no partial fills.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

M5_SEC = 300
ENTRY_WINDOW_BARS = 3
GIVE_UP_AFTER_SEC = 600


@dataclass(frozen=True)
class PyramidRules:
    risk_pct: float = 0.3  # % of the initial balance risked by the first unit (= 1R)
    max_adds: int | None = None  # None = no limit
    add_every_r: float = 1.0
    add_size: float = 1.0  # each add, as a multiple of the first unit
    max_open_risk_r: float = 0.0  # after an add the whole position risks at most this (0 = BE)
    trail: str = "swing"  # swing | none
    exit_on_reversal: bool = True


@dataclass
class PositionResult:
    status: str  # pending | no_entry | stopped | reversal | closed_eod
    r: float | None = None  # total result in R of the first unit's risk
    units: int = 0  # 1 + number of adds
    size_total: float = 0.0  # sum of unit sizes (1.0 = the first unit)
    entry_t: int | None = None
    exit_t: int | None = None
    exit_price: float | None = None
    mfe_r: float = 0.0  # best open result seen, in R
    events: list[tuple[int, str, float]] = field(default_factory=list)  # (time, what, price)


def _level_at(levels: pd.Series | None, times: np.ndarray | None, t: int) -> float | None:
    """The trailing level known at time t (levels are indexed by the time they became known)."""
    if levels is None or times is None or len(times) == 0:
        return None
    i = int(np.searchsorted(times, t, side="right")) - 1
    if i < 0:
        return None
    v = levels.iloc[i]
    return None if pd.isna(v) else float(v)


def simulate_position(
    setup: dict,
    signal_open: int,
    bars: pd.DataFrame,
    horizon: int,
    rules: PyramidRules,
    tick: float,
    trail_levels: pd.Series | None = None,
    now: int = 2**62,
    bar_seconds: int = M5_SEC,
    reversal_exits: pd.Series | None = None,
) -> PositionResult:
    """Play one pyramiding position on `bars` (M1 or M5, UTC index) after an M5 signal.

    `trail_levels`: last confirmed swing low (long) / high (short), indexed by the UTC time at
    which each value became known (M5 close). `reversal_exits`: close price of every M5 bar
    that is a reversal bar AGAINST this position (NaN otherwise), indexed by its close time.
    Pure function, no database.
    """
    long = setup["direction"] == "long"
    sign = 1.0 if long else -1.0
    e0, s0 = float(setup["entry"]), float(setup["stop"])
    risk_pts = abs(e0 - s0)  # 1R in points for one unit of size 1.0
    first_open = signal_open + M5_SEC
    deadline = first_open + ENTRY_WINDOW_BARS * M5_SEC
    lv_times = None
    if trail_levels is not None and rules.trail == "swing":
        lv_times = trail_levels.index.as_unit("s").asi8
    else:
        trail_levels = None

    rev_times = rev_vals = None
    if reversal_exits is not None and rules.exit_on_reversal:
        rev_times = reversal_exits.index.as_unit("s").asi8
        rev_vals = reversal_exits.to_numpy(dtype=float)
    rev_i = 0  # next reversal-bar slot to look at

    units: list[tuple[float, float]] = []  # (entry price, size)
    stop = s0
    res = PositionResult("pending")
    last_t, last_close = None, e0

    def pnl_r(price: float) -> float:
        return sum(q * sign * (price - e) for e, q in units) / risk_pts

    def close_all(t: int, price: float, status: str) -> PositionResult:
        res.status, res.exit_t, res.exit_price = status, t, price
        res.units, res.size_total = len(units), sum(u[1] for u in units)
        res.r = round(pnl_r(price), 3)
        res.events.append((t, "exit", price))
        return res

    for ts, bar in bars.iterrows():
        t = int(ts.timestamp())
        if t < first_open or t >= horizon:
            continue
        hi, lo, cl = float(bar["h"]), float(bar["l"]), float(bar["c"])
        last_close_before = last_close
        last_t, last_close = t, cl
        favourable = hi if long else lo
        adverse = lo if long else hi

        if not units:  # waiting for the entry
            if t >= deadline:
                res.status = "no_entry"
                return res
            if sign * (favourable - e0) < 0:
                continue
            units.append((e0, 1.0))
            res.entry_t = t
            if rev_times is not None:  # only reversal bars closing after the entry count
                rev_i = int(np.searchsorted(rev_times, t, side="right"))
            res.events.append((t, "entry", e0))
            if sign * (adverse - stop) <= 0:  # entry bar also reached the stop: worse case
                return close_all(t, stop, "stopped")
            continue

        # 0. a reversal bar against the position closed before this bar: out at its close
        if rev_times is not None:
            while rev_i < len(rev_times) and rev_times[rev_i] <= t:
                if not np.isnan(rev_vals[rev_i]):
                    return close_all(int(rev_times[rev_i]), float(rev_vals[rev_i]), "reversal")
                rev_i += 1

        # 1. trail behind the last confirmed swing (known before this bar), then the stop
        lvl = _level_at(trail_levels, lv_times, t)
        if lvl is not None:
            candidate = lvl - sign * tick
            if sign * (candidate - stop) > 0 and sign * (last_close_before - candidate) > 0:
                stop = candidate
                res.events.append((t, "trail", stop))
        if sign * (adverse - stop) <= 0:
            return close_all(t, stop, "stopped")

        # 2. adds at +1R, +2R, ... from the first entry; 3. stop so the whole position
        #    risks at most max_open_risk_r
        added = False
        while rules.max_adds is None or len(units) - 1 < rules.max_adds:
            n = len(units)  # next add number (1, 2, ...)
            price = e0 + sign * n * rules.add_every_r * risk_pts
            if sign * (favourable - price) < 0:
                break
            units.append((price, rules.add_size))
            res.events.append((t, f"add{n}", price))
            added = True
            q = sum(u[1] for u in units)
            needed = (sum(e * u for e, u in units) - sign * rules.max_open_risk_r * risk_pts) / q
            if sign * (needed - stop) > 0:
                stop = needed
                res.events.append((t, "stop", stop))

        res.mfe_r = max(res.mfe_r, round(pnl_r(favourable), 3))
        # 4. an add in this bar whose raised stop was also reached in this bar: worse case
        if added and sign * (adverse - stop) <= 0:
            return close_all(t, stop, "stopped")

    res.units = len(units)
    res.size_total = sum(u[1] for u in units)
    complete = (
        last_t is not None and last_t + bar_seconds >= horizon
    ) or now >= horizon + GIVE_UP_AFTER_SEC
    if not units:
        window_over = last_t is not None and last_t + bar_seconds >= deadline
        res.status = "no_entry" if (window_over or complete) else "pending"
        return res
    if not complete:
        return res  # still open: pending
    out = close_all(last_t, last_close, "closed_eod")
    out.units, out.size_total = len(units), sum(u[1] for u in units)
    return out


# --------------------------------------------------------------------------- shared inputs
def max_units_by_leverage(risk_pct: float, risk_pts: float, price: float, leverage: float) -> int:
    """How many same-size units fit under balance x leverage.

    One unit is sized so that risk_pts points = risk_pct % of the balance, so its notional is
    (risk_pct/100 x balance / risk_pts) x price. Units <= leverage x risk_pts / (risk_pct/100 x
    price). Example: GER40 at 24,000, 27-point stop, 0.3 %, 20:1 -> 7 units. At least 1.
    """
    if risk_pts <= 0 or price <= 0 or risk_pct <= 0:
        return 1
    return max(1, int(leverage * risk_pts / (risk_pct / 100 * price)))


def capped_rules(rules: PyramidRules, setup: dict, leverage: float) -> PyramidRules:
    """`rules` with max_adds limited by the leverage cap for this setup."""
    from dataclasses import replace

    risk_pts = abs(float(setup["entry"]) - float(setup["stop"]))
    room = max_units_by_leverage(rules.risk_pct, risk_pts, float(setup["entry"]), leverage) - 1
    adds = room if rules.max_adds is None else min(rules.max_adds, room)
    return replace(rules, max_adds=adds)


def rules_from_config(mg) -> PyramidRules:
    """PyramidRules from config.management."""
    return PyramidRules(
        risk_pct=mg.risk_pct, max_adds=mg.max_adds, add_every_r=mg.add_every_r,
        add_size=mg.add_size, max_open_risk_r=mg.max_open_risk_r, trail=mg.trail,
        exit_on_reversal=mg.exit_on_reversal,
    )  # fmt: skip


def exit_inputs(m5: pd.DataFrame, swing_n: int) -> dict[str, tuple[pd.Series, pd.Series]]:
    """Per direction: (trailing levels, reversal exits) from closed M5 bars, each indexed by the
    time the value became known (the M5 bar's close). Used by the backtest and live outcomes."""
    from app import features

    known_at = m5.index + pd.Timedelta(seconds=M5_SEC)
    last = features.last_swings(features.confirmed_swings(m5, swing_n))
    rev = features.reversal_bars(m5)
    close = m5["c"].to_numpy(dtype=float)

    def series(values) -> pd.Series:
        return pd.Series(values, index=known_at, dtype=float)

    return {
        "long": (
            series(last["last_sl_price"].to_numpy()),
            series(np.where(rev["bear_reversal"].to_numpy(), close, np.nan)),
        ),
        "short": (
            series(last["last_sh_price"].to_numpy()),
            series(np.where(rev["bull_reversal"].to_numpy(), close, np.nan)),
        ),
    }
