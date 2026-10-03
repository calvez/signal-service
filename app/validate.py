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

    # 3. his H1/D1 rule is decided by code, whatever the model says
    if exp.htf_alignment == "conflict":
        out.notes.append("htf_conflict")
        out.summary = "forced none: H1/D1 conflict"
        return out

    # 10. round first, so every check below sees the prices that would be shown
    s = read.setup
    digits = exp.digits
    entry, stop, target = (round(x, digits) for x in (s.entry, s.stop, s.target))
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, target)):
        return _reject(out, "non-finite or non-positive price")
    if not (math.isfinite(exp.atr) and exp.atr > 0):
        return _reject(out, "ATR unavailable")

    # 4. price order
    if s.direction == "long" and not stop < entry < target:
        return _reject(out, "long needs stop < entry < target")
    if s.direction == "short" and not target < entry < stop:
        return _reject(out, "short needs target < entry < stop")

    # 5. stop distance in ATR terms
    risk = abs(entry - stop)
    if not rules.stop_atr_min * exp.atr <= risk <= rules.stop_atr_max * exp.atr:
        return _reject(out, f"stop distance {risk / exp.atr:.2f} ATR outside allowed range")

    # 6. entry close to the market
    if abs(entry - exp.last_close) > rules.entry_max_atr_from_close * exp.atr:
        return _reject(out, "entry too far from the last close")

    # 7. reward to risk
    if abs(target - entry) / risk < rules.min_reward_risk:
        return _reject(out, "reward/risk below minimum")

    out.setup = {**s.model_dump(), "entry": entry, "stop": stop, "target": target}
    action = read.action

    # 8. counter-trend only at the edge of a trading range
    if not s.with_trend:
        at_edge = exp.pct_in_range >= 80 or exp.pct_in_range <= 20
        if not (exp.day_type_hint == "trading_range" and at_edge):
            if action == "alert":
                out.notes.append("counter-trend outside a range edge: alert -> watch")
            action = "watch"

    # 9. only configured grades make a sound; the rest is a silent watch
    if action == "alert" and s.grade not in rules.push_grades:
        out.notes.append(f"grade {s.grade} is not pushed: alert -> watch")
        action = "watch"

    out.action = action
    out.push = action == "alert"
    out.summary = "ok" if not out.notes else "ok: " + "; ".join(out.notes)
    return out
