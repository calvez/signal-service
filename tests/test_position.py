import pandas as pd
import pytest

from app.position import PyramidRules, simulate_position

S = 1_790_000_000  # signal bar open (multiple of 300)
FAR = S + 86400
LONG = {"direction": "long", "entry": 100.0, "stop": 90.0}  # 1R = 10 points
SHORT = {"direction": "short", "entry": 100.0, "stop": 110.0}
TICK = 0.1


def bars(rows, first=S + 300, step=300):
    idx = pd.to_datetime([first + step * i for i in range(len(rows))], unit="s", utc=True)
    return pd.DataFrame({"o": [r[2] for r in rows], "h": [r[0] for r in rows],
                         "l": [r[1] for r in rows], "c": [r[2] for r in rows]}, index=idx)  # fmt: skip


def run(rows, setup=LONG, rules=None, horizon=FAR, levels=None, now=FAR + 4000):
    return simulate_position(setup, S, bars(rows), horizon, rules or PyramidRules(trail="none"),
                             TICK, levels, now)  # fmt: skip


def test_plain_loss_is_minus_one_r():
    r = run([(101, 95, 100), (100, 89, 91)])
    assert (r.status, r.r, r.units) == ("stopped", -1.0, 1)


def test_runner_adds_and_breakeven_stop():
    # +1R (110): add 1, stop -> 105 (whole position at breakeven); +2R (120): add 2, stop -> 110
    rows = [(101, 99, 100), (111, 105.5, 110), (121, 112, 120), (122, 110, 111)]
    r = run(rows)
    assert r.units == 3 and r.status == "stopped" and r.exit_price == pytest.approx(110.0)
    assert r.r == pytest.approx(0.0)  # +1R, 0, -1R on the three units
    kinds = [e[1] for e in r.events]
    assert kinds == ["entry", "add1", "stop", "add2", "stop", "exit"]


def test_runner_held_to_the_close_pays_off():
    rows = [(101, 99, 100), (111, 105.5, 110), (121, 111, 120), (141, 125, 140)]
    r = run(rows, horizon=S + 300 * 5)  # flat after the 4th bar
    assert r.status == "closed_eod" and r.units == 3
    assert r.r == pytest.approx((40 + 30 + 20) / 10)  # three units from 100/110/120 to 140


def test_one_r_risk_variant():
    rules = PyramidRules(trail="none", max_open_risk_r=1.0)
    rows = [(101, 99, 100), (111, 104, 110), (121, 107, 120), (122, 106, 107)]
    r = run(rows, rules=rules)  # stop after add 2: (330 - 10) / 3 = 106.67
    assert r.status == "stopped" and r.r == pytest.approx(-1.0, abs=0.01)


def test_max_adds_is_respected():
    rows = [(101, 99, 100)] + [
        (100 + 10 * k + 1, 100 + 10 * k - 4, 100 + 10 * k) for k in range(1, 6)
    ]
    r = run(rows, rules=PyramidRules(trail="none", max_adds=2), horizon=S + 300 * 7)
    assert r.units == 3


def test_stop_only_moves_in_favour():
    rows = [(101, 99, 100), (111, 105.5, 110), (112, 106, 108), (113, 106, 109)]
    r = run(rows, horizon=S + 300 * 5)
    stops = [p for _, k, p in r.events if k in ("stop", "trail")]
    assert stops == sorted(stops)


def test_add_and_its_new_stop_in_the_same_bar_is_stopped_out():
    rows = [(101, 99, 100), (111, 104, 108)]  # reaches 110 (add) and 104 (< new stop 105)
    r = run(rows)
    assert r.status == "stopped" and r.units == 2 and r.r == pytest.approx(0.0)


def test_entry_bar_that_also_hits_the_stop_is_a_loss():
    r = run([(101, 89, 95)])
    assert (r.status, r.r) == ("stopped", -1.0)


def test_no_entry_and_pending():
    assert run([(99, 95, 97)] * 4).status == "no_entry"
    assert run([(99, 95, 97)] * 2, now=S + 900).status == "pending"
    still_open = run([(101, 99, 100), (105, 99, 104)], now=S + 1000)
    assert still_open.status == "pending" and still_open.r is None


def test_trailing_behind_confirmed_swings():
    # swing low 103 becomes known at S+1200 (after the 3rd bar): stop -> 102.9
    levels = pd.Series([95.0, 103.0], index=pd.to_datetime([S + 600, S + 1200], unit="s", utc=True))
    rows = [(101, 99, 100), (106, 100, 105), (107, 104, 106), (107, 102.5, 103)]
    r = run(rows, rules=PyramidRules(trail="swing"), levels=levels)
    assert r.status == "stopped" and r.exit_price == pytest.approx(102.9)
    assert "trail" in [k for _, k, _ in r.events]


def test_trailing_ignores_levels_not_known_yet():
    levels = pd.Series([103.0], index=pd.to_datetime([S + 10_000], unit="s", utc=True))
    rows = [(101, 99, 100), (106, 102.5, 105)]
    r = run(rows, rules=PyramidRules(trail="swing"), levels=levels, horizon=S + 900)
    assert r.status == "closed_eod"


def test_short_mirror():
    rows = [(101, 99, 100), (94.5, 89, 90), (88, 79, 80), (91, 78, 89)]
    r = run(rows, setup=SHORT)  # adds at 90 and 80; stop 95 then 90; exits at 90
    assert r.units == 3 and r.status == "stopped" and r.exit_price == pytest.approx(90.0)
    assert r.r == pytest.approx(0.0)


def test_m1_bars():
    rows = [(101, 99, 100)] + [(100.5, 99.5, 100)] * 4 + [(111, 105.5, 110)] + [(121, 111, 120)]
    r = simulate_position(LONG, S, bars(rows, step=60), S + 300 + 60 * 8,
                          PyramidRules(trail="none"), TICK, None, FAR, bar_seconds=60)  # fmt: skip
    assert r.units == 3 and r.status == "closed_eod"
