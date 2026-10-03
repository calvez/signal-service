"""The Telegram side of the service: command handlers, sending validated reads, monitors,
briefs, session wraps and the daily report.

`TelegramService.tick(now)` does everything that is due and is called every few seconds by a
small thread (and directly by the tests with a fake clock). Messages that must go out only
once (a brief, a wrap, the daily report) claim a key in the `kv` table first.
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app import charts, db, outcomes, reports, sessions
from app.config import Settings
from app.messages import alert_text, feedback_buttons, fmt_time
from app.status import STALE_SEC, MonitorEngine, Out, in_quiet_hours
from app.telegram import Bot, Reply, TelegramApi, take_screenshot

log = logging.getLogger("signal.scheduler")

TICK_SEC = 5
MONITOR_EVERY_SEC = 30
WRAP_GRACE_SEC = 30 * 60  # still send the wrap this long after the session ended
DAILY_GRACE_SEC = 2 * 3600
OUTCOMES_EVERY_SEC = 60
EXPORT_WEEKDAY, EXPORT_HOUR = 5, 10  # Saturday 10:00 Berlin: all outcomes are settled
M5_SEC = 300
TIMEFRAMES = {"M5": 300, "H1": 3600, "D1": 86400}

HELP = """Commands (none of them can trade):
/status – health and account at a glance
/today – today's reads, alerts and your answers
/brief [eu|us] – pre-session brief
/chart <SYMBOL> [M5|H1|D1] – chart, e.g. /chart GER40
/screenshot – picture of the MT5 virtual screen
/pause [minutes] – mute trade alerts and watches (ops and risk alerts keep coming)
/resume – unmute
/help – this list"""


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=5, check=False,
        )  # fmt: skip
        return out.stdout.strip() or "n/a"
    except (OSError, subprocess.TimeoutExpired):
        return "n/a"


class TelegramService:
    def __init__(self, settings: Settings, api: TelegramApi, started_at: int | None = None):
        self.s = settings
        self.cfg = settings.config
        self.api = api
        self.chat_id = settings.secrets.telegram_chat_id
        self.started_at = int(time.time()) if started_at is None else started_at
        self.engine = MonitorEngine(settings, self.started_at)
        self.bot = Bot(settings, api, self.commands())
        self._last_monitor = 0
        self._last_outcomes = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ helpers
    def _conn(self):
        return db.connect(self.s.db_path)

    def _send(self, text: str, silent: bool = True) -> None:
        self.api.send_message(self.chat_id, text, silent=silent)

    def _with_conn(self, fn, *args):
        conn = self._conn()
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    # ------------------------------------------------------------------ commands
    def commands(self) -> dict:
        return {
            "/help": lambda a: Reply(HELP),
            "/start": lambda a: Reply(HELP),
            "/status": self.cmd_status,
            "/today": self.cmd_today,
            "/brief": self.cmd_brief,
            "/chart": self.cmd_chart,
            "/screenshot": self.cmd_screenshot,
            "/pause": self.cmd_pause,
            "/resume": self.cmd_resume,
            "/restart_mt5": lambda a: Reply(
                "/restart_mt5 is not enabled yet. It needs a narrow sudoers rule that Lorant "
                "has to approve first."
            ),
        }

    def cmd_status(self, args) -> Reply:
        text = self._with_conn(
            lambda c: reports.status_text(self.s, c, int(time.time()), self.started_at)
        )
        return Reply(text)

    def cmd_today(self, args) -> Reply:
        return Reply(self._with_conn(lambda c: reports.today_text(self.s, c, int(time.time()))))

    def cmd_brief(self, args) -> Reply:
        now = int(time.time())
        name = args[0].lower() if args else None
        if name is None:
            active = sessions.active_session(self.cfg, now)
            nxt = sessions.next_session_start(self.cfg, now)
            name = active[0] if active else (nxt[0] if nxt else "eu")
        if name not in self.cfg.sessions:
            return Reply(f"Unknown session. Use one of: {', '.join(self.cfg.sessions)}")
        return Reply(self._with_conn(lambda c: reports.brief_text(self.s, c, now, name)))

    def resolve_symbol(self, text: str) -> str | None:
        t = text.upper()
        for sym, c in self.cfg.symbols.items():
            if t in (sym.upper(), sym.split(".")[0].upper(), c.name.upper()):
                return sym
        return None

    def cmd_chart(self, args) -> Reply:
        if not args:
            return Reply("Usage: /chart <SYMBOL> [M5|H1|D1], e.g. /chart GER40")
        sym = self.resolve_symbol(args[0])
        tf = args[1].upper() if len(args) > 1 else "M5"
        if sym is None:
            names = ", ".join(s.split(".")[0] for s in self.cfg.symbols)
            return Reply(f"Unknown symbol. Try one of: {names}")
        if tf not in TIMEFRAMES:
            return Reply("Timeframe must be M5, H1 or D1.")
        png = self._with_conn(self.render_symbol_chart, sym, tf, int(time.time()), None)
        if png is None:
            return Reply(f"No {tf} bars for {sym} yet.")
        return Reply(f"{sym} {tf}", png=png)

    def render_symbol_chart(self, conn, sym: str, tf: str, until: int, setup: dict | None):
        """PNG of `sym` (None when there are no bars). Used by /chart and by alerts."""
        cc = self.cfg.telegram.chart
        df = db.load_bars(conn, sym, tf, until, cc.bars + 120)
        if df.empty:
            return None
        start = None
        if tf == "M5":
            active = sessions.active_session(self.cfg, int(df.index[-1].timestamp()))
            if active:
                start = pd.Timestamp(active[1], unit="s", tz="UTC")
        return charts.render_chart(
            df, sym, tf, self.cfg.telegram.display_tz, setup, start,
            self.cfg.features.opening_range_bars, cc.bars, cc.width, cc.height,
            self.cfg.features.ema_period, db.get_digits(conn, sym) or 1,
        )  # fmt: skip

    def cmd_screenshot(self, args) -> Reply:
        png = take_screenshot()
        if png is None:
            return Reply("No screenshot: the MT5 display (:99) is not available here.")
        return Reply("MT5 virtual display", png=png)

    def cmd_pause(self, args) -> Reply:
        now = int(time.time())
        if args and args[0].isdigit():
            until = now + int(args[0]) * 60
        else:
            nxt = sessions.next_session_start(self.cfg, now)
            until = nxt[1] if nxt else now + 12 * 3600
        self._with_conn(lambda c: db.kv_set(c, "paused_until", str(until)))
        tz = self.cfg.telegram.display_tz
        return Reply(
            f"⏸ Trade alerts and watches muted until {fmt_time(until, tz, True)} Budapest. "
            "Ops and risk alerts keep coming. /resume to unmute."
        )

    def cmd_resume(self, args) -> Reply:
        self._with_conn(lambda c: db.kv_delete(c, "paused_until"))
        return Reply("▶️ Unmuted.")

    # ------------------------------------------------------------------ sending reads
    def send_pending_reads(self, now: int) -> int:
        """Send validated alerts/watches that have not been announced yet. Returns how many."""
        conn = self._conn()
        sent = 0
        try:
            rows = conn.execute(
                "SELECT * FROM reads WHERE notified_at IS NULL AND action IN ('alert', 'watch') "
                "ORDER BY id"
            ).fetchall()
            for r in rows:
                with conn:  # claim it, so nothing is ever sent twice
                    claimed = conn.execute(
                        "UPDATE reads SET notified_at = ? WHERE id = ? AND notified_at IS NULL",
                        (now, r["id"]),
                    ).rowcount
                if not claimed:
                    continue
                if now - (r["bar_time_utc"] + M5_SEC) > STALE_SEC:  # fail closed on old data
                    db.log_event(conn, "alert_dropped", {"read": r["id"], "why": "stale"})
                elif reports.is_paused(conn, now):
                    db.log_event(conn, "alert_muted", {"read": r["id"]})
                else:
                    sent += self._send_read(conn, r)
        finally:
            conn.close()
        return sent

    def _send_read(self, conn, r) -> int:
        digits = db.get_digits(conn, r["symbol"]) or 1
        text = alert_text(r, self.cfg, digits)
        markup = feedback_buttons(r["id"])
        silent = not r["push"]
        png = None
        try:
            png = self.render_symbol_chart(
                conn, r["symbol"], "M5", r["bar_time_utc"], json.loads(r["setup"])
            )
        except Exception:
            log.exception("chart failed for read %s", r["id"])
        if png:
            msg_id = self.api.send_photo(self.chat_id, png, text, silent, markup)
        else:
            msg_id = self.api.send_message(self.chat_id, text, silent, markup)
        if msg_id is None:
            db.log_event(conn, "alert_send_failed", {"read": r["id"]})
            return 0
        return 1

    # ------------------------------------------------------------------ scheduled messages
    def due_messages(self, conn, now: int) -> list[Out]:
        out: list[Out] = []
        for name, sess in self.cfg.sessions.items():
            d = sessions.local_date(self.cfg, name, now)
            win = sessions.session_window_utc(self.cfg, name, d)
            if not win:
                continue
            start, end = win
            brief_at = int(datetime.combine(d, sess.brief_at, tzinfo=ZoneInfo(sess.tz)).timestamp())
            if brief_at <= now < start and db.kv_once(conn, f"brief:{name}:{d}"):
                out.append(Out(reports.brief_text(self.s, conn, now, name)))
            if end <= now < end + WRAP_GRACE_SEC and db.kv_once(conn, f"wrap:{name}:{d}"):
                out.append(Out(reports.wrap_text(self.s, conn, name, start, end)))
        out += self._daily_report(conn, now)
        return out

    def weekly_export(self, conn, now: int) -> tuple[str, bytes] | None:
        """(filename, CSV bytes) of the past week's reads + feedback + outcomes, once a week."""
        z = ZoneInfo(self.cfg.reports.tz)
        local = datetime.fromtimestamp(now, tz=z)
        if local.weekday() != EXPORT_WEEKDAY or local.hour != EXPORT_HOUR:
            return None
        year, week, _ = local.isocalendar()
        if not db.kv_once(conn, f"export:{year}-W{week:02d}"):
            return None
        monday = datetime.combine(
            (local - timedelta(days=local.weekday())).date(), clock_time(0), tzinfo=z
        )
        name = f"reads_{year}-W{week:02d}.csv"
        folder = Path(self.s.db_path).resolve().parent / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        outcomes.update_outcomes(conn, self.cfg, now)
        outcomes.write_csv(conn, int(monday.timestamp()), now, folder / name)
        return name, (folder / name).read_bytes()

    def _daily_report(self, conn, now: int) -> list[Out]:
        rep = self.cfg.reports
        z = ZoneInfo(rep.tz)
        today = datetime.fromtimestamp(now, tz=z).date()
        at = int(datetime.combine(today, rep.daily_at, tzinfo=z).timestamp())
        if not (at <= now < at + DAILY_GRACE_SEC):
            return []
        if not any(sessions.is_trading_day(self.cfg, n, today) for n in self.cfg.sessions):
            return []
        if not db.kv_once(conn, f"daily:{today}"):
            return []
        return [Out(reports.daily_report_text(self.s, conn, now))]

    # ------------------------------------------------------------------ the tick
    def tick(self, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        self.send_pending_reads(now)
        conn = self._conn()
        try:
            if now - self._last_outcomes >= OUTCOMES_EVERY_SEC:
                self._last_outcomes = now
                outcomes.update_outcomes(conn, self.cfg, now)
            msgs = self.due_messages(conn, now)
            export = self.weekly_export(conn, now)
            if now - self._last_monitor >= MONITOR_EVERY_SEC:
                self._last_monitor = now
                msgs += self.engine.run(conn, now)
        finally:
            conn.close()
        for m in msgs:
            self._send(m.text, m.silent)
        if export:
            self.api.send_document(
                self.chat_id, export[0], export[1], "Weekly export (simulated outcomes)"
            )

    def announce_start(self) -> None:
        conn = self._conn()
        try:
            hb = (db.latest_heartbeats(conn, 1) or [None])[0]
        finally:
            conn.close()
        quiet = in_quiet_hours(self.cfg, int(time.time())) and not (hb and hb["positions"] > 0)
        self._send(f"🟢 signal-service started (v0.1.0, commit {git_commit()})", silent=quiet)

    # ------------------------------------------------------------------ threads
    def _ticker(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick crashed")
            self._stop.wait(TICK_SEC)

    def start(self) -> None:
        self.announce_start()
        for target, name in ((self.bot.run, "telegram-poll"), (self._ticker, "telegram-tick")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self.bot.stop()
        self.api.close()
