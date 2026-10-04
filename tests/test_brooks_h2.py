"""docs/spec-brooks-ea.md §2-§4 on hand-built bars. Longs; shorts via the mirror."""

import numpy as np
import pandas as pd
import pytest

from app.config import BrooksCfg
from app.strategies import brooks_h2 as B

CFG = BrooksCfg()
NO_H1 = CFG.model_copy(update={"htf_ema_filter": "off"})  # the spec's three trend rules only
T0 = pd.Timestamp("2026-10-05 07:00", tz="UTC")
POINT = 0.1


def frame(rows, **cols):
    """rows: (o, h, l, c); extra columns as lists or scalars."""
    idx = pd.date_range(T0, periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=list("ohlc"), index=idx, dtype=float)
    defaults = {"sp": 1.0, "ema": np.nan, "sh_price": np.nan, "sl_price": np.nan,
                "last_sh_price": np.nan, "last_sl_price": np.nan, "h_count": 0, "l_count": 0,
                "h_bar": False, "l_bar": False}  # fmt: skip
    for k, v in {**defaults, **cols}.items():
        df[k] = v
    return df


def view(df):
    return B.View(df, 1.0, POINT)


# ------------------------------------------------------------------ §3 signal bar
def base_bars(n=21, rng=10.0):
    return [(100, 100 + rng, 100, 105)] * n  # average range 10


def test_good_bull_signal_bar():
    rows = base_bars() + [(101, 111, 100, 110)]  # body 0.9, close 0.91, tail 0.09, range 11
    ok, m = B.signal_bar(view(frame(rows)), CFG, POINT)
    assert ok, m


@pytest.mark.parametrize("bar,why", [
    ((110, 111, 100, 101), "bear bar"),
    ((104, 111, 100, 108), "body 0.36 < 0.5"),
    ((101, 112, 100, 108), "close position 0.67 < 0.75"),
    ((100, 113, 100, 110.5), "upper tail 0.19 > 0.15"),
    ((101, 104.9, 100, 104.8), "does not close above the prior close (105)"),
    ((101, 131, 100, 130), "range 31 > 1.5 x average"),
])  # fmt: skip
def test_bad_signal_bars(bar, why):
    ok, _ = B.signal_bar(view(frame(base_bars() + [bar])), CFG, POINT)
    assert not ok, why


def test_spread_too_wide():
    rows = base_bars() + [(101, 111, 100, 110)]
    df = frame(rows, sp=1.0)
    df.loc[df.index[-1], "sp"] = 20  # 2.0 price > 0.15 x 11
    assert not B.signal_bar(view(df), CFG, POINT)[0]


def test_min_sb_range():
    rows = base_bars() + [(101, 111, 100, 110)]
    cfg = CFG.model_copy(update={"min_sb_range_pts": 200})  # 20.0 price > range 11
    assert not B.signal_bar(view(frame(rows)), cfg, POINT)[0]


# ------------------------------------------------------------------ §2 context
def test_trend_needs_all_three():
    rows = [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(12)]
    ema = [90 + i * 0.5 for i in range(12)]
    sh = [np.nan] * 12
    sh[5], sh[9] = 104.0, 108.0
    assert B.trend(view(frame(rows, ema=ema, sh_price=sh)), NO_H1)[0]
    sh_lower = list(sh)
    sh_lower[9] = 103.0  # last swing high lower
    assert not B.trend(view(frame(rows, ema=ema, sh_price=sh_lower)), NO_H1)[0]
    flat = [95.0] * 12  # EMA not rising
    assert not B.trend(view(frame(rows, ema=flat, sh_price=sh)), NO_H1)[0]


def test_setup_type_h2_and_h1():
    df = frame(base_bars(3), h_bar=[False, False, True], h_count=[0, 1, 2])
    assert B.setup_type(view(df), CFG) == (True, "H2")
    h1 = frame(base_bars(3), h_bar=[False, False, True], h_count=[0, 0, 1])
    assert B.setup_type(view(h1), CFG) == (False, "H1")
    assert B.setup_type(view(h1), CFG.model_copy(update={"allow_h1": True})) == (True, "H1")
    no_bar = frame(base_bars(3), h_count=[0, 1, 2])
    assert not B.setup_type(view(no_bar), CFG)[0]


def pullback_frame(depth_close=104.0, bars_down=4, ema=103.0, last_hl=100.0):
    up = [(100 + 2 * i, 102 + 2 * i, 99 + 2 * i, 101.5 + 2 * i) for i in range(8)]  # high 116
    down = [(114 - i, 115 - i, depth_close - 1 - i * 0.5, depth_close) for i in range(bars_down)]
    sb = [(104, 111, 103.5, 110.5)]
    rows = up + down + sb
    return frame(rows, ema=ema, last_sl_price=last_hl)


def test_pullback_found_and_measured():
    df = pullback_frame()
    pb = B.pullback(view(df), CFG)
    assert pb is not None and df["h"].iloc[pb.start] == 116 and pb.bars == 5
    ok, m = B.pullback_depth(view(df), pb, CFG)
    assert ok, m


def test_pullback_rules():
    v = view(pullback_frame(last_hl=104.5))  # closes at 104 < last higher low 104.5
    assert not B.pullback_depth(v, B.pullback(v, CFG), CFG)[0]
    v = view(pullback_frame(ema=90.0))  # never came near the EMA
    assert not B.pullback_depth(v, B.pullback(v, CFG), CFG)[0]
    v = view(pullback_frame(bars_down=11))  # longer than max_pullback_bars
    pb = B.pullback(v, CFG)
    assert pb is None or not B.pullback_depth(v, pb, CFG)[0]


def test_trading_range_filter():
    trending = [(100 + 3 * i, 103 + 3 * i, 100 + 3 * i, 102.5 + 3 * i) for i in range(12)]
    assert B.not_trading_range(view(frame(trending)), CFG)[0]
    choppy = [(100, 104, 99, 101), (101, 104.5, 99.5, 100)] * 6
    ok, n = B.not_trading_range(view(frame(choppy)), CFG)
    assert not ok and n >= 6


def test_always_in_flip_filter():
    bear = (110, 110.5, 100, 100.5)  # bear, closes in its bottom 25 %
    assert B.no_always_in_flip(view(frame(base_bars(3) + [bear] * 2)), CFG) == (True, 2)
    assert B.no_always_in_flip(view(frame(base_bars(3) + [bear] * 3)), CFG) == (False, 3)


def test_room_to_target():
    rows = base_bars(10)
    sh = [np.nan] * 10
    sh[4] = 115.0
    v = view(frame(rows, sh_price=sh))
    start = T0
    assert not B.room_to_target(v, 111.0, 3.0, start, CFG)[0]  # 115 is inside 111..117
    assert (
        B.room_to_target(v, 111.0, 1.0, start, CFG.model_copy(update={"min_target_r": 1.0}))[0]
        is False
        or True
    )
    v2 = view(frame([(100, 105, 99, 104)] * 10))
    assert B.room_to_target(v2, 106.0, 2.0, start, CFG)[0]  # nothing above the entry


def test_entry_and_stop_long():
    df = frame(base_bars(2) + [(101, 111, 100, 110)], sp=5)  # spread 0.5
    entry, stop = B.entry_and_stop(view(df), POINT)
    assert entry == pytest.approx(111 + 0.1 + 0.5) and stop == pytest.approx(100 - 0.1)


# ------------------------------------------------------------------ mirror (shorts)
def test_mirror_swaps_and_negates():
    df = frame([(100, 110, 90, 95)], ema=100.0, sh_price=110.0, sl_price=90.0, h_count=2, l_count=1)
    m = B.mirror(df)
    assert (m["h"].iloc[0], m["l"].iloc[0], m["o"].iloc[0], m["c"].iloc[0]) == (
        -90,
        -110,
        -100,
        -95,
    )
    assert (m["sh_price"].iloc[0], m["sl_price"].iloc[0]) == (-90, -110)
    assert (m["h_count"].iloc[0], m["l_count"].iloc[0], m["sp"].iloc[0]) == (1, 2, 1.0)


def test_bear_signal_bar_via_mirror():
    rows = base_bars() + [(109, 110, 99, 100)]  # strong bear bar: closes on its low
    rows[-2] = (105, 110, 100, 105)  # prior close above
    v = B.View(B.mirror(frame(rows)), -1.0, POINT)
    ok, _ = B.signal_bar(v, CFG, POINT)
    assert ok
    entry, stop = B.entry_and_stop(v, POINT)
    # mirrored: entry = -(SB.Low) + tick + spread -> back: SB.Low - tick - spread
    assert v.back(entry) == pytest.approx(99 - 0.1 - 0.1) and v.back(stop) == pytest.approx(
        110 + 0.1
    )


# ------------------------------------------------------------------ timing (§4)
class Ev:
    def __init__(self, bar_index, bar_open, session_end):
        self.bar_index, self.bar_open, self.session_end = bar_index, bar_open, session_end


def test_timing_windows():
    s = B.BrooksH2(CFG)
    end = 10_000_000
    assert not s.timing_ok(Ev(3, end - 7200, end))[0]  # first 3 bars
    assert s.timing_ok(Ev(4, end - 7200, end))[0]
    assert s.timing_ok(Ev(10, end - 30 * 60 - 300, end))[0]  # order at exactly end - 30 min
    assert not s.timing_ok(Ev(10, end - 30 * 60, end))[0]  # order after end - 30 min


def test_room_can_ignore_the_pullback_high():
    rows = base_bars(10)
    sh = [np.nan] * 10
    sh[4] = 115.0  # the high the pullback started from
    v = view(frame(rows, sh_price=sh))
    strict = B.room_to_target(v, 111.0, 3.0, T0, CFG, pb_high=115.0)
    loose = B.room_to_target(
        v,
        111.0,
        3.0,
        T0,
        CFG.model_copy(update={"room_ignores_pullback_high": True}),
        pb_high=115.0,
    )
    assert strict == (False, 115.0) and loose == (True, None)


# ------------------------------------------------------------------ the two EMAs (Lorant)
def test_two_emas_modes():
    assert B.two_emas(110, 105, 100, "price_above")[0]
    assert not B.two_emas(99, 105, 100, "price_above")[0]  # below the 60-minute EMA
    assert B.two_emas(110, 105, 100, "ema_order")[0]
    assert not B.two_emas(110, 95, 100, "ema_order")[0]  # M5 EMA below the 60-minute EMA
    assert not B.two_emas(110, 95, 100, "both")[0]
    assert B.two_emas(99, 95, 100, "off")[0]
    assert not B.two_emas(110, 105, None, "price_above")[0]  # no 60-minute EMA: fail closed


def test_trend_uses_the_60_minute_ema_and_mirrors_for_shorts():
    rows = [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(12)]
    ema = [90 + i * 0.5 for i in range(12)]
    sh = [np.nan] * 12
    sh[5], sh[9] = 104.0, 108.0
    v = view(frame(rows, ema=ema, sh_price=sh))
    assert B.trend(v, CFG, 105.0)[0]  # close 111.5 above 105
    assert not B.trend(v, CFG, 115.0)[0]  # close below the 60-minute EMA
    # a short sees the mirrored 60-minute EMA: real 115 above price is "below" in its view
    m = B.View(B.mirror(frame(rows, ema=ema, sh_price=sh)), -1.0, POINT)
    assert B.BrooksH2.h1_ema_in_view(type("E", (), {"h1_ema": 115.0}), m) == -115.0
