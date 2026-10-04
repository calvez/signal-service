"""Validation of the LLM's market read (docs/protocol.md §4). LLM output is UNTRUSTED.

`validate_read` never raises. It returns an Outcome whose `action` is what the rest of the
system may act on: if any rule fails the action is forced to `none` and the reason is kept.
"""

import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import RulesCfg


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadContext(_Strict):
    htf_alignment: Literal["aligned_bull", "aligned_bear", "conflict"]
    day_type: Literal[
        "trend_from_open",
        "spike_and_channel",
        "trading_range",
        "broad_channel",
        "tight_channel",
        "unclear",
    ]
    always_in: Literal["long", "short", "neutral"]


class Setup(_Strict):
    direction: Literal["long", "short"]
    type: Literal[
        "H1", "H2", "L1", "L2", "wedge", "failed_breakout", "breakout_pullback",
        "double_bottom", "double_top", "other",
    ]  # fmt: skip
    with_trend: bool
    entry_type: Literal["stop"]
    entry: float
    stop: float
    target: float
    grade: Literal["A", "B"]


class MarketRead(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    symbol: str
    bar_time_utc: str
    context: ReadContext
    action: Literal["none", "watch", "alert"]
    setup: Setup | None
    reason: str = Field(max_length=300)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


@dataclass(frozen=True)
class Expected:
    """What the code knows for sure about this request; the model must agree with it."""

    symbol: str
    bar_time_utc: str  # e.g. "2026-10-05T07:25:00Z" (open time of the evaluated bar)
    htf_alignment: str  # deterministic H1/D1 result
    atr: float
    last_close: float
    day_type_hint: str
    pct_in_range: float
    digits: int


@dataclass
class Outcome:
    action: str = "none"  # final: none | watch | alert
    push: bool = False  # True only for an alert that should make a sound
    valid: bool = True  # False when a rule rejected the answer
    summary: str = "ok"  # short text stored in llm_calls.validation / reads.validation
    notes: list[str] = field(default_factory=list)
    read: MarketRead | None = None
    recommendation: "Recommendation | None" = None  # prompt v3 answer
    setup: dict | None = None  # final setup, prices rounded to the symbol's digits


def _reject(out: Outcome, why: str) -> Outcome:
    out.action, out.push, out.valid, out.setup = "none", False, False, None
    out.summary = f"rejected: {why}"
    return out


def validate_read(raw: dict | None, exp: Expected, rules: RulesCfg) -> Outcome:
    out = Outcome()
    if raw is None:
        return _reject(out, "not a JSON object")
    try:
        read = MarketRead.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return _reject(out, f"schema ({'.'.join(map(str, first['loc']))}: {first['type']})")
    out.read = read

    # 2. the answer is about the request we sent
    if read.symbol != exp.symbol or read.bar_time_utc != exp.bar_time_utc:
        return _reject(out, "symbol/bar_time_utc do not match the request")

    # action vs setup consistency
    if read.action == "none":
        if read.setup is not None:
            return _reject(out, "action none but a setup was given")
        out.summary = "ok"
        return out
    if read.setup is None:
        return _reject(out, f"action {read.action} without a setup")

    return check_setup(read.setup.model_dump(), read.action, exp, rules, out)


def check_setup(
    setup: dict, action: str, exp: Expected, rules: RulesCfg, out: Outcome | None = None
) -> Outcome:
    """Rules 3-10 for one setup, whoever proposed it (the LLM or a Python strategy).

    `setup` needs direction, with_trend, entry, stop, target and grade (other keys are kept).
    `action` is the wanted action (alert | watch). Returns the final Outcome.
    """
    out = out or Outcome()

    # 3. his H1/D1 rule is decided by code, whatever the model says (switchable for backtests)
    if exp.htf_alignment == "conflict" and rules.require_htf_alignment:
        out.notes.append("htf_conflict")
        out.summary = "forced none: H1/D1 conflict"
        return out

    # 10. round first, so every check below sees the prices that would be shown
    digits = exp.digits
    # A missing target is allowed: pyramiding runners have no fixed target (app/position.py).
    try:
        entry, stop = (round(float(setup[k]), digits) for k in ("entry", "stop"))
        target = None if setup.get("target") is None else round(float(setup["target"]), digits)
    except (KeyError, TypeError, ValueError):
        return _reject(out, "setup without valid entry/stop/target")
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, *([target] if target else []))):
        return _reject(out, "non-finite or non-positive price")
    if not (math.isfinite(exp.atr) and exp.atr > 0):
        return _reject(out, "ATR unavailable")

    # 4. price order
    direction = setup.get("direction")
    far = target if target is not None else entry + (1 if direction == "long" else -1)
    if direction == "long" and not stop < entry < far:
        return _reject(out, "long needs stop < entry < target")
    if direction == "short" and not far < entry < stop:
        return _reject(out, "short needs target < entry < stop")
    if direction not in ("long", "short"):
        return _reject(out, "direction must be long or short")

    # 5. stop distance in ATR terms
    risk = abs(entry - stop)
    if not rules.stop_atr_min * exp.atr <= risk <= rules.stop_atr_max * exp.atr:
        return _reject(out, f"stop distance {risk / exp.atr:.2f} ATR outside allowed range")

    # 6. entry close to the market
    if abs(entry - exp.last_close) > rules.entry_max_atr_from_close * exp.atr:
        return _reject(out, "entry too far from the last close")

    # 7. reward to risk (only with a fixed target)
    if target is not None and abs(target - entry) / risk < rules.min_reward_risk:
        return _reject(out, "reward/risk below minimum")

    out.setup = {**setup, "entry": entry, "stop": stop, "target": target}

    # 8. counter-trend only at the edge of a trading range
    if not setup.get("with_trend", True):
        at_edge = exp.pct_in_range >= 80 or exp.pct_in_range <= 20
        if not (exp.day_type_hint == "trading_range" and at_edge):
            if action == "alert":
                out.notes.append("counter-trend outside a range edge: alert -> watch")
            action = "watch"

    # 9. only configured grades make a sound; the rest is a silent watch
    if action == "alert" and setup.get("grade") not in rules.push_grades:
        out.notes.append(f"grade {setup.get('grade')} is not pushed: alert -> watch")
        action = "watch"

    out.action = action
    out.push = action == "alert"
    out.summary = "ok" if not out.notes else "ok: " + "; ".join(out.notes)
    return out


# --------------------------------------------------------------------------- prompt v3
class Recommendation(_Strict):
    """The LLM's answer when a Python strategy proposed candidates (prompt v3, schema 2)."""

    schema_version: Literal[2] = Field(alias="schema")
    symbol: str
    bar_time_utc: str
    context: ReadContext
    decision: Literal["take", "watch", "skip"]
    candidate_id: int | None
    grade: Literal["A", "B"] | None
    reason: str = Field(max_length=300)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def validate_recommendation(
    raw: dict | None, candidates: list[dict], exp: Expected, rules: RulesCfg
) -> Outcome:
    """Check the LLM's take/watch/skip for the offered candidates (already validated by
    check_setup; `candidates[i]` is the setup dict of candidate id i+1).

    The final setup always carries the Python prices; the LLM only adds its grade and reason.
    Fails closed: anything unexpected -> action none.
    """
    out = Outcome()
    if raw is None:
        return _reject(out, "not a JSON object")
    try:
        rec = Recommendation.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        return _reject(out, f"schema ({'.'.join(map(str, first['loc']))}: {first['type']})")
    out.recommendation = rec
    if rec.symbol != exp.symbol or rec.bar_time_utc != exp.bar_time_utc:
        return _reject(out, "symbol/bar_time_utc do not match the request")
    if rec.decision == "skip":
        out.summary = "ok: skipped by the LLM"
        return out
    if rec.candidate_id is None or not 1 <= rec.candidate_id <= len(candidates):
        return _reject(out, f"{rec.decision} needs a candidate_id from the list")
    if rec.grade is None:
        return _reject(out, f"{rec.decision} needs a grade")
    setup = {**candidates[rec.candidate_id - 1], "grade": rec.grade}
    action = "alert" if rec.decision == "take" else "watch"
    return check_setup(setup, action, exp, rules, out)
