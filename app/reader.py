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

from app import db, features, htf, sessions
from app.config import Settings
from app.llm import LlmClient, parse_json_object
from app.validate import Expected, validate_read

log = logging.getLogger("signal.reader")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
M5_SEC = 300
MIN_HISTORY_BARS = 60  # EMA/ATR/swings need warm-up
STALE_AFTER_SEC = 2 * M5_SEC  # CLAUDE.md: never act on data older than two bar periods
M5_LOAD = 600

# Skips that are normal every day are only logged to the console, not stored as events.
QUIET_SKIPS = {"not_traded", "outside_session"}

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

    m5 = db.load_bars(conn, symbol, "M5", until_utc=bar_open, limit=M5_LOAD)
    if m5.empty or int(m5.index[-1].timestamp()) != bar_open:
        return _skip(conn, symbol, bar_open, "bar_not_stored")
    if len(m5) < MIN_HISTORY_BARS:
        return _skip(conn, symbol, bar_open, "not_enough_history")

    # ---- deterministic higher-timeframe check (his H1/D1 rule)
    asof = pd.Timestamp(bar_open + M5_SEC, unit="s", tz="UTC")  # the evaluated bar has closed
    fc = cfg.features
    h1 = htf.htf_state(
        db.load_bars(conn, symbol, "H1", until_utc=bar_open + M5_SEC, limit=300),
        htf.H1_SEC, asof, fc.ema_period, fc.swing_confirm_bars,
    )  # fmt: skip
    d1 = htf.htf_state(
        db.load_bars(conn, symbol, "D1", until_utc=bar_open + M5_SEC, limit=150),
        htf.D1_SEC, asof, fc.ema_period, fc.swing_confirm_bars,
    )  # fmt: skip
    alignment = htf.alignment(h1.state, d1.state, cfg.rules.htf_neutral_counts_as_conflict)
    if alignment == "conflict":
        return _skip(conn, symbol, bar_open, f"htf_conflict (H1 {h1.state}, D1 {d1.state})")

    # ---- features
    stz = cfg.sessions[session].tz
    day = pd.Series(m5.index.tz_convert(stz).date, index=m5.index)
    feats = features.compute_features(m5, fc.ema_period, fc.atr_period, fc.swing_confirm_bars, day)
    last = feats.iloc[-1]
    atr_now = float(last["atr"])
    if pd.isna(atr_now):
        return _skip(conn, symbol, bar_open, "atr_unavailable")

    local_day = sessions.local_date(cfg, session, bar_open)
    prev_day = sessions.previous_trading_day(cfg, session, local_day)
    cutoff = pd.Timestamp(sessions.cash_close_utc(cfg, session, prev_day), unit="s", tz="UTC")
    ctx = features.day_context(
        feats,
        pd.Timestamp(session_start, unit="s", tz="UTC"),
        fc.opening_range_bars,
        features.prior_close(m5, cutoff),
    )
    hint = features.day_type_hint(feats, ctx, atr_now)
    if ctx is None:
        return _skip(conn, symbol, bar_open, "no_day_context")

    # ---- prompt
    bar_iso = _iso(bar_open)
    z = ZoneInfo(stz)
    last_close = float(last["c"])
    fmt = lambda x: f"{x:.{digits}f}"  # noqa: E731
    values = {
        "symbol": symbol,
        "name": sym.name,
        "session": session.upper(),
        "bar_index_in_session": sessions.bar_index_in_session(cfg, session, bar_open),
        "bar_time_utc": bar_iso,
        "bar_time_local": f"{datetime.fromtimestamp(bar_open, tz=z):%Y-%m-%d %H:%M}",
        "session_tz": stz,
        "tick_size": f"{10**-digits:.{digits}f}",
        "atr": fmt(atr_now),
        "h1_state": h1.state,
        "d1_state": d1.state,
        "htf_alignment": alignment,
        "day_type_hint": hint,
        "session_open": fmt(ctx["session_open"]),
        "or_low": fmt(ctx["or_low"]),
        "or_high": fmt(ctx["or_high"]),
        "day_low": fmt(ctx["day_low"]),
        "day_high": fmt(ctx["day_high"]),
        "pct_in_range": f"{ctx['pct_in_range']:.0f}",
        "ema_crosses": ctx["ema_crosses"],
        "bars_same_side": ctx["bars_same_side"],
        "gap_pts": "n/a" if ctx["gap_pts"] is None else f"{ctx['gap_pts']:+.{digits}f}",
        "swings": _swings_text(feats, stz, digits),
        "leg_count": _legs_text(last),
        "news_window": cfg.news_window_min,
        "news_flag": sessions.news_flag(cfg, bar_open) or "none",
        "n": min(fc.prompt_bars, len(feats)),
        "bar_table": _bar_table(feats, fc.prompt_bars, stz, digits),
        "schema_json": schema_json(symbol, bar_iso),
    }
    system, user_tpl = load_prompt(cfg.llm.prompt_version)
    user = render(user_tpl, values)

    # ---- ask, parse, validate
    res = llm.complete(system, user, purpose="market_read")
    base = {
        "ts_utc": int(now),
        "symbol": symbol,
        "bar_time_utc": bar_open,
        "session": session,
        "llm_call_id": res.call_id,
        "model": cfg.llm.model,
        "prompt_version": cfg.llm.prompt_version,
        "htf_alignment": alignment,
        "day_type_hint": hint,
        "atr": atr_now,
        "last_close": last_close,
    }
    if not res.ok:
        row = {**base, "action": "none", "validation": f"llm error: {res.error}"}
        return ReadResult("stored", row["validation"], db.insert_read(conn, row), "none")

    raw = parse_json_object(res.text or "")
    expected = Expected(symbol, bar_iso, alignment, atr_now, last_close, hint,
                        ctx["pct_in_range"], digits)  # fmt: skip
    out = validate_read(raw, expected, cfg.rules)
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
