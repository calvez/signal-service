from datetime import UTC, datetime

import pytest

from app.timeconv import server_to_utc, utc_offset_sec, validate_mode


def server_epoch(y, m, d, hh, mm=0):
    """Raw server epoch for a wall-clock reading (naive clock read as UTC)."""
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def test_ny_plus_7_summer_and_winter():
    assert utc_offset_sec(server_epoch(2026, 7, 1, 12), "ny_plus_7") == 3 * 3600  # EDT
    assert utc_offset_sec(server_epoch(2026, 1, 15, 12), "ny_plus_7") == 2 * 3600  # EST


def test_ny_plus_7_gap_between_us_and_eu_dst_2026():
    # EU DST ends Sun 25 Oct, US DST ends Sun 1 Nov 2026. In between, Berlin is UTC+1 but a
    # ny_plus_7 server is still UTC+3.
    assert utc_offset_sec(server_epoch(2026, 10, 28, 12), "ny_plus_7") == 3 * 3600
    assert utc_offset_sec(server_epoch(2026, 11, 3, 12), "ny_plus_7") == 2 * 3600


def test_iana_mode_follows_that_zone():
    assert utc_offset_sec(server_epoch(2026, 10, 28, 12), "iana:Europe/Prague") == 3600  # CET
    assert utc_offset_sec(server_epoch(2026, 10, 20, 12), "iana:Europe/Prague") == 7200  # CEST


def test_fixed_mode():
    assert server_to_utc(1_000_000, "fixed:10800") == 1_000_000 - 10800


@pytest.mark.parametrize("bad", ["", "utc", "iana:Mars/Base", "fixed:abc", "ny_plus_8"])
def test_invalid_modes_rejected(bad):
    with pytest.raises(ValueError):
        validate_mode(bad)
