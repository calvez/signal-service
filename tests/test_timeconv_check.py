from datetime import UTC, datetime

from app.timeconv import offset_matches


def test_offset_matches_detects_wrong_rule():
    t = int(datetime(2026, 10, 28, 12, tzinfo=UTC).timestamp())  # EU/US DST gap
    assert offset_matches(3 * 3600, t, "ny_plus_7")
    assert not offset_matches(3 * 3600, t, "iana:Europe/Prague")  # Prague is UTC+1 here
