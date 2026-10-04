"""Health, risk and ops monitors, plus the texts for /status, /today, briefs and reports.

Monitors are split in two so they are easy to test:
  conditions(snapshot)   pure: what is wrong right now
  MonitorEngine.run()    the de-dup state machine: announce a problem ONCE, and ONCE when it
                         clears (state lives in the `kv` table, so a restart does not repeat it)
"""

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app import db, sessions
from app.config import AppConfig, Settings
from app.llm import utc_day_start
from app.messages import money, short_name
from app.timeconv import offset_matches

M5_SEC = 300
STALE_SEC = 2 * M5_SEC


@dataclass
class Out:
    """A message the monitors want sent."""

    text: str
    silent: bool = False


# --------------------------------------------------------------------------- time helpers
def local_day_start(now: int, tz: str) -> int:
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(now, tz=z).date()
    return int(datetime.combine(d, time(0), tzinfo=z).timestamp())


def in_quiet_hours(cfg: AppConfig, now: int) -> bool:
    q = cfg.telegram.quiet_hours
    t = datetime.fromtimestamp(now, tz=ZoneInfo(cfg.telegram.display_tz)).time()
    return (q.start <= t or t < q.end) if q.start > q.end else (q.start <= t < q.end)


def ago(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h"


# --------------------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    now: int
    started_at: int
    hb: sqlite3.Row | None
    hb_prev: sqlite3.Row | None
    session: tuple[str, int, int] | None  # active session now
    last_m5: dict[str, int | None]  # open time of the newest M5 bar, per symbol
    budget_exceeded: bool
    spend_today: float
    llm_recent: list[sqlite3.Row] = field(default_factory=list)  # newest first
    disk_free_pct: float = 100.0


def gather(settings: Settings, conn: sqlite3.Connection, now: int, started_at: int) -> Snapshot:
    cfg = settings.config
    hbs = db.latest_heartbeats(conn, 2)
    last_m5 = {}
    for sym in cfg.symbols:
        row = conn.execute(
            "SELECT MAX(t_utc) AS t FROM bars WHERE symbol = ? AND tf = 'M5'", (sym,)
        ).fetchone()
        last_m5[sym] = row["t"]
    day = utc_day_start(now)
    spend = db.llm_spend_since(conn, day)
    usage = shutil.disk_usage(Path(settings.db_path).resolve().parent)
    return Snapshot(
        now=now,
        started_at=started_at,
        hb=hbs[0] if hbs else None,
        hb_prev=hbs[1] if len(hbs) > 1 else None,
        session=sessions.active_session(cfg, now),
        last_m5=last_m5,
        budget_exceeded=db.event_exists_since(conn, "llm_budget_exceeded", day),
        spend_today=spend,
        llm_recent=conn.execute(
            "SELECT status, validation, error FROM llm_calls WHERE purpose = 'market_read' "
            "ORDER BY id DESC LIMIT 3"
        ).fetchall(),
        disk_free_pct=usage.free / usage.total * 100,
    )


# --------------------------------------------------------------------------- conditions
@dataclass
class Cond:
    key: str
    active: bool | None  # None = cannot judge now (e.g. outside a session): state unchanged
    on_text: str
    off_text: str
    critical: bool = False  # 🔴 problems turn the /status header red


def conditions(snap: Snapshot, settings: Settings) -> list[Cond]:
    cfg, mon = settings.config, settings.config.monitors
    out: list[Cond] = []

    # MT5 not reporting. The age counts from service start too, so a fresh start does not
    # alert instantly.
    timeout = mon.heartbeat_timeout_session_sec if snap.session else mon.heartbeat_timeout_other_sec
    last_hb = snap.hb["received_at"] if snap.hb else 0
    silent_for = snap.now - max(last_hb, snap.started_at)
    out.append(
        Cond(
            "mt5_silent",
            silent_for > timeout,
            f"🔴 MT5 not reporting (no heartbeat for {ago(silent_for)})",
            "🟢 MT5 is reporting again",
            critical=True,
        )
    )

    # Two heartbeats in a row say "not connected".
    has_two = snap.hb is not None and snap.hb_prev is not None
    disconnected = has_two and not snap.hb["connected"] and not snap.hb_prev["connected"]
    out.append(
        Cond(
            "mt5_disconnected",
            disconnected,
            "🔴 MT5 disconnected from the broker",
            "🟢 MT5 is connected to the broker again",
            critical=True,
        )
    )

    # Newest M5 bar of each traded symbol, judged only while its own session is open.
    for sym, sym_cfg in cfg.symbols.items():
        if sym_cfg.role != "traded":
            continue
        if snap.session is None or snap.session[0] != sym_cfg.session:
            stale = None
        else:
            last = snap.last_m5[sym]
            closed_at = last + M5_SEC if last is not None else max(snap.started_at, snap.session[1])
            stale = snap.now - closed_at > STALE_SEC
        out.append(
            Cond(
                f"stale:{sym}",
                stale,
                f"🟡 Data stale for {short_name(sym)} (no new M5 bar)",
                f"🟢 Data for {short_name(sym)} is flowing again",
                critical=True,
            )
        )

    if snap.hb is not None:
        reported = snap.hb["server_utc_offset_sec"]
        ok = offset_matches(reported, snap.hb["time_server"], cfg.server_time_mode)
        out.append(
            Cond(
                "time_mismatch",
                not ok,
                f"🟡 Time check mismatch: the EA reports a UTC offset of {reported} s, which "
                f"server_time_mode={cfg.server_time_mode} does not give. Bar times may be wrong.",
                "🟢 Time check OK again",
            )
        )

    if snap.hb is not None:
        # Phase 1 never trades: MT5 runs with algo trading off (deploy/mt5/startup.ini.example).
        out.append(
            Cond(
                "algo_trading_on",
                bool(snap.hb["trade_allowed"]),
                "🔴 Algo trading is switched ON in MT5. Phase 1 expects it off; check the "
                "terminal (/screenshot) and the start config.",
                "🟢 Algo trading is off in MT5 again",
                critical=True,
            )
        )

    out.append(
        Cond(
            "llm_budget",
            snap.budget_exceeded,
            f"🟡 LLM paused for today (budget ${cfg.llm.daily_budget_usd:.2f} used)",
            "🟢 LLM budget available again",
        )
    )

    # The last three calls all failed or were rejected by the validator.
    if len(snap.llm_recent) >= 3:
        bad = [
            r
            for r in snap.llm_recent
            if r["status"] != "ok" or (r["validation"] or "").startswith("rejected")
        ]
        last_problem = (bad[0]["error"] or bad[0]["validation"]) if bad else ""
        out.append(
            Cond(
                "llm_failing",
                len(bad) == 3,
                f"🟡 LLM failing (last: {last_problem})",
                "🟢 LLM answers are fine again",
            )
        )

    out.append(
        Cond(
            "disk_low",
            snap.disk_free_pct < mon.disk_min_free_pct,
            f"🟡 Disk low ({snap.disk_free_pct:.0f}% free)",
            "🟢 Disk space OK again",
        )
    )
    return out


# --------------------------------------------------------------------------- engine
class MonitorEngine:
    def __init__(self, settings: Settings, started_at: int):
        self.s = settings
        self.started_at = started_at

    def run(self, conn: sqlite3.Connection, now: int) -> list[Out]:
        cfg = self.s.config
        snap = gather(self.s, conn, now, self.started_at)
        positions_open = bool(snap.hb and snap.hb["positions"] > 0)
        quiet = in_quiet_hours(cfg, now) and not positions_open
        msgs: list[Out] = []
        for c in conditions(snap, self.s):
            if c.active is None:
                continue
            key, state = f"mon:{c.key}", db.kv_get(conn, f"mon:{c.key}") == "1"
            if c.active and not state:
                db.kv_set(conn, key, "1")
                msgs.append(Out(c.on_text, silent=quiet))
            elif not c.active and state:
                db.kv_set(conn, key, "0")
                msgs.append(Out(c.off_text, silent=True))
        if snap.hb is not None:
            msgs += self._position_change(conn, snap.hb)
            msgs += self._risk(conn, now, snap.hb)
        return msgs

    # ---- positions he opened/closed by hand
    def _position_change(self, conn, hb) -> list[Out]:
        sign = (hb["floating_pl"] > 0) - (hb["floating_pl"] < 0) if hb["positions"] else 0
        now_state = {"positions": hb["positions"], "sign": sign}
        prev = db.kv_get(conn, "pos_seen")
        db.kv_set(conn, "pos_seen", json.dumps(now_state))
        if prev is None:
            return []
        p = json.loads(prev)
        changed = p["positions"] != hb["positions"] or (
            hb["positions"] and p["sign"] and p["sign"] != sign
        )
        if not changed:
            return []
        pl = money(hb["floating_pl"], signed=True)
        return [Out(f"📌 Position change: {hb['positions']} open, floating {pl} {hb['currency']}")]

    # ---- FTMO risk (approximate; FTMO's own dashboard is authoritative)
    def _risk(self, conn, now: int, hb) -> list[Out]:
        cfg = self.s.config
        f = cfg.ftmo
        msgs: list[Out] = []
        day_start = self._day_start_balance(conn, now, hb)
        for kind, base, limit_pct, label in (
            ("daily", day_start, f.daily_loss_pct, "daily loss"),
            ("max", f.initial_balance, f.max_loss_pct, "max loss"),
        ):
            if base is None:
                continue
            limit = f.initial_balance * limit_pct / 100
            used = max(0.0, base - hb["equity"]) / limit * 100
            day = datetime.fromtimestamp(now, tz=ZoneInfo(f.day_reset_tz)).date().isoformat()
            crossed = [lv for lv in sorted(f.warn_levels_pct) if used >= lv]
            if not crossed:
                continue
            fresh = [db.kv_once(conn, f"risk:{kind}:{lv}:{day}") for lv in crossed]
            if fresh[-1]:  # announce only the highest level, once
                lost = money(base - hb["equity"])
                msgs.append(
                    Out(
                        f"⚠️ FTMO {label} at {used:.0f}% of the limit "
                        f"({lost} of {money(limit)}). Approximate."
                    )
                )
        if hb["positions"] > 0:
            for ev in cfg.news:
                ev_utc = int(ev.at.replace(tzinfo=ZoneInfo(ev.tz)).timestamp())
                if 0 <= ev_utc - now <= 600 and db.kv_once(conn, f"risk:news:{ev_utc}"):
                    minutes = -(-(ev_utc - now) // 60)  # round up
                    msgs.append(
                        Out(f"⚠️ Position open and high-impact news in {minutes} min: {ev.name}")
                    )
            berlin = datetime.fromtimestamp(now, tz=ZoneInfo("Europe/Berlin"))
            if berlin.weekday() == 4 and berlin.time() >= time(21, 45):
                if db.kv_once(conn, f"risk:friday:{berlin.date()}"):
                    msgs.append(Out("⚠️ Friday 21:45 Berlin and a position is still open."))
        return msgs

    def _day_start_balance(self, conn, now: int, hb) -> float | None:
        return day_start_balance(conn, self.s.config, now, hb)


def day_start_balance(conn, cfg: AppConfig, now: int, hb) -> float | None:
    """Balance at the start of the FTMO day (midnight Europe/Prague), taken from the first
    heartbeat received after that midnight and then kept for the rest of the day."""
    z = ZoneInfo(cfg.ftmo.day_reset_tz)
    today = datetime.fromtimestamp(now, tz=z).date()
    stored = db.kv_get(conn, "ftmo_day")
    if stored:
        s = json.loads(stored)
        if s["day"] == today.isoformat():
            return s["balance"]
    midnight = int(datetime.combine(today, time(0), tzinfo=z).timestamp())
    if hb is None or hb["received_at"] < midnight:
        return None  # nothing received since midnight yet
    db.kv_set(conn, "ftmo_day", json.dumps({"day": today.isoformat(), "balance": hb["balance"]}))
    return hb["balance"]
