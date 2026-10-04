import struct

import numpy as np
import pandas as pd

from app.charts import render_chart


def frame(n=120):
    idx = pd.date_range("2026-10-05 05:00", periods=n, freq="5min", tz="UTC")
    c = 24300 + np.cumsum(np.sin(np.arange(n) / 3) * 4)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(
        {"o": o, "h": np.maximum(o, c) + 2, "l": np.minimum(o, c) - 2, "c": c}, index=idx
    )


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_chart_is_phone_sized_png():
    df = frame()
    setup = {"entry": 24310.0, "stop": 24290.0, "target": 24350.0}
    png = render_chart(df, "GER40.cash", "M5", "Europe/Budapest", setup, df.index[40])
    assert png_size(png) == (1080, 1350)


def test_chart_without_setup_or_session():
    assert png_size(render_chart(frame(30), "GER40.cash", "H1", "Europe/Budapest")) == (1080, 1350)


def test_chart_with_hourly_ema_line():
    from app.features import htf_ema_on_ltf

    df = frame()
    idx = pd.date_range("2026-10-04 00:00", periods=40, freq="1h", tz="UTC")
    h1 = pd.DataFrame({"o": 24300.0, "h": 24310.0, "l": 24290.0, "c": 24300.0}, index=idx)
    line = htf_ema_on_ltf(df.index, h1, 20)
    assert line.notna().any()
    png = render_chart(df, "GER40.cash", "M5", "Europe/Budapest", htf_ema=line)
    assert png_size(png) == (1080, 1350)
    all_nan = pd.Series(float("nan"), index=df.index)
    assert png_size(render_chart(df, "GER40.cash", "M5", "Europe/Budapest", htf_ema=all_nan)) == (
        1080,
        1350,
    )
