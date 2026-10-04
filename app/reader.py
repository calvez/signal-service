"""Market read: build the prompt from computed features, ask the LLM, validate, store.

`run_read` is called for each newly closed M5 bar of a traded symbol. It NEVER raises for data
or model problems: it either skips (no LLM call, reason logged) or stores a read whose final
`action` has passed `validate.py`. Telegram (T8) only ever looks at stored, validated reads.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app import db, features, sessions
from app.config import Settings
from app.evaluation import D1_WINDOW, H1_WINDOW, M5_WINDOW, Skip, evaluate_bar
from app.llm import LlmClient, parse_json_object
from app.strategies import get_strategy
from app.validate import check_setup, validate_read, validate_recommendation

log = logging.getLogger("signal.reader")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
M5_SEC = 300
STALE_AFTER_SEC = 2 * M5_SEC  # CLAUDE.md: never act on data older than two bar periods

# Skips that are normal every day are only logged to the console, not stored as events.
QUIET_SKIPS = {"not_traded", "outside_session", "no_candidate"}

_inflight: set[tuple[str, int]] = set()
_inflight_lock = threading.Lock()


@dataclass(frozen=True)
class ReadResult:
    status: str  # "skipped" | "stored"
    reason: str = ""
    read_id: int | None = None
    action: str = "none"


# --------------------------------------------------------------------------- prompt
def load_prompt(version: str) -> tuple[str, str]:
    """(system, user template) from prompts/market_read_<version>.md."""
    text = (PROMPT_DIR / f"market_read_{version}.md").read_text()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m = re.search(r"##\s*System\s*(.*?)##\s*User\s*(.*)", text, flags=re.S)
    if not m:
        raise ValueError(f"prompt {version} needs '## System' and '## User' sections")
    return m.group(1).strip(), m.group(2).strip()


def render(template: str, values: dict[str, object]) -> str:
    """Fill {placeholders} in ONE pass (inserted text is never re-scanned). A placeholder with
    no value is an error: better no read than a prompt with a hole in it."""
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            missing.append(key)
            return m.group(0)
        return str(values[key])

    out = re.sub(r"\{(\w+)\}", sub, template)
    if missing:
        raise KeyError(f"unfilled placeholders: {sorted(set(missing))}")
    return out


def schema_json(symbol: str, bar_time_iso: str) -> str:
    """The answer shape shown to the model, with the request's own symbol and time filled in."""
    return json.dumps(
        {
            "schema": 1,
            "symbol": symbol,
            "bar_time_utc": bar_time_iso,
            "context": {
                "htf_alignment": "aligned_bull | aligned_bear | conflict",
                "day_type": "trend_from_open | spike_and_channel | trading_range | "
                "broad_channel | tight_channel | unclear",
                "always_in": "long | short | neutral",
            },
            "action": "none | watch | alert",
            "setup": "null, or {direction: long|short, type: H1|H2|L1|L2|wedge|failed_breakout|"
            "breakout_pullback|double_bottom|double_top|other, with_trend: true|false, "
            'entry_type: "stop", entry: number, stop: number, target: number, grade: A|B}',
            "reason": "max 300 chars, plain language",
        },
        indent=2,
    )


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bar_table(feats: pd.DataFrame, n: int, tz: str, digits: int) -> str:
    z = ZoneInfo(tz)
    lines = []
    for ts, r in feats.tail(n).iterrows():
        sig = "L" if r["sig_long"] else "S" if r["sig_short"] else "-"
        lines.append(
            f"{ts.astimezone(z):%H:%M} {r['o']:.{digits}f} {r['h']:.{digits}f} "
            f"{r['l']:.{digits}f} {r['c']:.{digits}f} {r['ema']:.{digits}f} "
            f"{r['bar_type']} {r['close_pos']:.2f} {sig}"
        )
    return "\n".join(lines)


def _swings_text(feats: pd.DataFrame, tz: str, digits: int) -> str:
    z = ZoneInfo(tz)
    sw = features.recent_swings(feats, 6)
    if not sw:
        return "none confirmed yet"
    return "; ".join(f"{s.kind} {s.price:.{digits}f} @{s.time.astimezone(z):%H:%M}" for s in sw)


def _legs_text(last: pd.Series) -> str:
    h = f"H count {int(last['h_count'])}" + (" (this bar is an H bar)" if last["h_bar"] else "")
    lo = f"L count {int(last['l_count'])}" + (" (this bar is an L bar)" if last["l_bar"] else "")
    return f"{h}; {lo}"


# --------------------------------------------------------------------------- main entry
def _skip(conn, symbol: str, bar_open: int, reason: str) -> ReadResult:
    log.info("read skipped %s %s: %s", symbol, _iso(bar_open), reason)
    if reason not in QUIET_SKIPS:
        db.log_event(conn, "read_skipped", {"symbol": symbol, "bar": _iso(bar_open), "why": reason})
    return ReadResult("skipped", reason)


def run_read(
    settings: Settings,
    llm: LlmClient,
    symbol: str,
    bar_open_utc: int,
    now: float | None = None,
) -> ReadResult:
    key = (symbol, bar_open_utc)
    with _inflight_lock:
        if key in _inflight:
            return ReadResult("skipped", "already_running")
        _inflight.add(key)
    conn = db.connect(settings.db_path)
    try:
        return _run(settings, llm, conn, symbol, bar_open_utc, time.time() if now is None else now)
    except Exception:  # fail closed: a bug must never become an alert
        log.exception("read crashed for %s %s", symbol, _iso(bar_open_utc))
        db.log_event(conn, "read_crashed", {"symbol": symbol, "bar": _iso(bar_open_utc)})
        return ReadResult("skipped", "crashed")
    finally:
        conn.close()
        with _inflight_lock:
            _inflight.discard(key)


def _run(settings, llm, conn, symbol, bar_open, now) -> ReadResult:
    cfg = settings.config
    sym = cfg.symbols.get(symbol)
    if sym is None or sym.role != "traded":
        return _skip(conn, symbol, bar_open, "not_traded")

    active = sessions.active_session(cfg, bar_open)
    if active is None or active[0] != sym.session:
        return _skip(conn, symbol, bar_open, "outside_session")
    session, session_start, _ = active

    if now - (bar_open + M5_SEC) > STALE_AFTER_SEC:
        return _skip(conn, symbol, bar_open, "stale_data")
    if db.read_exists(conn, symbol, bar_open):
        return _skip(conn, symbol, bar_open, "already_read")
    if llm.spend_today() >= cfg.llm.daily_budget_usd:
        return _skip(conn, symbol, bar_open, "budget_exceeded")

    digits = db.get_digits(conn, symbol)
    if digits is None:
        return _skip(conn, symbol, bar_open, "unknown_digits")

    # ---- the Python evaluation (the same code the backtester runs)
    try:
        ev = evaluate_bar(
            cfg, symbol, bar_open,
            db.load_bars(conn, symbol, "M5", until_utc=bar_open, limit=M5_WINDOW),
            db.load_bars(conn, symbol, "H1", until_utc=bar_open + M5_SEC, limit=H1_WINDOW),
            db.load_bars(conn, symbol, "D1", until_utc=bar_open + M5_SEC, limit=D1_WINDOW),
            digits,
        )  # fmt: skip
    except Skip as skip:
        return _skip(conn, symbol, bar_open, str(skip))
    values = prompt_values(cfg, ev)
    if cfg.engine.strategy:
        return _run_engine(settings, llm, conn, ev, values, now)
    return _run_llm_only(settings, llm, conn, ev, values, now)


def prompt_values(cfg, ev) -> dict:
    """Placeholder values shared by all prompt versions (built from the Python evaluation)."""
    d, stz, ctx = ev.digits, ev.session_tz, ev.ctx
    fmt = lambda x: f"{x:.{d}f}"  # noqa: E731
    z = ZoneInfo(stz)
    return {
        "symbol": ev.symbol,
        "name": cfg.symbols[ev.symbol].name,
        "session": ev.session.upper(),
        "bar_index_in_session": ev.bar_index,
        "bar_time_utc": ev.bar_iso,
        "bar_time_local": f"{datetime.fromtimestamp(ev.bar_open, tz=z):%Y-%m-%d %H:%M}",
        "session_tz": stz,
        "tick_size": f"{10**-d:.{d}f}",
        "atr": fmt(ev.atr),
        "h1_state": ev.h1.state,
        "d1_state": ev.d1.state,
        "htf_alignment": ev.alignment,
        "h1_ema": "n/a" if ev.h1_ema is None else (
            f"{fmt(ev.h1_ema)} (last close {ev.last_close - ev.h1_ema:+.{d}f} pts from it)"),
        "day_type_hint": ev.hint,
        "session_open": fmt(ctx["session_open"]),
        "or_low": fmt(ctx["or_low"]),
        "or_high": fmt(ctx["or_high"]),
        "day_low": fmt(ctx["day_low"]),
        "day_high": fmt(ctx["day_high"]),
        "pct_in_range": f"{ctx['pct_in_range']:.0f}",
        "ema_crosses": ctx["ema_crosses"],
        "bars_same_side": ctx["bars_same_side"],
        "gap_pts": "n/a" if ctx["gap_pts"] is None else f"{ctx['gap_pts']:+.{d}f}",
        "swings": _swings_text(ev.feats, stz, d),
        "leg_count": _legs_text(ev.last),
        "news_window": cfg.news_window_min,
        "news_flag": ev.news or "none",
        "n": min(cfg.features.prompt_bars, len(ev.feats)),
        "bar_table": _bar_table(ev.feats, cfg.features.prompt_bars, stz, d),
        "schema_json": schema_json(ev.symbol, ev.bar_iso),
    }  # fmt: skip


def _base_row(cfg, ev, res, now, prompt_version: str) -> dict:
    return {
        "ts_utc": int(now),
        "symbol": ev.symbol,
        "bar_time_utc": ev.bar_open,
        "session": ev.session,
        "llm_call_id": res.call_id,
        "model": cfg.llm.model,
        "prompt_version": prompt_version,
        "htf_alignment": ev.alignment,
        "day_type_hint": ev.hint,
        "atr": ev.atr,
        "last_close": ev.last_close,
    }


def _run_llm_only(settings, llm, conn, ev, values, now) -> ReadResult:
    """No Python strategy yet: the LLM reads the chart and may propose a setup (prompt v2)."""
    cfg = settings.config
    system, user_tpl = load_prompt(cfg.llm.prompt_version)
    res = llm.complete(system, render(user_tpl, values), purpose="market_read")
    base = _base_row(cfg, ev, res, now, cfg.llm.prompt_version)
    if not res.ok:
        row = {**base, "action": "none", "validation": f"llm error: {res.error}"}
        return ReadResult("stored", row["validation"], db.insert_read(conn, row), "none")

    raw = parse_json_object(res.text or "")
    out = validate_read(raw, ev.expected(), cfg.rules)
    db.update_llm_call(conn, res.call_id, json.dumps(raw) if raw is not None else None, out.summary)
    row = {
        **base,
        "model_action": out.read.action if out.read else None,
        "action": out.action,
        "push": int(out.push),
        "grade": out.setup["grade"] if out.setup else None,
        "setup": json.dumps(out.setup) if out.setup else None,
        "context": json.dumps(out.read.context.model_dump()) if out.read else None,
        "reason": out.read.reason if out.read else None,
        "validation": out.summary,
    }
    read_id = db.insert_read(conn, row)
    return ReadResult("stored", out.summary, read_id, out.action)


# --------------------------------------------------------------------------- engine mode
def schema_json_v2(symbol: str, bar_time_iso: str) -> str:
    return json.dumps(
        {
            "schema": 2,
            "symbol": symbol,
            "bar_time_utc": bar_time_iso,
            "context": {
                "htf_alignment": "aligned_bull | aligned_bear | conflict",
                "day_type": "trend_from_open | spike_and_channel | trading_range | "
                "broad_channel | tight_channel | unclear",
                "always_in": "long | short | neutral",
            },
            "decision": "take | watch | skip",
            "candidate_id": "the id of the candidate, or null for skip",
            "grade": "A | B, or null for skip",
            "reason": "max 250 chars, plain language",
        },
        indent=2,
    )


def candidates_text(setups: list[dict], ev) -> str:
    d = ev.digits
    lines = []
    for i, s in enumerate(setups, 1):
        risk = abs(s["entry"] - s["stop"])
        rr = abs(s["target"] - s["entry"]) / risk
        trend = "with trend" if s["with_trend"] else "COUNTER-trend"
        ev_txt = ", ".join(f"{k}={v}" for k, v in s.get("evidence", {}).items()) or "-"
        lines.append(
            f"{i}) {s['type']} {s['direction']} ({trend}), strategy grade {s['grade']}: "
            f"entry {s['entry']:.{d}f} ({'buy' if s['direction'] == 'long' else 'sell'} stop), "
            f"stop {s['stop']:.{d}f} ({risk:.{d}f} pts, {risk / ev.atr:.1f} ATR), "
            f"target {s['target']:.{d}f} ({rr:.1f}R); evidence: {ev_txt}"
        )
    return "\n".join(lines)


def _run_engine(settings, llm, conn, ev, values, now) -> ReadResult:
    """Python evaluates (strategy candidates), the LLM recommends take / watch / skip."""
    cfg = settings.config
    strategy = get_strategy(cfg.engine.strategy, cfg)
    exp = ev.expected()
    setups = []
    for cand in strategy.candidates(ev):
        checked = check_setup(cand.as_setup(), "alert", exp, cfg.rules)
        if checked.setup is not None:
            setups.append({**checked.setup, "evidence": dict(cand.evidence)})
        else:
            log.info("candidate dropped %s %s: %s", ev.symbol, ev.bar_iso, checked.summary)
    if not setups:
        return _skip(conn, ev.symbol, ev.bar_open, "no_candidate")

    tag = f"{strategy.name}:{strategy.version}"
    values = {**values, "strategy": tag, "candidates": candidates_text(setups, ev),
              "schema_json": schema_json_v2(ev.symbol, ev.bar_iso)}  # fmt: skip
    version = cfg.engine.prompt_version
    system, user_tpl = load_prompt(version)
    res = llm.complete(system, render(user_tpl, values), purpose="market_read")
    base = _base_row(cfg, ev, res, now, version)
    if not res.ok:
        row = {**base, "action": "none", "validation": f"llm error: {res.error}"}
        return ReadResult("stored", row["validation"], db.insert_read(conn, row), "none")

    raw = parse_json_object(res.text or "")
    plain = [{k: v for k, v in s.items() if k != "evidence"} for s in setups]
    out = validate_recommendation(raw, plain, exp, cfg.rules)
    db.update_llm_call(conn, res.call_id, json.dumps(raw) if raw is not None else None, out.summary)
    rec = out.recommendation
    setup = None
    if out.setup is not None:
        chosen = setups[rec.candidate_id - 1]
        setup = {**out.setup, "strategy": tag, "candidate_id": rec.candidate_id,
                 "evidence": chosen["evidence"]}  # fmt: skip
    row = {
        **base,
        "model_action": rec.decision if rec else None,
        "action": out.action,
        "push": int(out.push),
        "grade": setup["grade"] if setup else None,
        "setup": json.dumps(setup) if setup else None,
        "context": json.dumps(rec.context.model_dump()) if rec else None,
        "reason": rec.reason if rec else None,
        "validation": out.summary,
    }
    read_id = db.insert_read(conn, row)
    return ReadResult("stored", out.summary, read_id, out.action)
