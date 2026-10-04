"""File spool: how the MT5 EA hands bars and heartbeats to the service.

Why files and not HTTP: MT5 only lets an EA call WebRequest for URLs entered by hand under
Tools > Options (stored encrypted), and every start with a start config resets that list. So the
EA (mt5/BarPusher.mq5) writes each payload, in exactly the JSON format of docs/protocol.md §1-2,
as a file into a folder that only the users `mt5` (writes) and `signal` (reads) can access:

    /var/spool/signal-mt5/bars_<time>_<n>.json   one POST /v1/bars body
    /var/spool/signal-mt5/hb_<time>_<n>.json     one POST /v1/heartbeat body

The EA writes `<name>.tmp` first and renames it, so a `.json` file is always complete.
Files are processed oldest name first with the same validation as the HTTP endpoints, then
deleted. A file that fails validation is moved to `rejected/` and logged (fail closed: nothing
from it is stored, and nothing is lost silently).
"""

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from app import db
from app.config import Settings
from app.ingest import UnknownSymbol, read_candidate, store_bars, store_heartbeat
from app.models import BarsPayload, HeartbeatPayload

log = logging.getLogger("signal.spool")

POLL_SEC = 2.0
MAX_FILES_PER_ROUND = 500  # a backfill writes ~30 files; this only bounds a long outage


class SpoolWatcher:
    def __init__(
        self,
        settings: Settings,
        spool_dir: str | Path,
        on_new_bar: Callable[[str, int], None] | None = None,
    ):
        self.s = settings
        self.dir = Path(spool_dir)
        self.rejected = self.dir / "rejected"
        self.on_new_bar = on_new_bar  # called with (symbol, bar open time UTC) for market reads
        self._stop = threading.Event()

    def _reject(self, path: Path, why: str) -> None:
        log.warning("spool file %s rejected: %s", path.name, why)
        try:
            self.rejected.mkdir(exist_ok=True)
            path.replace(self.rejected / path.name)
        except OSError:
            path.unlink(missing_ok=True)
        conn = db.connect(self.s.db_path)
        try:
            db.log_event(conn, "spool_rejected", {"file": path.name, "why": why[:300]})
        finally:
            conn.close()

    def _handle(self, path: Path) -> None:
        kind = path.name.split("_", 1)[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        if kind == "bars":
            payload = BarsPayload.model_validate(data)
            rows = store_bars(self.s, payload)
            bar = read_candidate(self.s, payload, rows)
            if bar is not None and self.on_new_bar is not None:
                self.on_new_bar(payload.symbol, bar)
        elif kind == "hb":
            payload = HeartbeatPayload.model_validate(data)
            # When the EA wrote it, not when we got round to reading it.
            store_heartbeat(self.s, payload, int(path.stat().st_mtime))
        else:
            raise ValueError(f"unknown file kind {kind!r}")

    def process_once(self) -> int:
        """Handle the waiting files. Returns how many were stored."""
        try:
            files = sorted(self.dir.glob("*.json"))[:MAX_FILES_PER_ROUND]
        except OSError:
            log.exception("cannot list spool dir %s", self.dir)
            return 0
        stored = 0
        for path in files:
            try:
                self._handle(path)
            except FileNotFoundError:
                continue  # vanished meanwhile
            except (ValueError, ValidationError, UnknownSymbol) as exc:
                # json.JSONDecodeError and UnicodeDecodeError are ValueErrors too.
                self._reject(path, f"{type(exc).__name__}: {exc}")
                continue
            path.unlink(missing_ok=True)
            stored += 1
        return stored

    def run(self) -> None:
        log.info("watching spool dir %s", self.dir)
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:  # keep watching whatever happens
                log.exception("spool round crashed")
            self._stop.wait(POLL_SEC)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="spool", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
