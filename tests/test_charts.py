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
