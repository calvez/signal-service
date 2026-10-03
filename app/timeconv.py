"""MT5 server time -> UTC.

MT5 sends bar times as raw epochs from the broker's *server clock* (a naive clock reading
interpreted as if it were UTC). Which clock that is depends on the broker, so the rule is
configured with SERVER_TIME_MODE (decided in task T2 by comparing heartbeat offsets):

  ny_plus_7        server clock = New York local time + 7 h (DST follows New York)
  iana:<Zone>      server clock = local time of that zone, e.g. iana:Europe/Prague
  fixed:<seconds>  server clock = UTC + a constant number of seconds

All results are UTC epoch seconds. Times inside a DST gap or repeated hour resolve with
fold=0 (the first occurrence); the markets we watch are closed at those moments.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NEW_YORK = ZoneInfo("America/New_York")


def validate_mode(mode: str) -> str:
    """Raise ValueError if `mode` is not a supported SERVER_TIME_MODE. Returns it unchanged."""
    if mode == "ny_plus_7":
        return mode
    if mode.startswith("iana:"):
        try:
            ZoneInfo(mode[5:])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone in server_time_mode: {mode!r}") from exc
        return mode
    if mode.startswith("fixed:"):
        try:
            int(mode[6:])
        except ValueError as exc:
            raise ValueError(f"fixed server_time_mode needs integer seconds: {mode!r}") from exc
        return mode
    raise ValueError(f"unknown server_time_mode: {mode!r} (ny_plus_7 | iana:<Zone> | fixed:<s>)")


def _naive_clock(t_server: int) -> datetime:
    """The raw server epoch read as a naive wall-clock datetime."""
    return datetime.fromtimestamp(t_server, tz=UTC).replace(tzinfo=None)


def server_to_utc(t_server: int, mode: str) -> int:
    """Convert a raw MT5 server epoch to a real UTC epoch under the given mode."""
    validate_mode(mode)
    if mode == "ny_plus_7":
        local = _naive_clock(t_server) - timedelta(hours=7)
        return int(local.replace(tzinfo=NEW_YORK).timestamp())
    if mode.startswith("iana:"):
        return int(_naive_clock(t_server).replace(tzinfo=ZoneInfo(mode[5:])).timestamp())
    return t_server - int(mode[6:])


def utc_offset_sec(t_server: int, mode: str) -> int:
    """The server's UTC offset (server clock minus UTC) at that moment under the given mode.
    Compare with the `server_utc_offset_sec` the EA reports."""
    return t_server - server_to_utc(t_server, mode)


def offset_matches(reported_offset_sec: int, t_server: int, mode: str) -> bool:
    """Monitor check (every heartbeat): does the configured rule give the offset the EA reports?"""
    return utc_offset_sec(t_server, mode) == reported_offset_sec
