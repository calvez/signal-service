"""Session windows, holidays, DST and news flags.

Windows are defined in exchange time (config `sessions`) and converted with zoneinfo, so the
Europe/US DST dates are handled by the tz database, never by fixed offsets. A session does not
exist on exchange holidays or weekends (exchange_calendars: XETR for EU, XNYS for US).

All function inputs/outputs are UTC epoch seconds unless a name says `local`.
"""

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from app.config import AppConfig

M5_SEC = 300


@lru_cache
def _calendar(name: str):
    return xcals.get_calendar(name)


def calendar_name(cfg: AppConfig, session: str) -> str:
    """Holiday calendar of a session = the calendar of its first *traded* symbol."""
    for sym in cfg.symbols.values():
        if sym.session == session and sym.role == "traded":
            return sym.calendar
    raise ValueError(f"session {session!r} has no traded symbol in config")


def is_trading_day(cfg: AppConfig, session: str, local_date: date) -> bool:
    """False on weekends and exchange holidays. Fails closed (False) outside the calendar."""
    try:
        return bool(_calendar(calendar_name(cfg, session)).is_session(pd.Timestamp(local_date)))
    except xcals.errors.DateOutOfBounds:
        return False


def _local_to_utc(local_date: date, t: time, tz: str) -> int:
    return int(datetime.combine(local_date, t, tzinfo=ZoneInfo(tz)).timestamp())


def session_window_utc(cfg: AppConfig, session: str, local_date: date) -> tuple[int, int] | None:
    """(start, end) of the session on that local date, or None if there is no session."""
    if not is_trading_day(cfg, session, local_date):
        return None
    s = cfg.sessions[session]
    return _local_to_utc(local_date, s.start, s.tz), _local_to_utc(local_date, s.end, s.tz)


def cash_close_utc(cfg: AppConfig, session: str, local_date: date) -> int:
    s = cfg.sessions[session]
    return _local_to_utc(local_date, s.cash_close, s.tz)


def local_date(cfg: AppConfig, session: str, t_utc: int) -> date:
    return datetime.fromtimestamp(t_utc, tz=ZoneInfo(cfg.sessions[session].tz)).date()


def previous_trading_day(cfg: AppConfig, session: str, d: date) -> date:
    d = d - timedelta(days=1)
    while not is_trading_day(cfg, session, d):
        d -= timedelta(days=1)
    return d


def active_session(cfg: AppConfig, t_utc: int) -> tuple[str, int, int] | None:
    """(session name, start, end) if `t_utc` is inside a session window, else None.
    A bar belongs to a session when its OPEN time is inside the window."""
    for name in cfg.sessions:
        win = session_window_utc(cfg, name, local_date(cfg, name, t_utc))
        if win and win[0] <= t_utc < win[1]:
            return name, win[0], win[1]
    return None


def bar_index_in_session(cfg: AppConfig, session: str, bar_open_utc: int) -> int | None:
    """1-based index of the M5 bar within its session (1 = the opening bar), or None."""
    win = session_window_utc(cfg, session, local_date(cfg, session, bar_open_utc))
    if not win or not (win[0] <= bar_open_utc < win[1]):
        return None
    return (bar_open_utc - win[0]) // M5_SEC + 1


def next_session_start(cfg: AppConfig, t_utc: int) -> tuple[str, int] | None:
    """The next session start strictly after `t_utc` (looks up to 14 days ahead)."""
    best: tuple[str, int] | None = None
    for name in cfg.sessions:
        d0 = local_date(cfg, name, t_utc)
        for k in range(15):
            win = session_window_utc(cfg, name, d0 + timedelta(days=k))
            if win and win[0] > t_utc:
                if best is None or win[0] < best[1]:
                    best = (name, win[0])
                break
    return best


def news_flag(cfg: AppConfig, bar_open_utc: int) -> str | None:
    """Name of a configured high-impact event near this M5 bar, else None.
    The bar [open, open+5min) is flagged if it overlaps [event - window, event + window]."""
    w = cfg.news_window_min * 60
    for ev in cfg.news:
        ev_utc = int(ev.at.replace(tzinfo=ZoneInfo(ev.tz)).astimezone(UTC).timestamp())
        if bar_open_utc < ev_utc + w and bar_open_utc + M5_SEC > ev_utc - w:
            return ev.name
    return None
