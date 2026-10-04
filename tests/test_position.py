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


def run(rows, setup=LONG, rules=None, horizon=FAR, levels=None, now=FAR + 4000, reversals=None):
    return simulate_position(setup, S, bars(rows), horizon, rules or PyramidRules(trail="none"),
                             TICK, levels, now, reversal_exits=reversals)  # fmt: skip


def rev(closes_at_and_price):
    """Reversal-bar series: {close time: close price}."""
    times = sorted(closes_at_and_price)
    return pd.Series([closes_at_and_price[t] for t in times],
                     index=pd.to_datetime(times, unit="s", utc=True), dtype=float)  # fmt: skip


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
    r = run(rows, rules=PyramidRules(trail="none", max_adds=2), horizon=S + 300 * 5)
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


def test_defaults_are_lorants_numbers():
    r = PyramidRules()
    assert (r.risk_pct, r.max_adds, r.add_size, r.exit_on_reversal) == (0.3, None, 1.0, True)


def test_no_limit_on_adds():
    rows = [(101, 99, 100)] + [
        (100 + 10 * k + 1, 100 + 10 * k - 4, 100 + 10 * k) for k in range(1, 7)
    ]
    r = run(rows, rules=PyramidRules(trail="none"), horizon=S + 300 * 8)
    assert r.units == 7  # entry + an add at every +1R


def test_exit_on_a_reversal_bar_at_its_close():
    # entry in bar 1; bar 3 (closes at S+1200) is a reversal bar closing at 112
    rows = [(101, 99, 100), (111, 105.5, 110), (115, 111, 112), (130, 112, 128)]
    reversals = rev({S + 900: float("nan"), S + 1200: 112.0})
    r = run(rows, reversals=reversals)
    assert (r.status, r.exit_price, r.exit_t) == ("reversal", 112.0, S + 1200)
    assert r.r == pytest.approx((12 + 2) / 10)  # units from 100 and 110 closed at 112


def test_reversal_bars_before_the_entry_do_not_count():
    rows = [(99, 95, 97), (101, 99, 100), (105, 100, 104)]
    reversals = rev({S + 600: 97.0})  # the bar before the entry
    r = run(rows, reversals=reversals, horizon=S + 300 * 4)
    assert r.status == "closed_eod"


def test_reversal_exit_can_be_switched_off():
    rows = [(101, 99, 100), (105, 100, 104), (106, 101, 105)]
    r = run(rows, rules=PyramidRules(trail="none", exit_on_reversal=False),
            reversals=rev({S + 900: 104.0}), horizon=S + 300 * 4)  # fmt: skip
    assert r.status == "closed_eod"


def test_short_reversal_exit():
    rows = [(101, 99, 100), (94.5, 89, 90), (93, 88, 92), (92.5, 90, 91)]
    r = run(rows, setup=SHORT, reversals=rev({S + 1200: 92.0}), horizon=S + 300 * 6)
    assert r.status == "reversal" and r.r == pytest.approx((8 + -2) / 10)


def test_leverage_cap():
    from app.position import capped_rules, max_units_by_leverage

    assert max_units_by_leverage(0.3, 27, 24000, 20) == 7
    assert max_units_by_leverage(0.3, 2, 24000, 20) == 1  # very tight stop: no room to add
    capped = capped_rules(PyramidRules(), {"entry": 24000, "stop": 23973}, 20)
    assert capped.max_adds == 6
    assert capped_rules(PyramidRules(max_adds=2), {"entry": 24000, "stop": 23973}, 20).max_adds == 2
