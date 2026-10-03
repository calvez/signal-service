"""Al Brooks price-action mechanics, computed deterministically (no LLM involved).

Input everywhere: a DataFrame of CLOSED bars, oldest first, with a tz-aware UTC DatetimeIndex
(the bar OPEN time) and float columns `o h l c`.

NO LOOKAHEAD: every value in row t is computed from rows <= t only. Swing points are therefore
reported on the row where they become *confirmed* (N bars after the swing bar), never on the
swing bar itself. tests/test_features.py checks this by changing every bar after t and asserting
that nothing at or before t moves.

Definitions live in the docstrings so they can be checked against Brooks.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Tunable thresholds (named so they are easy to find and change).
TREND_BAR_BODY_MIN = 0.50  # body / range at least this for a trend bar
TREND_BAR_CLOSE_OUTER = 1 / 3  # close within the outer third of the range
DOJI_BODY_MAX = 0.25  # body / range at most this
SIGNAL_CLOSE_NEAR_EXTREME = 2 / 3  # signal bar closes in the top (bottom) third
SIGNAL_TAIL_MAX = 0.25  # tail on the trade side at most this fraction of the range
SIGNAL_RANGE_MAX_ATR = 2.0  # signal bar no bigger than this many ATRs


# --------------------------------------------------------------------------- indicators
def ema(close: pd.Series, period: int = 20) -> pd.Series:
    """Exponential moving average, alpha = 2 / (period + 1), seeded with the first close
    (the recursion MetaTrader uses). Early values are less reliable: feed >= 3 x period bars."""
    return close.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range, Wilder smoothing (alpha = 1 / period).

    True range = max(high - low, |high - previous close|, |low - previous close|); the first
    bar uses high - low. NaN until `period` bars exist.
    """
    prev_c = df["c"].shift(1)
    tr = pd.concat(
        [df["h"] - df["l"], (df["h"] - prev_c).abs(), (df["l"] - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# --------------------------------------------------------------------------- bar types
def classify_bars(df: pd.DataFrame, atr_s: pd.Series | None = None) -> pd.DataFrame:
    """Per-bar shape columns (all use the bar itself and the previous bar only).

    range        high - low
    body_ratio   |close - open| / range            (0 for a zero-range bar)
    close_pos    (close - low) / range, 0 = closed on the low, 1 = on the high (0.5 if no range)
    trend_bull   close > open, body_ratio >= 0.5 and close in the top third
    trend_bear   close < open, body_ratio >= 0.5 and close in the bottom third
    doji         body_ratio <= 0.25
    inside       high <= previous high and low >= previous low (not identical to it)
    outside      high > previous high and low < previous low
    bar_type     text label for the prompt: bull_trend | bear_trend | doji | bull | bear,
                 plus "/inside" or "/outside" when that applies
    sig_long     usable buy signal bar: bull bar (close > open), close in the top third,
                 upper tail <= 25 % of the range, range <= 2 ATR
    sig_short    mirror image for sells
    """
    o, h, low, c = df["o"], df["h"], df["l"], df["c"]
    rng = h - low
    safe = rng.where(rng > 0)
    body_ratio = ((c - o).abs() / safe).fillna(0.0)
    close_pos = ((c - low) / safe).fillna(0.5)
    upper_tail = ((h - np.maximum(o, c)) / safe).fillna(0.0)
    lower_tail = ((np.minimum(o, c) - low) / safe).fillna(0.0)

    out = pd.DataFrame(index=df.index)
    out["range"] = rng
    out["body_ratio"] = body_ratio
    out["close_pos"] = close_pos
    out["trend_bull"] = (
        (c > o) & (body_ratio >= TREND_BAR_BODY_MIN) & (close_pos >= 1 - TREND_BAR_CLOSE_OUTER)
    )
    out["trend_bear"] = (
        (c < o) & (body_ratio >= TREND_BAR_BODY_MIN) & (close_pos <= TREND_BAR_CLOSE_OUTER)
    )
    out["doji"] = body_ratio <= DOJI_BODY_MAX
    ph, pl = h.shift(1), low.shift(1)
    out["inside"] = (h <= ph) & (low >= pl) & ((h < ph) | (low > pl))
    out["outside"] = (h > ph) & (low < pl)

    base = np.select(
        [out["trend_bull"], out["trend_bear"], out["doji"], c >= o],
        ["bull_trend", "bear_trend", "doji", "bull"],
        default="bear",
    )
    suffix = np.where(out["inside"], "/inside", np.where(out["outside"], "/outside", ""))
    out["bar_type"] = pd.Series(base, index=df.index) + pd.Series(suffix, index=df.index)

    if atr_s is None:
        atr_s = atr(df)
    small_enough = rng <= SIGNAL_RANGE_MAX_ATR * atr_s  # False while ATR is NaN
    out["sig_long"] = (
        (c > o)
        & (close_pos >= SIGNAL_CLOSE_NEAR_EXTREME)
        & (upper_tail <= SIGNAL_TAIL_MAX)
        & small_enough
    )
    out["sig_short"] = (
        (c < o)
        & (close_pos <= 1 - SIGNAL_CLOSE_NEAR_EXTREME)
        & (lower_tail <= SIGNAL_TAIL_MAX)
        & small_enough
    )
    return out


# --------------------------------------------------------------------------- swings
def confirmed_swings(df: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    """Swing highs/lows, reported when they become CONFIRMED.

    Swing high at bar i: high[i] >= the highs of the n bars before it and > the highs of the
    n bars after it. Swing low is the mirror image. (Ties on the left allowed, on the right not,
    so of two equal highs the later one is the swing.)

    The swing is only known once bar i+n has closed, so it appears on row i+n, never earlier.
    Columns, NaN except on confirmation rows:
      sh_price, sh_time   the swing high's price and the open time of its bar
      sl_price, sl_time   same for swing lows
    """
    h, low = df["h"].to_numpy(), df["l"].to_numpy()
    size = len(df)
    sh_p = np.full(size, np.nan)
    sl_p = np.full(size, np.nan)
    sh_t = np.full(size, np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    sl_t = np.full(size, np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    times = df.index.tz_convert("UTC").tz_localize(None).to_numpy(dtype="datetime64[ns]")
    for i in range(n, size - n):
        j = i + n  # confirmation row
        if h[i] >= h[i - n : i].max() and h[i] > h[i + 1 : j + 1].max():
            sh_p[j], sh_t[j] = h[i], times[i]
        if low[i] <= low[i - n : i].min() and low[i] < low[i + 1 : j + 1].min():
            sl_p[j], sl_t[j] = low[i], times[i]
    out = pd.DataFrame(
        {"sh_price": sh_p, "sl_price": sl_p, "sh_time": sh_t, "sl_time": sl_t}, index=df.index
    )
    for col in ("sh_time", "sl_time"):
        out[col] = pd.to_datetime(out[col], utc=True)
    return out


def last_swings(swings: pd.DataFrame) -> pd.DataFrame:
    """The most recent confirmed swing high/low known at each row (forward-filled)."""
    return swings.ffill().rename(
        columns={
            "sh_price": "last_sh_price",
            "sh_time": "last_sh_time",
            "sl_price": "last_sl_price",
            "sl_time": "last_sl_time",
        }
    )


@dataclass(frozen=True)
class Swing:
    kind: str  # "high" | "low"
    time: pd.Timestamp  # open time of the swing bar
    price: float


def recent_swings(swings: pd.DataFrame, k: int = 6) -> list[Swing]:
    """The last k confirmed swings (as of the last row), oldest first, for the prompt."""
    found: list[Swing] = []
    for kind, p, t in (("high", "sh_price", "sh_time"), ("low", "sl_price", "sl_time")):
        sub = swings[[p, t]].dropna()
        found += [Swing(kind, row[t], float(row[p])) for _, row in sub.iterrows()]
    found.sort(key=lambda s: s.time)
    return found[-k:]


# --------------------------------------------------------------------------- leg counts
def leg_counts(
    df: pd.DataFrame, last_sl: pd.Series, last_sh: pd.Series, day: pd.Series | None = None
) -> pd.DataFrame:
    """Brooks pullback-leg counting: H1/H2 in a bull trend, L1/L2 in a bear trend.

    **Rules (for Lorant to review - see docs/progress.md):**

    Bull side ("H" count). State per day: `ref_high` (highest high so far), `pullback` flag,
    `h_count`. For each bar, in this order:
      1. STRUCTURE BREAK - low < last confirmed swing low -> bull structure is broken:
         h_count = 0, pullback = False, ref_high = this bar's high.
      2. NEW EXTREME - high > ref_high -> the trend leg resumed: ref_high = high,
         h_count = 0, pullback = False.
      3. PULLBACK - high < previous bar's high -> pullback = True.
      4. H BAR - high > previous bar's high while pullback is True (and high <= ref_high):
         h_count += 1 and pullback = False. This bar is "H1", "H2", ...
    The first bar of each day starts fresh (counts 0, ref_high = its high).

    Bear side ("L" count) is the exact mirror: structure break = high > last confirmed swing
    high; new extreme = low < ref_low; pullback = low > previous low; L bar = low < previous
    low while pullback.

    The structure-break rule is also what keeps the counters honest: in a bear trend every new
    low undercuts the last swing low, so the bull "H" count keeps resetting to 0.

    `last_sl` / `last_sh`: price of the last confirmed swing low/high known at each row
    (NaN if none yet). `day`: label per row; counters reset when it changes (default: one day).

    Columns: h_count, h_bar (this bar is an H bar), l_count, l_bar.
    """
    h, low = df["h"].to_numpy(), df["l"].to_numpy()
    sl, sh = last_sl.to_numpy(), last_sh.to_numpy()
    days = np.zeros(len(df), dtype=int) if day is None else day.to_numpy()
    size = len(df)
    h_count, l_count = np.zeros(size, dtype=int), np.zeros(size, dtype=int)
    h_bar, l_bar = np.zeros(size, dtype=bool), np.zeros(size, dtype=bool)

    hc = lc = 0
    h_pb = l_pb = False
    ref_high = ref_low = np.nan
    for i in range(size):
        if i == 0 or days[i] != days[i - 1]:
            hc = lc = 0
            h_pb = l_pb = False
            ref_high, ref_low = h[i], low[i]
        else:
            # --- bull side
            if not np.isnan(sl[i]) and low[i] < sl[i]:
                hc, h_pb, ref_high = 0, False, h[i]
            elif h[i] > ref_high:
                hc, h_pb, ref_high = 0, False, h[i]
            elif h[i] < h[i - 1]:
                h_pb = True
            elif h[i] > h[i - 1] and h_pb:
                hc, h_pb = hc + 1, False
                h_bar[i] = True
            # --- bear side
            if not np.isnan(sh[i]) and h[i] > sh[i]:
                lc, l_pb, ref_low = 0, False, low[i]
            elif low[i] < ref_low:
                lc, l_pb, ref_low = 0, False, low[i]
            elif low[i] > low[i - 1]:
                l_pb = True
            elif low[i] < low[i - 1] and l_pb:
                lc, l_pb = lc + 1, False
                l_bar[i] = True
        h_count[i], l_count[i] = hc, lc
    return pd.DataFrame(
        {"h_count": h_count, "h_bar": h_bar, "l_count": l_count, "l_bar": l_bar}, index=df.index
    )


# --------------------------------------------------------------------------- day context
def prior_close(df: pd.DataFrame, cutoff_utc: pd.Timestamp, bar_seconds: int = 300) -> float | None:
    """Close of the last bar that had closed by `cutoff_utc` (e.g. the previous cash close)."""
    closed = df[df.index + pd.Timedelta(seconds=bar_seconds) <= cutoff_utc]
    return None if closed.empty else float(closed["c"].iloc[-1])


def _side_changes(side: np.ndarray) -> int:
    nz = side[side != 0]
    return int((nz[1:] != nz[:-1]).sum()) if len(nz) > 1 else 0


def day_context(
    df: pd.DataFrame,
    session_start: pd.Timestamp,
    opening_range_bars: int = 6,
    prior_close_price: float | None = None,
) -> dict | None:
    """Context of the session day as of the LAST row of `df` (which needs an `ema` column).

    Bars before `session_start` are ignored for the day statistics. None if the session has
    no bar yet. Keys:
      session_open        open of the first bar of the session
      or_high, or_low     high/low of the first `opening_range_bars` bars (only those that
                          exist so far); or_complete tells whether all of them have closed
      day_high, day_low   extremes since the session start
      pct_in_range        last close in today's range, 0 = at the low, 100 = at the high
      ema_crosses         times the close changed side of the EMA since the session start
      bars_same_side      consecutive bars (counting back from now, across the day boundary)
                          closing on the same side of the EMA as the last bar; 0 if on the EMA
      gap_pts             session open minus prior close (None if unknown)
    """
    d = df[df.index >= session_start]
    if d.empty:
        return None
    first = d.iloc[:opening_range_bars]
    hi, lo, last_close = d["h"].max(), d["l"].min(), d["c"].iloc[-1]
    side_day = np.sign((d["c"] - d["ema"]).to_numpy())
    side_all = np.sign((df["c"] - df["ema"]).to_numpy())
    same = 0
    if side_all[-1] != 0:
        for s in side_all[::-1]:
            if s != side_all[-1]:
                break
            same += 1
    return {
        "session_open": float(d["o"].iloc[0]),
        "or_high": float(first["h"].max()),
        "or_low": float(first["l"].min()),
        "or_complete": len(d) >= opening_range_bars,
        "day_high": float(hi),
        "day_low": float(lo),
        "pct_in_range": float(50.0 if hi == lo else (last_close - lo) / (hi - lo) * 100),
        "ema_crosses": _side_changes(side_day),
        "bars_same_side": same,
        "gap_pts": None if prior_close_price is None else float(d["o"].iloc[0] - prior_close_price),
    }


def day_type_hint(df: pd.DataFrame, ctx: dict | None, atr_now: float) -> str:
    """Deterministic hint for the LLM and the validator, NOT a verdict. One of:

    tight_channel  last 10 bars all closed on one side of the EMA and at most 2 of them
                   touched it (low <= EMA for bulls, high >= EMA for bears)
    trading_range  the close crossed the EMA 3+ times today
    trend          at most 1 EMA cross, today's range >= 3 ATR and price in the top or
                   bottom quarter of it
    unclear        everything else, or fewer than 6 bars / no ATR yet
    """
    if ctx is None or np.isnan(atr_now) or len(df) < 6:
        return "unclear"
    if ctx["bars_same_side"] >= 10:
        last = df.tail(10)
        bull = last["c"].iloc[-1] > last["ema"].iloc[-1]
        touches = (last["l"] <= last["ema"]).sum() if bull else (last["h"] >= last["ema"]).sum()
        if touches <= 2:
            return "tight_channel"
    if ctx["ema_crosses"] >= 3:
        return "trading_range"
    pct = ctx["pct_in_range"]
    wide = (ctx["day_high"] - ctx["day_low"]) >= 3 * atr_now
    if ctx["ema_crosses"] <= 1 and wide and (pct >= 75 or pct <= 25):
        return "trend"
    return "unclear"


# --------------------------------------------------------------------------- assembly
def compute_features(
    df: pd.DataFrame,
    ema_period: int = 20,
    atr_period: int = 14,
    swing_n: int = 2,
    day: pd.Series | None = None,
) -> pd.DataFrame:
    """All per-bar feature columns in one frame (ema, atr, bar shapes, swings, leg counts)."""
    out = df[["o", "h", "l", "c"]].copy()
    out["ema"] = ema(df["c"], ema_period)
    out["atr"] = atr(df, atr_period)
    shapes = classify_bars(df, out["atr"])
    swings = confirmed_swings(df, swing_n)
    last = last_swings(swings)
    legs = leg_counts(df, last["last_sl_price"], last["last_sh_price"], day)
    return pd.concat([out, shapes, swings, last, legs], axis=1)
