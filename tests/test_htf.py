import numpy as np
import pandas as pd

from app import db
from app.htf import D1_SEC, H1_SEC, alignment, htf_state

START = pd.Timestamp("2026-09-01", tz="UTC")


def zigzag(n, drift, amp=3.0, period=6, freq="1h"):
    """Closed bars on a drifting zigzag: drift > 0 up, < 0 down, 0 sideways."""
    i = np.arange(n)
    c = 1000 + drift * i + amp * np.sin(2 * np.pi * i / period)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(START, periods=n, freq=freq)
    return pd.DataFrame(
        {"o": o, "h": np.maximum(o, c) + 0.5, "l": np.minimum(o, c) - 0.5, "c": c}, index=idx
    )


def end(df, sec):
    return df.index[-1] + pd.Timedelta(seconds=sec)


def test_uptrend_downtrend_range():
    up, down, flat = zigzag(80, 1.0), zigzag(80, -1.0), zigzag(80, 0.0)
    assert htf_state(up, H1_SEC, end(up, H1_SEC)).state == "bull"
    assert htf_state(down, H1_SEC, end(down, H1_SEC)).state == "bear"
    assert htf_state(flat, H1_SEC, end(flat, H1_SEC)).state == "neutral"


def test_too_little_history_is_neutral():
    short = zigzag(10, 1.0)
    assert htf_state(short, H1_SEC, end(short, H1_SEC)).state == "neutral"


def test_still_forming_bar_is_not_used():
    up = zigzag(80, 1.0)
    # asof = open of the last bar: that bar has not closed, so it must be ignored
    s = htf_state(up, H1_SEC, up.index[-1])
    assert s == htf_state(up.iloc[:-1], H1_SEC, up.index[-1])


def test_htf_has_no_lookahead():
    up = zigzag(80, 1.0)
    t = up.index[50] + pd.Timedelta(seconds=H1_SEC)  # bar 50 has just closed
    before = htf_state(up, H1_SEC, t)
    wrecked = up.copy()
    wrecked.iloc[51:, :] = 5000.0  # rewrite the future
    assert htf_state(wrecked, H1_SEC, t) == before


def test_d1_uses_day_length():
    up = zigzag(80, 5.0, freq="1D")
    assert htf_state(up, D1_SEC, end(up, D1_SEC)).state == "bull"
    # with asof one hour into the last day, that D1 bar is not closed yet
    asof = up.index[-1] + pd.Timedelta(hours=1)
    assert htf_state(up, D1_SEC, asof) == htf_state(up.iloc[:-1], D1_SEC, asof)


def test_alignment_table():
    assert alignment("bull", "bull") == "aligned_bull"
    assert alignment("bear", "bear") == "aligned_bear"
    assert alignment("bull", "bear") == "conflict"
    assert alignment("bear", "bull") == "conflict"
    assert alignment("bull", "neutral") == "conflict"
    assert alignment("neutral", "bear") == "conflict"
    assert alignment("neutral", "neutral") == "conflict"
    # configurable: neutral no longer blocks, but both neutral still does
    assert alignment("bull", "neutral", neutral_is_conflict=False) == "aligned_bull"
    assert alignment("neutral", "bear", neutral_is_conflict=False) == "aligned_bear"
    assert alignment("neutral", "neutral", neutral_is_conflict=False) == "conflict"
    assert alignment("bull", "bear", neutral_is_conflict=False) == "conflict"


def test_load_bars_roundtrip(tmp_path):
    path = str(tmp_path / "x.db")
    db.init_db(path)
    conn = db.connect(path)
    rows = [
        {"symbol": "S", "tf": "M5", "t_server": 0, "t_utc": 1_000_000 + 300 * i, "o": 1, "h": 2,
         "l": 0.5, "c": 1.5, "tv": 1, "sp": 1, "received_at": 0}
        for i in range(10)
    ]  # fmt: skip
    db.upsert_bars(conn, rows)
    df = db.load_bars(conn, "S", "M5", limit=4)
    assert len(df) == 4 and df.index.is_monotonic_increasing and str(df.index.tz) == "UTC"
    assert df.index[-1] == pd.Timestamp(1_000_000 + 300 * 9, unit="s", tz="UTC")
    assert len(db.load_bars(conn, "S", "M5", until_utc=1_000_000 + 600)) == 3
    assert db.load_bars(conn, "NOPE", "M5").empty
