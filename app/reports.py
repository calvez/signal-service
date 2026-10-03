"""Texts for /status, /today, the pre-session brief, the session wrap and the daily report.

Pure read-only functions over the database: they send nothing themselves.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app import db, features, htf, sessions
from app.config import AppConfig, Settings
from app.llm import utc_day_start
from app.messages import fmt_time, money, short_name
from app.status import (
    M5_SEC,
    STALE_SEC,
    ago,
    conditions,
    day_start_balance,
    gather,
    local_day_start,
)


# --------------------------------------------------------------------------- shared pieces
def reads_summary(conn, start: int, end: int) -> dict:
    rows = conn.execute(
        "SELECT action, validation FROM reads WHERE ts_utc >= ? AND ts_utc < ?", (start, end)
    ).fetchall()
    bad = ("rejected", "llm error")
    return {
        "reads": len(rows),
        "alert": sum(r["action"] == "alert" for r in rows),
        "watch": sum(r["action"] == "watch" for r in rows),
        "rejected": sum(r["validation"].startswith(bad) for r in rows),
    }


def is_paused(conn, now: int) -> bool:
    until = db.kv_get(conn, "paused_until")
    return until is not None and now < int(until)


def feedback_counts(conn, start: int, end: int, symbols: list[str] | None = None) -> dict:
    """His latest choice per alert/watch read in the window; unanswered ones count too."""
    rows = conn.execute(
        "SELECT r.symbol, (SELECT choice FROM feedback f WHERE f.read_id = r.id "
        "ORDER BY f.id DESC LIMIT 1) AS choice "
        "FROM reads r WHERE r.action IN ('alert', 'watch') AND r.ts_utc >= ? AND r.ts_utc < ?",
        (start, end),
    ).fetchall()
    counts = {"take": 0, "skip": 0, "unsure": 0, "none": 0}
    for r in rows:
        if symbols is None or r["symbol"] in symbols:
            counts[r["choice"] or "none"] += 1
    return counts


def feedback_line(fb: dict) -> str:
    return (
        f"You: {fb['take']} take · {fb['skip']} skip · {fb['unsure']} unsure · "
        f"{fb['none']} unanswered"
    )


def llm_line(settings: Settings, conn, now: int) -> str:
    cfg = settings.config.llm
    spend = db.llm_spend_since(conn, utc_day_start(now))
    return f"${spend:.2f} / ${cfg.daily_budget_usd:.2f}"


def heartbeat_line(hb, now: int) -> str:
    if hb is None:
        return "no heartbeat yet"
    state = "connected" if hb["connected"] else "DISCONNECTED"
    return f"{state} · heartbeat {ago(now - hb['received_at'])} ago"


# --------------------------------------------------------------------------- /status
def status_text(settings: Settings, conn, now: int, started_at: int) -> str:
    cfg = settings.config
    tz = cfg.telegram.display_tz
    snap = gather(settings, conn, now, started_at)
    problems = [c for c in conditions(snap, settings) if c.active]
    icon = "🔴" if any(c.critical for c in problems) else "🟡" if problems else "🟢"
    names = [c.key.split(":")[0].replace("_", " ") for c in problems]
    headline = ", ".join(dict.fromkeys(names)) or "All good"
    lines = [f"{icon} {headline} · {fmt_time(now, tz)} Budapest"]

    hb = snap.hb
    ea = f" · EA {hb['ea_version']}" if hb else ""
    lines.append(f"MT5        {heartbeat_line(hb, now)}{ea}")

    data = []
    for sym in cfg.symbols:
        last = snap.last_m5[sym]
        if last is None:
            data.append(f"{short_name(sym)} M5 –")
        else:
            fresh = now - (last + M5_SEC) <= STALE_SEC
            data.append(f"{short_name(sym)} M5 {fmt_time(last, tz)} {'✓' if fresh else '✗'}")
    lines.append("Data       " + " · ".join(data))

    if hb is not None:
        lines += _account_lines(cfg, conn, now, hb)

    s = reads_summary(conn, local_day_start(now, tz), now + 1)
    paused = "yes" if is_paused(conn, now) else "no"
    lines.append(
        f"Signals    {s['reads']} reads · {s['alert']} alert · {s['watch']} watch · "
        f"paused: {paused}"
    )

    last_call = conn.execute(
        "SELECT ts_utc, status, latency_ms FROM llm_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_call is None:
        call = "no call yet"
    else:
        outcome = "OK" if last_call["status"] == "ok" else "ERROR"
        secs = (last_call["latency_ms"] or 0) / 1000
        call = f"last call {fmt_time(last_call['ts_utc'], tz)} {outcome} ({secs:.1f} s)"
    lines.append(f"LLM        {llm_line(settings, conn, now)} today · {call}")

    nxt = sessions.next_session_start(cfg, now)
    if nxt:
        name, start = nxt
        tz_x = cfg.sessions[name].tz
        lines.append(
            f"Next       {name.upper()} session {fmt_time(start, tz_x, True)} {tz_x.split('/')[-1]}"
        )
    return "\n".join(lines)


def _account_lines(cfg: AppConfig, conn, now: int, hb) -> list[str]:
    pos = f"{hb['positions']} position{'s' if hb['positions'] != 1 else ''}"
    if hb["positions"]:
        pos += f" ({money(hb['floating_pl'], signed=True)})"
    lines = [
        f"Account    balance {money(hb['balance'])} · equity {money(hb['equity'])} "
        f"{hb['currency']} · {pos}"
    ]
    start_balance = day_start_balance(conn, cfg, now, hb)
    closed = "n/a" if start_balance is None else money(hb["balance"] - start_balance, signed=True)
    lines.append(f"Today      {closed} closed · {money(hb['floating_pl'], signed=True)} open")

    f = cfg.ftmo
    daily_limit = f.initial_balance * f.daily_loss_pct / 100
    max_limit = f.initial_balance * f.max_loss_pct / 100
    daily_used = 0.0
    if start_balance is not None:
        daily_used = max(0.0, start_balance - hb["equity"]) / daily_limit * 100
    max_used = max(0.0, f.initial_balance - hb["equity"]) / max_limit * 100
    lines.append(
        f"FTMO       daily loss used {daily_used:.0f}% of {daily_limit:,.0f} · "
        f"max loss used {max_used:.0f}% of {max_limit:,.0f}"
    )
    return lines


# --------------------------------------------------------------------------- brief
def session_window_around(cfg: AppConfig, session: str, now: int) -> tuple[int, int]:
    """The session window that is running now, else the next one."""
    d = sessions.local_date(cfg, session, now)
    for k in range(-1, 15):
        win = sessions.session_window_utc(cfg, session, d + timedelta(days=k))
        if win and win[1] > now:
            return win
    raise ValueError("no session found in the next two weeks")


def _news_epoch(ev) -> int:
    return int(ev.at.replace(tzinfo=ZoneInfo(ev.tz)).timestamp())


def _symbol_brief(settings: Settings, conn, sym: str, session: str, start: int, now: int) -> str:
    cfg = settings.config
    fc = cfg.features
    asof = pd.Timestamp(now, unit="s", tz="UTC")
    h1_bars = db.load_bars(conn, sym, "H1", now, 300)
    d1_bars = db.load_bars(conn, sym, "D1", now, 150)
    h1 = htf.htf_state(h1_bars, htf.H1_SEC, asof, fc.ema_period, fc.swing_confirm_bars)
    d1 = htf.htf_state(d1_bars, htf.D1_SEC, asof, fc.ema_period, fc.swing_confirm_bars)
    name = short_name(sym)
    if cfg.symbols[sym].role != "traded":
        return f"{name}  context only · H1 {h1.state} · D1 {d1.state}"

    alignment = htf.alignment(h1.state, d1.state, cfg.rules.htf_neutral_counts_as_conflict)
    text = f"{name}  H1 {h1.state} · D1 {d1.state} → {alignment.replace('_', ' ')}"

    digits = db.get_digits(conn, sym) or 1
    m5 = db.load_bars(conn, sym, "M5", now, 2000)
    if m5.empty:
        return text
    prev = sessions.previous_trading_day(cfg, session, sessions.local_date(cfg, session, start))
    prev_open = sessions.session_window_utc(cfg, session, prev)[0]
    prev_close = sessions.cash_close_utc(cfg, session, prev)
    t_open, t_close = (pd.Timestamp(t, unit="s", tz="UTC") for t in (prev_open, prev_close))
    prev_day = m5[(m5.index >= t_open) & (m5.index < t_close)]
    if not prev_day.empty:
        text += f" · prev day {prev_day['l'].min():,.{digits}f}–{prev_day['h'].max():,.{digits}f}"
    close_price = features.prior_close(m5, t_close)
    if close_price is not None:
        text += f" · gap {m5['c'].iloc[-1] - close_price:+.{digits}f}"
    return text


def brief_text(settings: Settings, conn, now: int, session: str) -> str:
    cfg = settings.config
    tz = cfg.telegram.display_tz
    sess = cfg.sessions[session]
    start, end = session_window_around(cfg, session, now)
    minutes = (start - now) // 60
    when = f"in {minutes} min" if minutes > 0 else "is running" if now < end else "has ended"
    city = sess.tz.split("/")[-1]
    lines = [f"🌅 {session.upper()} session {when} ({fmt_time(start, sess.tz, True)} {city})"]

    for sym, sym_cfg in cfg.symbols.items():
        if sym_cfg.session == session:
            lines.append(_symbol_brief(settings, conn, sym, session, start, now))

    events = [e for e in cfg.news if start <= _news_epoch(e) <= end]
    news = ", ".join(f"{e.name} {fmt_time(_news_epoch(e), tz)}" for e in events)
    lines.append(f"News: {news or 'none in window'}")
    lines.append(
        f"LLM: {cfg.llm.model} · prompt {cfg.llm.prompt_version} · "
        f"spend today {llm_line(settings, conn, now)}"
    )
    hb = (db.latest_heartbeats(conn, 1) or [None])[0]
    balance = f" · balance {money(hb['balance'])} {hb['currency']}" if hb else ""
    lines.append(f"MT5: {heartbeat_line(hb, now)}{balance}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- /today, wrap, daily
def today_text(settings: Settings, conn, now: int) -> str:
    cfg = settings.config
    tz = cfg.telegram.display_tz
    start = local_day_start(now, tz)
    s = reads_summary(conn, start, now + 1)
    lines = [
        f"📅 Today · {s['reads']} reads · {s['alert']} alert · {s['watch']} watch · "
        f"{s['rejected']} rejected/failed"
    ]
    rows = conn.execute(
        "SELECT r.*, (SELECT choice FROM feedback f WHERE f.read_id = r.id "
        "ORDER BY f.id DESC LIMIT 1) AS choice "
        "FROM reads r WHERE r.action IN ('alert', 'watch') AND r.ts_utc >= ? ORDER BY r.id",
        (start,),
    ).fetchall()
    for r in rows:
        setup = json.loads(r["setup"])
        lines.append(
            f" • {fmt_time(r['bar_time_utc'], tz)} {short_name(r['symbol'])} "
            f"{setup['direction'].upper()} {setup['type']} {setup['grade']} "
            f"({r['action']}) → {r['choice'] or 'no answer'}"
        )
    lines.append(feedback_line(feedback_counts(conn, start, now + 1)))
    lines.append("Hypothetical results: coming with the outcome simulator")
    lines.append(f"LLM spend {llm_line(settings, conn, now)}")
    return "\n".join(lines)


def wrap_text(settings: Settings, conn, session: str, start: int, end: int) -> str:
    symbols = [s for s, c in settings.config.symbols.items() if c.session == session]
    rows = conn.execute(
        "SELECT symbol, action FROM reads WHERE bar_time_utc >= ? AND bar_time_utc < ?",
        (start, end),
    ).fetchall()
    rows = [r for r in rows if r["symbol"] in symbols]
    n_alert = sum(r["action"] == "alert" for r in rows)
    n_watch = sum(r["action"] == "watch" for r in rows)
    fb = feedback_counts(conn, start, end + 600, symbols)
    return "\n".join(
        [
            f"🏁 {session.upper()} session over · {len(rows)} reads · {n_alert} alert · "
            f"{n_watch} watch",
            feedback_line(fb),
            "Hypothetical R so far: coming with the outcome simulator",
        ]
    )


def daily_report_text(settings: Settings, conn, now: int) -> str:
    tz = settings.config.reports.tz
    start = local_day_start(now, tz)
    s = reads_summary(conn, start, now + 1)
    fb = feedback_counts(conn, start, now + 1)
    llm = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM llm_calls WHERE ts_utc >= ?",
        (start,),
    ).fetchone()
    day = datetime.fromtimestamp(now, tz=ZoneInfo(tz))
    return "\n".join(
        [
            f"📊 Daily report {day:%a %d %b}",
            f"Reads {s['reads']} · alerts {s['alert']} · watch {s['watch']} · "
            f"rejected/failed {s['rejected']}",
            feedback_line(fb),
            "Hypothetical results: coming with the outcome simulator",
            f"LLM {llm['n']} calls · ${llm['cost']:.2f}",
        ]
    )
