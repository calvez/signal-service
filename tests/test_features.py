import numpy as np
import pandas as pd
import pytest

from app import features as F

T0 = pd.Timestamp("2026-10-05 07:00", tz="UTC")


def make_df(rows, start=T0):
    """rows: (o, h, l, c) tuples -> M5 frame."""
    idx = pd.date_range(start, periods=len(rows), freq="5min")
    return pd.DataFrame(rows, columns=list("ohlc"), index=idx, dtype=float)


def from_hl(pairs, start=T0):
    """Bars given only as (high, low); open = close = midpoint."""
    return make_df([((h + low) / 2, h, low, (h + low) / 2) for h, low in pairs], start)


def random_df(n=120, seed=7):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.2, n)
    h = np.maximum(o, c) + rng.uniform(0, 1, n)
    low = np.minimum(o, c) - rng.uniform(0, 1, n)
    return make_df(list(zip(o, h, low, c, strict=True)))


# ---------------------------------------------------------------- indicators
def test_ema_constant_and_step():
    s = pd.Series([10.0] * 30)
    assert F.ema(s, 20).iloc[-1] == pytest.approx(10.0)
    step = pd.Series([0.0, 21.0])
    assert F.ema(step, 20).iloc[1] == pytest.approx(2.0)  # alpha = 2/21 -> 21 * 2/21


def test_atr_constant_range_is_that_range():
    df = make_df([(10, 12, 10, 11)] * 30)  # range 2, no gaps
    a = F.atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert a.iloc[13:].eq(2.0).all()


def test_atr_counts_gaps():
    df = make_df([(10, 11, 9, 10)] * 14 + [(20, 21, 19, 20)])  # gap up 10
    a = F.atr(df, 14)
    # last true range = |21 - 10| = 11; previous ATR was 2 -> (13 * 2 + 11) / 14
    assert a.iloc[-1] == pytest.approx((13 * 2 + 11) / 14)


# ---------------------------------------------------------------- bar types
def test_classification_cases():
    rows = [
        (10, 20, 10, 20),  # bull trend bar: body 100 %, closes on high
        (20, 20, 10, 10),  # bear trend bar
        (15, 20, 10, 15.5),  # doji (body 5 %)
        (15, 19, 11, 18),  # inside the previous bar (19 <= 20, 11 >= 10); plain bull bar
        (6, 25, 5, 22),  # outside bar (25 > 19, 5 < 11), strong bull body
    ]
    t = F.classify_bars(make_df(rows))
    assert t["bar_type"].tolist() == [
        "bull_trend",
        "bear_trend",
        "doji",
        "bull/inside",
        "bull_trend/outside",
    ]
    assert t["trend_bull"].tolist() == [True, False, False, False, True]
    assert t["trend_bear"].tolist() == [False, True, False, False, False]
    assert t["doji"].tolist() == [False, False, True, False, False]
    assert t["inside"].tolist() == [False, False, False, True, False]
    assert t["outside"].tolist() == [False, False, False, False, True]


def test_bar_type_labels():
    t = F.classify_bars(make_df([(10, 20, 10, 20), (20, 20, 10, 10), (15, 20, 10, 15.5)]))
    assert t["bar_type"].tolist() == ["bull_trend", "bear_trend", "doji"]


def test_zero_range_bar_is_safe():
    t = F.classify_bars(make_df([(10, 10, 10, 10), (10, 10, 10, 10)]))
    assert t["close_pos"].tolist() == [0.5, 0.5]
    assert t["doji"].all()
    assert not t["inside"].iloc[1]  # an identical bar is not an inside bar


def test_signal_bar_quality():
    base = [(100, 101, 99, 100)] * 20  # ATR ~ 2
    good_long = (100, 102, 100, 101.9)  # bull, closes near high, tiny top tail
    tailed_long = (100, 104, 100, 101.0)  # closes in the lower part with a big top tail
    huge_long = (100, 110, 100, 109.9)  # good shape but 5 x ATR
    good_short = (101.9, 102, 100, 100.1)
    df = make_df(base + [good_long, tailed_long, huge_long, good_short])
    t = F.classify_bars(df)
    tail = t.iloc[20:]
    assert tail["sig_long"].tolist() == [True, False, False, False]
    assert tail["sig_short"].tolist() == [False, False, False, True]


# ---------------------------------------------------------------- swings
def test_swing_high_appears_only_after_n_right_bars_close():
    # highs: peak of 20 at index 4, n = 2 -> confirmed on row 6
    df = from_hl([(10, 5), (12, 6), (15, 8), (18, 9), (20, 11), (17, 10), (16, 9), (15, 8)])
    sw = F.confirmed_swings(df, 2)
    assert sw["sh_price"].notna().tolist() == [False] * 6 + [True, False]
    assert sw["sh_price"].iloc[6] == 20
    assert sw["sh_time"].iloc[6] == df.index[4]
    # the same data truncated right after the peak shows nothing yet
    assert F.confirmed_swings(df.iloc[:6], 2)["sh_price"].isna().all()


def test_swing_low_and_equal_highs_pick_the_later_bar():
    df = from_hl([(10, 9), (10, 8), (10, 6), (10, 8), (10, 9), (11, 10)])
    sw = F.confirmed_swings(df, 2)
    assert sw["sl_price"].iloc[4] == 6 and sw["sl_time"].iloc[4] == df.index[2]
    flat = from_hl([(10, 5), (10, 5), (20, 5), (20, 5), (10, 5), (10, 5), (9, 5)])
    s = F.confirmed_swings(flat, 2)
    assert s["sh_time"].dropna().tolist() == [flat.index[3]]  # later of the two 20s


def test_recent_swings_order():
    df = from_hl([(10, 9), (10, 8), (10, 6), (10, 8), (10, 9), (14, 10), (12, 9), (11, 8), (10, 7)])
    sw = F.confirmed_swings(df, 2)
    kinds = [(s.kind, s.price) for s in F.recent_swings(sw)]
    assert ("low", 6.0) in kinds and ("high", 14.0) in kinds
    assert [s.time for s in F.recent_swings(sw)] == sorted(s.time for s in F.recent_swings(sw))


# ---------------------------------------------------------------- leg counts
NAN = pd.Series(dtype=float)


def counts(pairs, last_sl=None, last_sh=None, day=None):
    df = from_hl(pairs)
    nan = pd.Series(np.nan, index=df.index)
    return F.leg_counts(
        df, nan if last_sl is None else last_sl, nan if last_sh is None else last_sh, day
    )


def test_h1_h2_then_new_high_resets():
    pairs = [(10, 8), (12, 9), (14, 11), (13, 10.5), (13.5, 11), (13, 10.8), (13.8, 11.2), (15, 12)]
    r = counts(pairs)
    assert r["h_count"].tolist() == [0, 0, 0, 0, 1, 1, 2, 0]
    assert r["h_bar"].tolist() == [False, False, False, False, True, False, True, False]


def test_l1_l2_mirror():
    pairs = [(12, 10), (11, 8), (9, 6), (9.5, 7), (9, 6.5), (9.2, 7.2), (8.8, 6.4), (8, 5)]
    r = counts(pairs)
    # bounce (low above previous low) arms the pullback; the next lower low is L1, etc.
    assert r["l_count"].tolist() == [0, 0, 0, 0, 1, 1, 2, 0]
    assert r["l_bar"].iloc[4] and r["l_bar"].iloc[6]


def test_structure_break_resets_h_count():
    pairs = [(10, 8), (12, 9), (14, 11), (13, 10.5), (13.5, 11), (13.2, 7)]  # last bar undercuts
    df = from_hl(pairs)
    last_sl = pd.Series([np.nan] * 5 + [10.0], index=df.index)  # a confirmed swing low at 10
    r = F.leg_counts(df, last_sl, pd.Series(np.nan, index=df.index))
    assert r["h_count"].tolist() == [0, 0, 0, 0, 1, 0]


def test_counts_reset_each_day():
    pairs = [(10, 8), (12, 9), (11, 8.5), (11.5, 9)]
    df = from_hl(pairs)
    day = pd.Series([1, 1, 1, 2], index=df.index)
    nan = pd.Series(np.nan, index=df.index)
    r = F.leg_counts(df, nan, nan, day)
    assert r["h_count"].tolist() == [0, 0, 0, 0]  # H1 on the last bar is cancelled by the new day
    same_day = F.leg_counts(df, nan, nan)
    assert same_day["h_count"].tolist() == [0, 0, 0, 1]


def test_bear_trend_keeps_h_count_at_zero_via_structure_break():
    full = F.compute_features(
        from_hl([(100 - i + (1 if i % 3 == 0 else 0), 98 - i) for i in range(30)]), swing_n=2
    )
    assert full["h_count"].iloc[-1] == 0


# ---------------------------------------------------------------- day context
def prepared(rows_hl):
    df = from_hl(rows_hl)
    df["ema"] = F.ema(df["c"], 20)
    return df


def test_day_context_values():
    pairs = [(10, 8), (12, 9), (14, 11), (13, 10), (15, 12), (16, 13), (17, 14), (18, 15)]
    df = prepared(pairs)
    ctx = F.day_context(df, df.index[0], opening_range_bars=6, prior_close_price=8.0)
    assert ctx["session_open"] == 9.0
    assert (ctx["or_high"], ctx["or_low"], ctx["or_complete"]) == (16, 8, True)
    assert (ctx["day_high"], ctx["day_low"]) == (18, 8)
    assert ctx["pct_in_range"] == pytest.approx((16.5 - 8) / 10 * 100)
    assert ctx["gap_pts"] == 1.0
    assert ctx["bars_same_side"] >= 1


def test_day_context_ignores_bars_before_session_and_handles_partial_or():
    pairs = [(50, 40)] * 5 + [(10, 8), (12, 9)]
    df = prepared(pairs)
    ctx = F.day_context(df, df.index[5], opening_range_bars=6)
    assert (ctx["day_high"], ctx["day_low"]) == (12, 8)
    assert ctx["or_complete"] is False
    assert ctx["gap_pts"] is None
    assert F.day_context(df, df.index[-1] + pd.Timedelta("5min")) is None


def test_ema_crosses_and_same_side():
    c = [10, 12, 8, 12, 8, 12]
    df = make_df([(x, x + 0.5, x - 0.5, x) for x in c])
    df["ema"] = [10, 10, 10, 10, 10, 10.0]
    ctx = F.day_context(df, df.index[0])
    assert ctx["ema_crosses"] == 4  # 12 -> 8 -> 12 -> 8 -> 12 (the first bar sits on the EMA)
    assert ctx["bars_same_side"] == 1


def test_prior_close_uses_only_bars_closed_by_cutoff():
    df = from_hl([(10, 8), (11, 9), (12, 10)])
    cutoff = df.index[1] + pd.Timedelta("5min")  # bar 1 has just closed, bar 2 has not
    assert F.prior_close(df, cutoff) == 10.0
    assert F.prior_close(df, df.index[0]) is None


def test_day_type_hints():
    # tight bull channel: 14 bars always above a rising EMA, never touching it
    up = prepared([(100 + i, 99 + i) for i in range(30)])
    ctx = F.day_context(up, up.index[0])
    assert F.day_type_hint(up, ctx, atr_now=1.0) == "tight_channel"
    # chop around the EMA
    chop = prepared([(101 + (i % 2) * 2, 99 + (i % 2) * 2) for i in range(30)])
    chop["ema"] = 101.0
    ctx = F.day_context(chop, chop.index[0])
    assert F.day_type_hint(chop, ctx, atr_now=1.0) == "trading_range"
    # too little data / no ATR
    assert F.day_type_hint(up.iloc[:3], F.day_context(up.iloc[:3], up.index[0]), 1.0) == "unclear"
    assert F.day_type_hint(up, ctx, float("nan")) == "unclear"
    assert F.day_type_hint(up, None, 1.0) == "unclear"


# ---------------------------------------------------------------- NO LOOKAHEAD
@pytest.mark.parametrize("t", [30, 55, 80, 100])
def test_compute_features_has_no_lookahead(t):
    """Rewrite every bar after t: nothing at or before t may change."""
    df = random_df()
    day = pd.Series((np.arange(len(df)) // 40), index=df.index)
    base = F.compute_features(df, day=day)

    future = df.copy()
    rng = np.random.default_rng(99)
    junk = 100 + np.cumsum(rng.normal(0, 5, len(df) - t - 1))
    future.iloc[t + 1 :, future.columns.get_loc("c")] = junk
    future.iloc[t + 1 :, future.columns.get_loc("o")] = junk + 1
    future.iloc[t + 1 :, future.columns.get_loc("h")] = junk + 4
    future.iloc[t + 1 :, future.columns.get_loc("l")] = junk - 4
    changed = F.compute_features(future, day=day)

    pd.testing.assert_frame_equal(base.iloc[: t + 1], changed.iloc[: t + 1])


@pytest.mark.parametrize("t", [30, 80])
def test_truncated_history_gives_same_rows(t):
    """Computing on bars[:t+1] equals computing on everything, for row t."""
    df = random_df()
    full = F.compute_features(df)
    cut = F.compute_features(df.iloc[: t + 1])
    pd.testing.assert_frame_equal(full.iloc[: t + 1], cut)


def test_day_context_has_no_lookahead():
    df = prepared([(100 + (i % 7), 98 + (i % 5)) for i in range(60)])
    start = df.index[10]
    a = F.day_context(df.iloc[:40], start)
    future = df.copy()
    future.iloc[40:, future.columns.get_loc("h")] = 999.0
    future.iloc[40:, future.columns.get_loc("l")] = 1.0
    b = F.day_context(future.iloc[:40], start)
    assert a == b


# ---------------------------------------------------------------- 60-minute EMA on the 5-minute chart
def h1_frame(n=40, start="2026-10-04 00:00"):
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    c = 100 + np.arange(n, dtype=float)
    return pd.DataFrame({"o": c, "h": c + 1, "l": c - 1, "c": c}, index=idx)


def test_htf_ema_uses_only_closed_hourly_bars():
    h1 = h1_frame()
    h1_ema = F.ema(h1["c"], 20)
    m5 = pd.date_range("2026-10-05 10:00", "2026-10-05 11:55", freq="5min", tz="UTC")
    line = F.htf_ema_on_ltf(m5, h1, 20)
    # the 10:00 H1 bar closes at 11:00; the 5-minute bar 10:55-11:00 is the first to know it
    assert (
        line[pd.Timestamp("2026-10-05 10:50", tz="UTC")]
        == h1_ema[pd.Timestamp("2026-10-05 09:00", tz="UTC")]
    )
    assert (
        line[pd.Timestamp("2026-10-05 10:55", tz="UTC")]
        == h1_ema[pd.Timestamp("2026-10-05 10:00", tz="UTC")]
    )
    # constant within the hour: a step line
    assert line["2026-10-05 11:00":"2026-10-05 11:50"].nunique() == 1


def test_htf_ema_has_no_lookahead():
    h1 = h1_frame()
    m5 = pd.date_range("2026-10-05 10:00", "2026-10-05 11:55", freq="5min", tz="UTC")
    base = F.htf_ema_on_ltf(m5, h1, 20)
    wrecked = h1.copy()
    # The last 5-minute bar (11:55) closes at 12:00, together with the 11:00 hour, so that hour
    # is legitimately known. Everything from the 12:00 hour on is still forming: change it.
    wrecked.loc["2026-10-05 12:00":, ["o", "h", "l", "c"]] = 9999.0
    changed = F.htf_ema_on_ltf(m5, wrecked, 20)
    pd.testing.assert_series_equal(base, changed)
    # and changing the 11:00 hour DOES move the last value: the boundary is exact
    wrecked.loc["2026-10-05 11:00", ["o", "h", "l", "c"]] = 9999.0
    moved = F.htf_ema_on_ltf(m5, wrecked, 20)
    assert moved.iloc[-1] != base.iloc[-1] and moved.iloc[:-1].equals(base.iloc[:-1])


def test_htf_ema_before_any_closed_hour_is_nan():
    h1 = h1_frame(n=2, start="2026-10-05 10:00")
    m5 = pd.date_range("2026-10-05 10:00", periods=3, freq="5min", tz="UTC")
    assert F.htf_ema_on_ltf(m5, h1, 20).isna().all()
    assert F.htf_ema_on_ltf(m5, h1.iloc[0:0], 20).isna().all()


def test_leg_counts_arms_the_pullback_flag_for_entries():
    """h_pb marks "a buy stop above this bar's high would become the next H bar"."""
    # up, pullback, H1, pullback again, H2
    pairs = [(10, 8), (12, 9), (14, 11), (13, 10.5), (13.5, 11), (13.2, 10.8), (13.8, 11.2)]
    r = counts(pairs)
    assert r["h_count"].tolist() == [0, 0, 0, 0, 1, 1, 2]
    assert r["h_bar"].tolist() == [False, False, False, False, True, False, True]
    # bar 3 is a lower high -> armed, a break above it would be H1
    # bar 5 is a lower high after H1 -> armed, a break above it would be H2 (the signal bar)
    assert r["h_pb"].tolist() == [False, False, False, True, False, True, False]


def test_leg_counts_pullback_flag_mirrors_on_the_bear_side():
    pairs = [(12, 10), (11, 8), (9, 6), (9.5, 7), (9, 6.5), (9.2, 7.2), (8.8, 6.4)]
    r = counts(pairs)
    assert r["l_count"].tolist() == [0, 0, 0, 0, 1, 1, 2]
    assert r["l_pb"].tolist() == [False, False, False, True, False, True, False]
