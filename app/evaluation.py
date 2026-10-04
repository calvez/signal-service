"""Python evaluation of one closed M5 bar. Used by BOTH the live reader and the backtester,
so a backtest measures exactly the code that runs live.

`evaluate_bar` takes plain bar frames (from the live database or from history) and returns
everything the code knows about the bar: features, day context, H1/D1 state, the 60-minute EMA,
and the candidate setups of the configured strategy. It raises `Skip` when the bar must not be
evaluated (outside the session, H1/D1 conflict, not enough history, ...).

No lookahead: only bars that had closed by the end of the evaluated bar are used.
"""

from dataclasses import dataclass, field

import pandas as pd

from app import features, htf, sessions
from app.config import AppConfig
from app.validate import Expected

M5_SEC = 300
M5_WINDOW = 600  # M5 bars per evaluation (live and backtest use the same window)
H1_WINDOW = 300
D1_WINDOW = 150
MIN_HISTORY_BARS = 60  # EMA/ATR/swings need warm-up


def _htf(bars: pd.DataFrame, tf_sec: int, closes: pd.Timestamp, fc, cache: dict | None):
    if cache is None:
        return htf.htf_state(bars, tf_sec, closes, fc.ema_period, fc.swing_confirm_bars)
    closed = bars[bars.index + pd.Timedelta(seconds=tf_sec) <= closes]
    key = (tf_sec, closed.index[-1] if len(closed) else None, len(closed))
    if key not in cache:
        cache[key] = htf.htf_state(closed, tf_sec, closes, fc.ema_period, fc.swing_confirm_bars)
    return cache[key]


class Skip(Exception):
    """This bar is not evaluated; the message is the reason (logged, never alerted)."""


@dataclass
class Evaluation:
    symbol: str
    session: str
    session_tz: str
    session_start: int
    session_end: int
    bar_open: int  # UTC epoch, open time of the evaluated (closed) M5 bar
    bar_index: int  # 1 = first bar of the session
    digits: int
    feats: pd.DataFrame  # all feature columns, last row = the evaluated bar
    atr: float
    last_close: float
    h1: htf.TrendState
    d1: htf.TrendState
    alignment: str
    ctx: dict
    hint: str
    h1_ema: float | None  # 60-minute EMA20 as known at this bar's close
    news: str | None
    candidates: list = field(default_factory=list)  # filled by a strategy (app/strategies)

    @property
    def last(self) -> pd.Series:
        return self.feats.iloc[-1]

    @property
    def bar_iso(self) -> str:
        return pd.Timestamp(self.bar_open, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")

    def expected(self) -> Expected:
        """What the validator compares a setup with."""
        return Expected(
            self.symbol, self.bar_iso, self.alignment, self.atr, self.last_close, self.hint,
            self.ctx["pct_in_range"], self.digits,
        )  # fmt: skip


def evaluate_bar(
    cfg: AppConfig,
    symbol: str,
    bar_open: int,
    m5: pd.DataFrame,
    h1_bars: pd.DataFrame,
    d1_bars: pd.DataFrame,
    digits: int,
    htf_cache: dict | None = None,
) -> Evaluation:
    """Evaluate the closed M5 bar opening at `bar_open` (UTC). Bars after it are ignored.

    `htf_cache`: optional dict the backtester passes in. The H1/D1 state can only change when
    a higher-timeframe bar closes, so it is cached per (timeframe, newest closed bar). Same
    result as without the cache; only faster.
    """
    sym = cfg.symbols.get(symbol)
    if sym is None or sym.role != "traded":
        raise Skip("not_traded")
    active = sessions.active_session(cfg, bar_open)
    if active is None or active[0] != sym.session:
        raise Skip("outside_session")
    session, session_start, session_end = active

    bar_ts = pd.Timestamp(bar_open, unit="s", tz="UTC")
    m5 = m5[m5.index <= bar_ts].tail(M5_WINDOW)
    if m5.empty or m5.index[-1] != bar_ts:
        raise Skip("bar_not_stored")
    if len(m5) < MIN_HISTORY_BARS:
        raise Skip("not_enough_history")

    # ---- his H1/D1 rule, deterministic. Only higher-timeframe bars closed by this bar's close.
    closes = bar_ts + pd.Timedelta(seconds=M5_SEC)
    fc = cfg.features
    h1_win = h1_bars[h1_bars.index <= closes].tail(H1_WINDOW)
    d1_win = d1_bars[d1_bars.index <= closes].tail(D1_WINDOW)
    h1 = _htf(h1_win, htf.H1_SEC, closes, fc, htf_cache)
    d1 = _htf(d1_win, htf.D1_SEC, closes, fc, htf_cache)
    alignment = htf.alignment(h1.state, d1.state, cfg.rules.htf_neutral_counts_as_conflict)
    if alignment == "conflict" and cfg.rules.require_htf_alignment:
        raise Skip(f"htf_conflict (H1 {h1.state}, D1 {d1.state})")

    # ---- features on the M5 window
    stz = cfg.sessions[session].tz
    day = pd.Series(m5.index.tz_convert(stz).date, index=m5.index)
    feats = features.compute_features(m5, fc.ema_period, fc.atr_period, fc.swing_confirm_bars, day)
    atr_now = float(feats["atr"].iloc[-1])
    if pd.isna(atr_now):
        raise Skip("atr_unavailable")

    local_day = sessions.local_date(cfg, session, bar_open)
    prev_day = sessions.previous_trading_day(cfg, session, local_day)
    cutoff = pd.Timestamp(sessions.cash_close_utc(cfg, session, prev_day), unit="s", tz="UTC")
    ctx = features.day_context(
        feats,
        pd.Timestamp(session_start, unit="s", tz="UTC"),
        fc.opening_range_bars,
        features.prior_close(m5, cutoff),
    )
    if ctx is None:
        raise Skip("no_day_context")
    hint = features.day_type_hint(feats, ctx, atr_now)

    h1_line = features.htf_ema_on_ltf(m5.index[-1:], h1_win, fc.ema_period)
    h1_ema = None if pd.isna(h1_line.iloc[0]) else float(h1_line.iloc[0])

    return Evaluation(
        symbol=symbol, session=session, session_tz=stz, session_start=session_start,
        session_end=session_end,
        bar_open=bar_open, bar_index=sessions.bar_index_in_session(cfg, session, bar_open),
        digits=digits, feats=feats, atr=atr_now, last_close=float(feats["c"].iloc[-1]),
        h1=h1, d1=d1, alignment=alignment, ctx=ctx, hint=hint, h1_ema=h1_ema,
        news=sessions.news_flag(cfg, bar_open),
    )  # fmt: skip
