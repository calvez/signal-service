"""Advisors for decisions during a running campaign (app/campaign.py).

Python decides WHEN to ask ("the situation applies") and WHICH options are allowed; the advisor
picks one. Principle: the AI may only REDUCE risk — close early, tighten a stop, or veto an
add-on. It can never widen a stop, add on its own or keep a position past a hard rule. The
mandatory rules (server-side stop, breakeven, trailing, always-in flip, session flatten, daily
guard) run before any advisor is asked and cannot be overridden.

    RuleAdvisor   the spec's behaviour (hold, keep the stop, take the add the rules allow)
    LlmAdvisor    asks the LLM (prompts/manage_v1.md); on any error or invalid answer it
                  returns the point's SAFE default (exit/tighten: hold/keep; add: skip)
"""

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import db
from app.llm import parse_json_object

log = logging.getLogger("signal.advisor")
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Option:
    id: str
    text: str  # what the option does, with prices
    sl: float | None = None  # tighten options: the new common stop


@dataclass
class DecisionPoint:
    kind: str  # add | exit | tighten
    triggers: list[str]
    options: list[Option]
    rule_choice: str  # what the spec's rules would do
    safe_choice: str  # what to do if the AI fails (never increases risk)
    extra: dict = field(default_factory=dict)

    def option(self, oid: str) -> Option | None:
        return next((o for o in self.options if o.id == oid), None)


class RuleAdvisor:
    """The spec without AI: hold, keep the stop, take every add the rules allow."""

    name = "rules"

    def decide(self, point: DecisionPoint, campaign, ev) -> tuple[str, str]:
        return point.rule_choice, "rule"


# --------------------------------------------------------------------------- LLM
class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_version: int = Field(alias="schema")
    choice: str
    reason: str = Field(max_length=300)


def _positions_text(c, digits: int, price: float) -> str:
    lines = []
    for p in c.positions:
        r = c.sign * (price - p.entry) * p.size / c.r_pts
        lines.append(
            f"  {p.kind:5} entry {p.entry:.{digits}f}  stop {p.sl:.{digits}f}  "
            f"size {p.size:g}x  now {r:+.2f}R"
        )
    return "\n".join(lines) or "  (none)"


class LlmAdvisor:
    """Asks the LLM. `cache_path`: optional SQLite file that remembers answers by prompt hash
    (backtests: a rerun costs nothing and gives the same decisions)."""

    name = "llm"

    def __init__(self, settings, llm, cache_path: str | None = None, version: str = "manage_v1"):
        self.s = settings
        self.llm = llm
        self.version = version
        self.cache_path = cache_path
        text = (PROMPT_DIR / f"{version}.md").read_text()
        import re

        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        m = re.search(r"##\s*System\s*(.*?)##\s*User\s*(.*)", text, flags=re.S)
        self.system, self.user_tpl = m.group(1).strip(), m.group(2).strip()
        self.calls = 0
        self.cache_hits = 0
        if cache_path:
            conn = sqlite3.connect(cache_path)
            conn.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, text TEXT, "
                         "created INTEGER)")  # fmt: skip
            conn.commit()
            conn.close()

    # ---- prompt
    def prompt(self, point: DecisionPoint, c, ev) -> str:
        from app.reader import prompt_values, render

        cfg = self.s.config
        d = ev.digits
        price = ev.last_close
        first = next((p for p in c.positions if p.kind == "entry"), None)
        values = prompt_values(cfg, ev)
        open_r = sum(c.sign * (price - p.entry) * p.size for p in c.positions) / c.r_pts
        first_r = 0.0 if first is None else c.sign * (price - first.entry) / c.r_pts
        z = ZoneInfo(ev.session_tz)
        flat_at = ev.session_end - cfg.brooks.flatten_mins * 60
        values.update({
            "minutes_to_flat": max(0, (flat_at - (ev.bar_open + 300)) // 60),
            "campaign_id": c.id, "direction": c.direction, "setup": c.setup,
            "r_points": f"{c.r_pts:.{d}f}", "positions": _positions_text(c, d, price),
            "open_r": f"{open_r:+.2f}R", "first_r": f"{first_r:+.2f}R", "mfe_r": f"{c.max_r:+.2f}R",
            "mae_r": f"{c.min_r:+.2f}R", "bars_in_trade": c.bars_in_trade, "adds": c.adds,
            "point": point.kind, "triggers": "; ".join(point.triggers),
            "options": "\n".join(f"- {o.id}: {o.text}" for o in point.options),
            "bar_time_local": f"{datetime.fromtimestamp(ev.bar_open, tz=z):%Y-%m-%d %H:%M}",
        })  # fmt: skip
        return render(self.user_tpl, values)

    # ---- cache
    def _key(self, user: str) -> str:
        return hashlib.sha256(
            f"{self.s.config.llm.model}\n{self.system}\n{user}".encode()
        ).hexdigest()

    def _cached(self, key: str) -> str | None:
        if not self.cache_path:
            return None
        conn = sqlite3.connect(self.cache_path)
        try:
            row = conn.execute("SELECT text FROM llm_cache WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

    def _store(self, key: str, text: str) -> None:
        if not self.cache_path:
            return
        conn = sqlite3.connect(self.cache_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?)", (key, text, int(time.time()))
            )
            conn.commit()
        finally:
            conn.close()

    # ---- decide
    def decide(self, point: DecisionPoint, c, ev) -> tuple[str, str]:
        if ev is None:
            return point.safe_choice, "no market context: safe choice"
        user = self.prompt(point, c, ev)
        key = self._key(user)
        text = self._cached(key)
        call_id = None
        if text is not None:
            self.cache_hits += 1
        else:
            res = self.llm.complete(self.system, user, purpose=f"manage_{point.kind}")
            self.calls += 1
            call_id = res.call_id
            if not res.ok:
                return point.safe_choice, f"llm error ({res.error}): safe choice"
            text = res.text or ""
        raw = parse_json_object(text)
        verdict = "ok"
        try:
            ans = _Answer.model_validate(raw) if raw is not None else None
        except ValidationError:
            ans = None
        if ans is None or point.option(ans.choice) is None:
            verdict = "rejected: invalid answer or unknown option"
            choice, reason = point.safe_choice, "invalid AI answer: safe choice"
        else:
            choice, reason = ans.choice, ans.reason
            if call_id is None or text is not None:
                self._store(key, text)
        if call_id is not None:
            conn = db.connect(self.s.db_path)
            try:
                db.update_llm_call(
                    conn, call_id, json.dumps(raw) if raw is not None else None, verdict
                )
            finally:
                conn.close()
        return choice, reason


def utc_iso(t: int) -> str:
    return datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
