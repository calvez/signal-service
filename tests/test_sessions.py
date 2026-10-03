from datetime import UTC, date, datetime

import pytest
import yaml

from app import sessions as S
from app.config import AppConfig


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return AppConfig.model_validate(yaml.safe_load(open("config.example.yaml")))


def utc(y, m, d, hh, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def test_eu_and_us_windows_in_summer(cfg):
    assert S.session_window_utc(cfg, "eu", date(2026, 10, 5)) == (
        utc(2026, 10, 5, 7),
        utc(2026, 10, 5, 9),
    )
    assert S.session_window_utc(cfg, "us", date(2026, 10, 5)) == (
        utc(2026, 10, 5, 13, 30),
        utc(2026, 10, 5, 15, 30),
    )


def test_oct_gap_europe_winter_us_still_summer(cfg):
    # 28 Oct 2026: Berlin is CET (UTC+1), New York is still EDT (UTC-4)
    assert S.session_window_utc(cfg, "eu", date(2026, 10, 28)) == (
        utc(2026, 10, 28, 8),
        utc(2026, 10, 28, 10),
    )
    assert S.session_window_utc(cfg, "us", date(2026, 10, 28)) == (
        utc(2026, 10, 28, 13, 30),
        utc(2026, 10, 28, 15, 30),
    )
    # after 1 Nov both are on winter time
    assert S.session_window_utc(cfg, "us", date(2026, 11, 3))[0] == utc(2026, 11, 3, 14, 30)


def test_mar_gap_us_summer_europe_still_winter(cfg):
    # 16 Mar 2026: US DST began 8 Mar, Europe's begins 29 Mar
    assert S.session_window_utc(cfg, "eu", date(2026, 3, 16))[0] == utc(2026, 3, 16, 8)
    assert S.session_window_utc(cfg, "us", date(2026, 3, 16))[0] == utc(2026, 3, 16, 13, 30)


def test_holidays_and_weekends(cfg):
    assert S.session_window_utc(cfg, "eu", date(2026, 5, 1)) is None  # Labour Day, XETR closed
    assert S.session_window_utc(cfg, "us", date(2026, 5, 1)) is not None  # NYSE open
    assert S.session_window_utc(cfg, "us", date(2026, 11, 26)) is None  # Thanksgiving
    assert S.session_window_utc(cfg, "eu", date(2026, 11, 26)) is not None
    assert S.session_window_utc(cfg, "eu", date(2026, 12, 25)) is None
    assert S.session_window_utc(cfg, "us", date(2026, 12, 25)) is None
    assert S.session_window_utc(cfg, "eu", date(2026, 10, 3)) is None  # Saturday


def test_far_future_fails_closed(cfg):
    assert S.session_window_utc(cfg, "eu", date(2080, 1, 7)) is None


def test_active_session_and_bar_index(cfg):
    assert S.active_session(cfg, utc(2026, 10, 5, 6, 59)) is None
    name, start, end = S.active_session(cfg, utc(2026, 10, 5, 7, 0))
    assert (name, start, end) == ("eu", utc(2026, 10, 5, 7), utc(2026, 10, 5, 9))
    assert S.active_session(cfg, utc(2026, 10, 5, 8, 55))[0] == "eu"  # last bar, closes at 09:00
    assert S.active_session(cfg, utc(2026, 10, 5, 9, 0)) is None
    assert S.active_session(cfg, utc(2026, 5, 1, 7, 0)) is None  # EU holiday
    assert S.bar_index_in_session(cfg, "eu", utc(2026, 10, 5, 7, 0)) == 1
    assert S.bar_index_in_session(cfg, "eu", utc(2026, 10, 5, 7, 30)) == 7
    assert S.bar_index_in_session(cfg, "eu", utc(2026, 10, 5, 9, 0)) is None


def test_previous_trading_day_skips_weekend_and_holiday(cfg):
    assert S.previous_trading_day(cfg, "eu", date(2026, 10, 5)) == date(
        2026, 10, 2
    )  # Monday -> Friday
    assert S.previous_trading_day(cfg, "eu", date(2026, 12, 28)) == date(
        2026, 12, 23
    )  # 24-26 closed


def test_cash_close(cfg):
    assert S.cash_close_utc(cfg, "eu", date(2026, 10, 5)) == utc(2026, 10, 5, 15, 30)


def test_next_session_start_from_friday_evening(cfg):
    assert S.next_session_start(cfg, utc(2026, 10, 2, 20)) == ("eu", utc(2026, 10, 5, 7))
    assert S.next_session_start(cfg, utc(2026, 10, 5, 8)) == ("us", utc(2026, 10, 5, 13, 30))


def test_news_flag_window_is_plus_minus_two_minutes(cfg):
    # example event: 2026-10-06 08:30 New York = 12:30 UTC
    f = lambda hh, mm: S.news_flag(cfg, utc(2026, 10, 6, hh, mm))  # noqa: E731
    assert f(12, 20) is None
    assert f(12, 25) is not None  # bar 12:25-12:30 reaches the event
    assert f(12, 30) is not None
    assert f(12, 35) is None  # starts 5 min after the event
