"""Hypothetical outcome simulator. Everything here is SIMULATED, not a real trade result.

For every alert/watch read it replays the bars that followed the signal bar (M5 live; M5 or the
finer M1 bars in backtests):

  1. ENTRY    The entry is a stop order. It triggers when price trades through the entry within
              the next ENTRY_WINDOW_BARS (3) M5 bars, i.e. before signal open + 4 x 5 min:
              high >= entry for a long, low <= entry for a short. No trigger in that window ->
              `no_entry` (not counted in R). The fill is the entry price itself; spread is
              handled by the backtester as a cost in R, slippage is not modelled.
  2. EXIT     From the entry bar onwards, whichever of stop or target is hit first. Bars only
              give high and low, so when one bar touches BOTH, the trade counts as a LOSS.
              That includes the entry bar: if it triggers the entry and also reaches the stop,
              it is a loss, because the order of events inside the bar is unknown. With M1
              bars this ambiguity is five times rarer.
  3. RESULT   win = +reward/risk (R), loss = -1 R.
  4. TIMEOUT  Still open at the cash close of that session day: `expired`, marked to the last
              close (R can be positive or negative).
  5. PENDING  Not enough bars yet to decide; it is re-simulated on the next run.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app import db, sessions
from app.config import AppConfig

ENTRY_WINDOW_BARS = 3
M5_SEC = 300
GIVE_UP_AFTER_SEC = 600  # missing bars: decide anyway this long after the horizon


@dataclass(frozen=True)
class Outcome:
    status: str  # pending | no_entry | win | loss | expired
    r: float | None = None
    entry_t: int | None = None
    exit_t: int | None = None


def simulate(
    setup: dict,
    signal_open: int,
    bars: pd.DataFrame,
    horizon: int,
    now: int,
    bar_seconds: int = M5_SEC,
) -> Outcome:
    """Replay `bars` (closed bars of `bar_seconds` length, UTC index) for one M5 signal.
    Pure function, no database.

    `signal_open`: open time of the M5 signal bar. `horizon`: UTC time when an open trade
    expires. Bars inside the signal bar itself (M1 bars before signal_open + 5 min) are ignored.
    """
    long = setup["direction"] == "long"
    entry, stop, target = setup["entry"], setup["stop"], setup["target"]
    risk = abs(entry - stop)
    reward_r = abs(target - entry) / risk
    first_open = signal_open + M5_SEC  # the signal bar has closed here
    entry_deadline = first_open + ENTRY_WINDOW_BARS * M5_SEC  # end of the 3rd M5 bar

    entered_at: int | None = None
    last_open: int | None = None
    last_close = entry
    for ts, bar in bars.iterrows():
        t = int(ts.timestamp())
        if t < first_open or t >= horizon:
            continue
        last_open, last_close = t, float(bar["c"])
        if entered_at is None:
            if t >= entry_deadline:
                return Outcome("no_entry")
            triggered = bar["h"] >= entry if long else bar["l"] <= entry
            if not triggered:
                continue
            entered_at = t
        hit_stop = bar["l"] <= stop if long else bar["h"] >= stop
        hit_target = bar["h"] >= target if long else bar["l"] <= target
        if hit_stop:  # includes "both in the same bar": counted as a loss
            return Outcome("loss", -1.0, entered_at, t)
        if hit_target:
            return Outcome("win", round(reward_r, 2), entered_at, t)

    data_complete = (last_open is not None and last_open + bar_seconds >= horizon) or (
        now >= horizon + GIVE_UP_AFTER_SEC
    )
    if entered_at is None:
        window_over = last_open is not None and last_open + bar_seconds >= entry_deadline
        return Outcome("no_entry") if (window_over or data_complete) else Outcome("pending")
    if not data_complete:
        return Outcome("pending", entry_t=entered_at)
    move = (last_close - entry) if long else (entry - last_close)
    return Outcome("expired", round(move / risk, 2), entered_at, last_open)


# --------------------------------------------------------------------------- database side
def _horizon(cfg: AppConfig, session: str, signal_open: int) -> int:
    return sessions.cash_close_utc(cfg, session, sessions.local_date(cfg, session, signal_open))


def update_outcomes(conn: sqlite3.Connection, cfg: AppConfig, now: int | None = None) -> int:
    """Simulate every alert/watch that has no final outcome yet. Returns how many changed."""
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT r.id, r.symbol, r.session, r.bar_time_utc, r.setup FROM reads r "
        "LEFT JOIN outcomes o ON o.read_id = r.id "
        "WHERE r.action IN ('alert', 'watch') AND (o.read_id IS NULL OR o.status = 'pending')"
    ).fetchall()
    changed = 0
    for r in rows:
        horizon = _horizon(cfg, r["session"], r["bar_time_utc"])
        bars = db.load_bars_between(conn, r["symbol"], "M5", r["bar_time_utc"], horizon)
        out = simulate(json.loads(r["setup"]), r["bar_time_utc"], bars, horizon, now)
        with conn:
            conn.execute(
                "INSERT INTO outcomes (read_id, status, r, entry_t, exit_t, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (read_id) DO UPDATE SET "
                "status = excluded.status, r = excluded.r, entry_t = excluded.entry_t, "
                "exit_t = excluded.exit_t, updated_at = excluded.updated_at",
                (r["id"], out.status, out.r, out.entry_t, out.exit_t, now),
            )
        changed += 1
    return changed


def summarize(
    conn: sqlite3.Connection,
    start: int,
    end: int,
    symbols: list[str] | None = None,
    action: str = "alert",
    only_taken: bool = False,
) -> dict:
    """Counts and total R of the simulated outcomes of reads made in [start, end).

    `action`: which reads ('alert' or 'watch'). `only_taken`: only those he answered with
    "I'd take it" (his latest answer counts).
    """
    rows = conn.execute(
        "SELECT r.symbol, o.status, o.r, (SELECT choice FROM feedback f "
        "WHERE f.read_id = r.id ORDER BY f.id DESC LIMIT 1) AS choice "
        "FROM reads r LEFT JOIN outcomes o ON o.read_id = r.id "
        "WHERE r.action = ? AND r.ts_utc >= ? AND r.ts_utc < ?",
        (action, start, end),
    ).fetchall()
    out = {"n": 0, "win": 0, "loss": 0, "expired": 0, "no_entry": 0, "pending": 0, "r": 0.0}
    for r in rows:
        if symbols is not None and r["symbol"] not in symbols:
            continue
        if only_taken and r["choice"] != "take":
            continue
        out["n"] += 1
        out[r["status"] or "pending"] += 1
        out["r"] += r["r"] or 0.0
    out["r"] = round(out["r"], 2)
    return out


def summary_line(label: str, s: dict) -> str:
    """One line for the reports. Always says that the numbers are simulated."""
    return (
        f"Simulated {label}: {s['n']} → {s['win']} win · {s['loss']} loss · "
        f"{s['expired']} expired · {s['no_entry']} no entry · {s['pending']} pending · "
        f"{s['r']:+.1f}R"
    )


# --------------------------------------------------------------------------- CSV export
CSV_COLUMNS = [
    "read_id", "bar_time_utc", "symbol", "session", "model", "prompt_version", "htf_alignment",
    "day_type_hint", "model_action", "action", "grade", "direction", "type", "with_trend",
    "entry", "stop", "target", "atr", "validation", "reason", "choice", "choice_time_utc",
    "sim_status", "sim_r", "sim_entry_time_utc", "sim_exit_time_utc",
]  # fmt: skip


def _iso(epoch: int | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def export_rows(conn: sqlite3.Connection, start: int, end: int) -> list[dict]:
    """reads + his feedback + simulated outcomes for reads made in [start, end)."""
    rows = conn.execute(
        "SELECT r.*, o.status AS sim_status, o.r AS sim_r, o.entry_t, o.exit_t, "
        "f.choice AS choice, f.ts_utc AS choice_ts "
        "FROM reads r LEFT JOIN outcomes o ON o.read_id = r.id "
        "LEFT JOIN feedback f ON f.id = (SELECT MAX(id) FROM feedback WHERE read_id = r.id) "
        "WHERE r.ts_utc >= ? AND r.ts_utc < ? ORDER BY r.id",
        (start, end),
    ).fetchall()
    out = []
    for r in rows:
        setup = json.loads(r["setup"]) if r["setup"] else {}
        out.append({
            "read_id": r["id"], "bar_time_utc": _iso(r["bar_time_utc"]), "symbol": r["symbol"],
            "session": r["session"], "model": r["model"], "prompt_version": r["prompt_version"],
            "htf_alignment": r["htf_alignment"], "day_type_hint": r["day_type_hint"],
            "model_action": r["model_action"] or "", "action": r["action"],
            "grade": r["grade"] or "", "direction": setup.get("direction", ""),
            "type": setup.get("type", ""), "with_trend": setup.get("with_trend", ""),
            "entry": setup.get("entry", ""), "stop": setup.get("stop", ""),
            "target": setup.get("target", ""), "atr": r["atr"] if r["atr"] is not None else "",
            "validation": r["validation"], "reason": r["reason"] or "",
            "choice": r["choice"] or "", "choice_time_utc": _iso(r["choice_ts"]),
            "sim_status": r["sim_status"] or "", "sim_r": "" if r["sim_r"] is None else r["sim_r"],
            "sim_entry_time_utc": _iso(r["entry_t"]), "sim_exit_time_utc": _iso(r["exit_t"]),
        })  # fmt: skip
    return out


def write_csv(conn: sqlite3.Connection, start: int, end: int, path) -> int:
    """Write the export to `path`; returns the number of rows."""
    import csv

    rows = export_rows(conn, start, end)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
