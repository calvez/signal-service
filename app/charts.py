"""PNG charts for Telegram (docs/telegram.md): dark, large labels, 1080x1350 for a phone.

Shows the last N bars, the EMA20 (solid blue), on M5 also the 60-minute EMA20 (dashed, see
features.htf_ema_on_ltf), the opening range, today's high/low (since the session start) and,
for a setup, the entry/stop/target lines.
"""

import io

import matplotlib

matplotlib.use("Agg")  # no display on the server

import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from app.features import ema  # noqa: E402

BG, FG, GRID = "#0e1117", "#e6e6e6", "#2a2f3a"
UP, DOWN = "#26a69a", "#ef5350"
EMA_COLOUR = "#42a5f5"  # solid blue: EMA20 of the chart's own timeframe
HTF_EMA_COLOUR = "#e0e0e0"  # dashed: EMA20 of the 60-minute chart, drawn on the 5-minute chart
STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(up=UP, down=DOWN, edge="inherit", wick="inherit"),
    facecolor=BG,
    figcolor=BG,
    gridcolor=GRID,
    rc={"font.size": 18, "axes.labelcolor": FG, "xtick.color": FG, "ytick.color": FG},
)


def render_chart(
    df: pd.DataFrame,
    symbol: str,
    tf_label: str,
    tz: str,
    setup: dict | None = None,
    session_start: pd.Timestamp | None = None,
    opening_range_bars: int = 6,
    bars: int = 60,
    width: int = 1080,
    height: int = 1350,
    ema_period: int = 20,
    digits: int = 1,
    htf_ema: pd.Series | None = None,
    htf_label: str = "H1",
) -> bytes:
    """PNG bytes. `df`: closed bars (o h l c, UTC index) with enough history for the EMA.
    `htf_ema`: optional higher-timeframe EMA aligned to df's index (dashed line)."""
    if df.empty:
        raise ValueError("no bars to draw")
    full = df.copy()
    full["ema"] = ema(full["c"], ema_period)
    if htf_ema is not None:
        full["htf_ema"] = htf_ema.reindex(full.index)
    shown = full.tail(bars).copy()
    shown.index = shown.index.tz_convert(tz).tz_localize(None)
    shown = shown.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close"})

    lines: list[tuple[float, str, str, str]] = []  # price, label, colour, linestyle
    if session_start is not None:
        today = full[full.index >= session_start]
        if not today.empty:
            first = today.iloc[:opening_range_bars]
            lines += [
                (float(first["h"].max()), "OR high", "#b39ddb", "--"),
                (float(first["l"].min()), "OR low", "#b39ddb", "--"),
                (float(today["h"].max()), "Day high", "#90a4ae", ":"),
                (float(today["l"].min()), "Day low", "#90a4ae", ":"),
            ]
    if setup:
        lines += [
            (setup["entry"], "Entry", "#ffd54f", "-"),
            (setup["stop"], "Stop", DOWN, "-"),
            (setup["target"], "Target", UP, "-"),
        ]

    plots = [mpf.make_addplot(shown["ema"], color=EMA_COLOUR, width=2.5)]
    has_htf = "htf_ema" in shown and shown["htf_ema"].notna().any()
    if has_htf:
        plots.append(
            mpf.make_addplot(shown["htf_ema"], color=HTF_EMA_COLOUR, width=2, linestyle="dashed")
        )

    kwargs: dict = {}
    if lines:
        kwargs["hlines"] = dict(
            hlines=[p for p, *_ in lines],
            colors=[c for _, _, c, _ in lines],
            linestyle=[ls for *_, ls in lines],
            linewidths=2,
        )
    fig, axes = mpf.plot(
        shown,
        type="candle",
        style=STYLE,
        addplot=plots,
        figsize=(width / 100, height / 100),
        returnfig=True,
        warn_too_much_data=10_000,
        datetime_format="%H:%M" if tf_label != "D1" else "%d %b",
        tight_layout=False,
        ylabel="",
        **kwargs,
    )
    ax = axes[0]
    ax.set_title(f"{symbol}  {tf_label}", color=FG, fontsize=30, loc="left", pad=20)
    legend = [(f"── EMA{ema_period} {tf_label}", EMA_COLOUR)]
    if has_htf:
        legend.append((f"- - EMA{ema_period} {htf_label}", HTF_EMA_COLOUR))
    for i, (text, colour) in enumerate(legend):
        ax.text(
            0.01, 0.98 - 0.05 * i, text, transform=ax.transAxes, color=colour,
            fontsize=18, va="top", ha="left", fontweight="bold",
        )  # fmt: skip
    for price, label, colour, _ in lines:
        ax.text(
            0.99, price, f"{label} {price:.{digits}f} ",
            transform=ax.get_yaxis_transform(), color=colour, fontsize=18,
            va="bottom", ha="right", fontweight="bold",
        )  # fmt: skip
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=BG)  # no bbox trimming: exact size
    matplotlib.pyplot.close(fig)
    return buf.getvalue()
