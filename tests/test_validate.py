import copy

import pytest
import yaml

from app.config import AppConfig
from app.validate import Expected, validate_read

RULES = AppConfig.model_validate(yaml.safe_load(open("config.example.yaml"))).rules
HTF_RULES = RULES.model_copy(update={"require_htf_alignment": True})  # his H1/D1 rule switched on
BAR = "2026-10-05T07:25:00Z"
SETUP = {
    "direction": "long", "type": "H2", "with_trend": True, "entry_type": "stop",
    "entry": 24325.0, "stop": 24298.0, "target": 24379.0, "grade": "A",
}  # fmt: skip
GOOD = {
    "schema": 1,
    "symbol": "GER40.cash",
    "bar_time_utc": BAR,
    "context": {
        "htf_alignment": "aligned_bull",
        "day_type": "trend_from_open",
        "always_in": "long",
    },
    "action": "alert",
    "setup": SETUP,
    "reason": "H2 with trend",
}


def exp(**over):
    base = dict(symbol="GER40.cash", bar_time_utc=BAR, htf_alignment="aligned_bull", atr=30.0,
                last_close=24320.0, day_type_hint="trend", pct_in_range=60.0, digits=1)  # fmt: skip
    base.update(over)
    return Expected(**base)


def raw(**over):
    r = copy.deepcopy(GOOD)
    setup_over = over.pop("setup", None)
    if setup_over is not None:
        r["setup"] = (
            setup_over if setup_over == {} or setup_over is None else {**r["setup"], **setup_over}
        )
    r.update(over)
    return r


def test_good_alert_passes_and_pushes():
    out = validate_read(GOOD, exp(), RULES)
    assert out.valid and out.action == "alert" and out.push and out.summary == "ok"


def test_none_is_fine():
    out = validate_read(raw(action="none", setup=None) | {"setup": None}, exp(), RULES)
    assert out.valid and out.action == "none" and not out.push


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"schema": 2},
        {k: v for k, v in GOOD.items() if k != "reason"},
        {**GOOD, "extra": 1},
        {**GOOD, "action": "buy"},
        {**GOOD, "reason": "x" * 301},
        {**GOOD, "context": {**GOOD["context"], "day_type": "weird"}},
        {**GOOD, "setup": {**SETUP, "type": "magic"}},
        {**GOOD, "setup": {**SETUP, "entry": "abc"}},
        {**GOOD, "setup": {**SETUP, "entry_type": "market"}},
        {**GOOD, "setup": {**SETUP, "grade": "C"}},
        {**GOOD, "setup": None},  # alert without setup
        {**GOOD, "action": "none"},  # none with a setup
    ],
)
def test_schema_failures_force_none(bad):
    out = validate_read(bad, exp(), RULES)
    assert not out.valid and out.action == "none" and not out.push and out.setup is None
    assert out.summary.startswith("rejected")


def test_identity_must_match_request():
    assert not validate_read(raw(symbol="US100.cash"), exp(), RULES).valid
    assert not validate_read(raw(bar_time_utc="2026-10-05T07:20:00Z"), exp(), RULES).valid


def test_htf_conflict_overrides_model():
    out = validate_read(GOOD, exp(htf_alignment="conflict"), HTF_RULES)
    assert out.action == "none" and not out.push and "htf_conflict" in out.notes
    # switched off (Lorant 2026-10-04): a conflict no longer blocks
    assert validate_read(GOOD, exp(htf_alignment="conflict"), RULES).action == "alert"


@pytest.mark.parametrize(
    "setup",
    [
        {"stop": 24330.0},  # long with stop above entry
        {"target": 24320.0},  # target below entry
        {"direction": "short"},  # short with long prices
    ],
)
def test_price_order(setup):
    out = validate_read(raw(setup=setup), exp(), RULES)
    assert not out.valid and out.action == "none" and "needs" in out.summary


def test_short_with_correct_order_passes():
    s = {"direction": "short", "entry": 24320.0, "stop": 24345.0, "target": 24270.0, "type": "L2"}
    assert validate_read(raw(setup=s), exp(last_close=24319.0), RULES).action == "alert"


def test_stop_distance_limits_in_atr():
    # ATR 30: allowed risk 9 .. 90 points
    assert not validate_read(raw(setup={"stop": 24320.0}), exp(), RULES).valid  # 5 pts: too tight
    assert not validate_read(raw(setup={"stop": 24200.0, "target": 24600.0}), exp(), RULES).valid
    assert validate_read(raw(setup={"stop": 24316.0}), exp(), RULES).valid  # 9 pts = 0.3 ATR


def test_entry_must_be_near_last_close():
    out = validate_read(raw(), exp(last_close=24250.0), RULES)  # 75 pts away > 1 ATR
    assert not out.valid and "far" in out.summary


def test_reward_risk_minimum():
    out = validate_read(raw(setup={"target": 24340.0}), exp(), RULES)  # 15 reward vs 27 risk
    assert not out.valid and "reward" in out.summary


def test_counter_trend_downgraded_unless_range_edge():
    ct = {"with_trend": False}
    out = validate_read(raw(setup=ct), exp(day_type_hint="trend", pct_in_range=10), RULES)
    assert out.valid and out.action == "watch" and not out.push
    out = validate_read(raw(setup=ct), exp(day_type_hint="trading_range", pct_in_range=50), RULES)
    assert out.action == "watch"
    for pct in (5, 20, 80, 95):
        out = validate_read(
            raw(setup=ct), exp(day_type_hint="trading_range", pct_in_range=pct), RULES
        )
        assert out.action == "alert", pct
    out = validate_read(raw(setup=ct), exp(day_type_hint="trading_range", pct_in_range=21), RULES)
    assert out.action == "watch"


def test_grade_b_is_silent_unless_configured():
    out = validate_read(raw(setup={"grade": "B"}), exp(), RULES)
    assert out.action == "watch" and not out.push
    both = RULES.model_copy(update={"push_grades": ["A", "B"]})
    assert validate_read(raw(setup={"grade": "B"}), exp(), both).push


def test_watch_stays_watch_and_is_never_pushed():
    out = validate_read(raw(action="watch"), exp(), RULES)
    assert out.valid and out.action == "watch" and not out.push


def test_prices_rounded_to_symbol_digits():
    s = {"entry": 24325.04, "stop": 24298.0, "target": 24379.0}
    out = validate_read(raw(setup=s), exp(digits=1), RULES)
    assert out.setup["entry"] == 24325.0
    out = validate_read(raw(setup={"entry": 24325.26, "target": 24379.123}), exp(digits=0), RULES)
    assert out.setup["entry"] == 24325 and out.setup["target"] == 24379


@pytest.mark.parametrize("atr", [float("nan"), 0.0, -1.0])
def test_unusable_atr_rejects(atr):
    assert not validate_read(GOOD, exp(atr=atr), RULES).valid


# ------------------------------------------------------------------ prompt v3: recommendations
from app.validate import validate_recommendation  # noqa: E402

CANDS = [
    {"direction": "long", "type": "H2", "with_trend": True, "entry_type": "stop",
     "entry": 24325.0, "stop": 24298.0, "target": 24379.0, "grade": "A"},
    {"direction": "short", "type": "L2", "with_trend": False, "entry_type": "stop",
     "entry": 24310.0, "stop": 24337.0, "target": 24256.0, "grade": "B"},
]  # fmt: skip
REC = {
    "schema": 2, "symbol": "GER40.cash", "bar_time_utc": BAR,
    "context": {"htf_alignment": "aligned_bull", "day_type": "trend_from_open", "always_in": "long"},
    "decision": "take", "candidate_id": 1, "grade": "A", "reason": "clean H2 at the EMA",
}  # fmt: skip


def rec(**over):
    return {**REC, **over}


def test_take_uses_python_prices_and_pushes():
    out = validate_recommendation(rec(), CANDS, exp(), RULES)
    assert out.action == "alert" and out.push and out.valid
    assert out.setup["entry"] == 24325.0 and out.setup["type"] == "H2"


def test_llm_cannot_smuggle_in_prices():
    out = validate_recommendation(rec(entry=1.0), CANDS, exp(), RULES)  # extra key
    assert not out.valid and out.action == "none"


def test_skip_and_watch():
    assert (
        validate_recommendation(
            rec(decision="skip", candidate_id=None, grade=None), CANDS, exp(), RULES
        ).action
        == "none"
    )
    w = validate_recommendation(rec(decision="watch"), CANDS, exp(), RULES)
    assert w.action == "watch" and not w.push


@pytest.mark.parametrize("over", [{"candidate_id": 3}, {"candidate_id": 0}, {"candidate_id": None},
                                  {"grade": None}, {"decision": "buy"}, {"schema": 1},
                                  {"symbol": "US100.cash"}])  # fmt: skip
def test_bad_recommendations_fail_closed(over):
    out = validate_recommendation(rec(**over), CANDS, exp(), RULES)
    assert out.action == "none" and not out.push


def test_grade_b_from_llm_is_silent_and_rules_still_apply():
    out = validate_recommendation(rec(grade="B"), CANDS, exp(), RULES)
    assert out.action == "watch"
    ct = validate_recommendation(rec(candidate_id=2), CANDS, exp(last_close=24312.0), RULES)
    assert ct.action == "watch"  # counter-trend outside a range edge is downgraded
    assert (
        validate_recommendation(rec(), CANDS, exp(htf_alignment="conflict"), HTF_RULES).action
        == "none"
    )
