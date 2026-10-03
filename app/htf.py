"""Higher-timeframe (H1, D1) trend state and the H1/D1 alignment rule.

Lorant skips an instrument when H1 and D1 disagree. The state of each timeframe is a vote of
three simple, deterministic checks on CLOSED bars only (a bar counts once its open time plus
the timeframe length is <= `asof`, so no still-forming H1/D1 bar leaks in):

  slope      EMA20 now vs `slope_bars` bars ago: up +1, down -1; a change smaller than
             `flat_tol_atr` x ATR14 counts as flat (0), so sideways noise is not a slope
  price      last close above the EMA +1, below -1
  structure  last two confirmed swing highs and lows: higher high AND higher low +1,
             lower high AND lower low -1, anything else (or fewer than two of each) 0.
             "Higher/lower" means by more than the same ATR tolerance.

Sum of votes >= `min_votes` (default 2) -> bull, <= -min_votes -> bear, else neutral.
"""

from dataclasses import dataclass

import pandas as pd

from app.features import atr, confirmed_swings, ema

H1_SEC = 3600
D1_SEC = 86400


@dataclass(frozen=True)
class TrendState:
    state: str  # "bull" | "bear" | "neutral"
    slope: int
    price: int
    structure: int


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


def _structure_vote(swings: pd.DataFrame, tol: float) -> int:
    highs = swings["sh_price"].dropna().to_numpy()
    lows = swings["sl_price"].dropna().to_numpy()
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh, hl = highs[-1] - highs[-2] > tol, lows[-1] - lows[-2] > tol
    lh, ll = highs[-2] - highs[-1] > tol, lows[-2] - lows[-1] > tol
    return 1 if hh and hl else -1 if lh and ll else 0


def htf_state(
    df: pd.DataFrame,
    tf_seconds: int,
    asof: pd.Timestamp,
    ema_period: int = 20,
    swing_n: int = 2,
    slope_bars: int = 3,
    min_votes: int = 2,
    flat_tol_atr: float = 0.15,
) -> TrendState:
    """Trend state of one timeframe as of `asof` (UTC). Neutral if there is too little history."""
    closed = df[df.index + pd.Timedelta(seconds=tf_seconds) <= asof]
    if len(closed) < ema_period + slope_bars:
        return TrendState("neutral", 0, 0, 0)
    e = ema(closed["c"], ema_period)
    delta = e.iloc[-1] - e.iloc[-1 - slope_bars]
    tol = flat_tol_atr * atr(closed).iloc[-1]  # NaN tolerance (short history) -> never flat
    slope = 0 if abs(delta) < tol else _sign(delta)
    price = _sign(closed["c"].iloc[-1] - e.iloc[-1])
    structure = _structure_vote(confirmed_swings(closed, swing_n), 0.0 if tol != tol else tol)
    total = slope + price + structure
    state = "bull" if total >= min_votes else "bear" if total <= -min_votes else "neutral"
    return TrendState(state, slope, price, structure)


def alignment(h1: str, d1: str, neutral_is_conflict: bool = True) -> str:
    """aligned_bull | aligned_bear | conflict.

    Opposite directions are always a conflict. A neutral timeframe counts as a conflict when
    `neutral_is_conflict` (config `rules.htf_neutral_counts_as_conflict`); otherwise one
    neutral plus one directional timeframe follows the directional one. Both neutral is
    always a conflict.
    """
    if h1 == d1 == "bull":
        return "aligned_bull"
    if h1 == d1 == "bear":
        return "aligned_bear"
    if "neutral" in (h1, d1) and not neutral_is_conflict and h1 != d1:
        direction = h1 if h1 != "neutral" else d1
        return "aligned_bull" if direction == "bull" else "aligned_bear"
    return "conflict"
