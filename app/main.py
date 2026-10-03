"""FastAPI app: ingest endpoints for the MT5 EA. Run with:

uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
"""

import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException

from app import db
from app.config import Settings, get_settings
from app.models import BarsPayload, HeartbeatPayload
from app.timeconv import server_to_utc

log = logging.getLogger("signal")


def _iso(epoch: int | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db(settings.db_path)
        if not settings.secrets.ingest_token:
            log.error("INGEST_TOKEN is empty: every authenticated endpoint will answer 401")
        conn = db.connect(settings.db_path)
        try:
            db.log_event(conn, "service_start")
        finally:
            conn.close()
        yield

    app = FastAPI(title="signal-service", lifespan=lifespan)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        """Bearer auth. Fails closed: an empty configured token never matches anything."""
        expected = settings.secrets.ingest_token
        scheme, _, given = (authorization or "").partition(" ")
        if (
            not expected
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(given.encode(), expected.encode())
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    def health() -> dict:
        conn = db.connect(settings.db_path)
        try:
            last_bar: dict[str, dict[str, str | None]] = {}
            for r in conn.execute(
                "SELECT symbol, tf, MAX(t_utc) AS t FROM bars GROUP BY symbol, tf"
            ):
                last_bar.setdefault(r["symbol"], {})[r["tf"]] = _iso(r["t"])
            hb = conn.execute("SELECT MAX(received_at) AS t FROM heartbeats").fetchone()["t"]
        finally:
            conn.close()
        return {"ok": True, "last_bar_utc": last_bar, "last_heartbeat_utc": _iso(hb)}

    @app.post("/v1/bars", dependencies=[Depends(require_token)])
    def post_bars(payload: BarsPayload) -> dict:
        if payload.symbol not in settings.config.symbols:
            raise HTTPException(status_code=422, detail=f"unknown symbol {payload.symbol!r}")
        mode = settings.config.server_time_mode
        now = int(time.time())
        rows = [
            {
                "symbol": payload.symbol,
                "tf": payload.timeframe,
                "t_server": b.t,
                "t_utc": server_to_utc(b.t, mode),
                "o": b.o,
                "h": b.h,
                "l": b.l,
                "c": b.c,
                "tv": b.tv,
                "sp": b.sp,
                "received_at": now,
            }
            for b in payload.bars
        ]
        conn = db.connect(settings.db_path)
        try:
            accepted = db.upsert_bars(conn, rows)
        finally:
            conn.close()
        return {"accepted": accepted}

    @app.post("/v1/heartbeat", dependencies=[Depends(require_token)])
    def post_heartbeat(payload: HeartbeatPayload) -> dict:
        row = payload.model_dump(exclude={"schema_version"})
        row["connected"] = int(row["connected"])
        row["trade_allowed"] = int(row["trade_allowed"])
        row["received_at"] = int(time.time())
        conn = db.connect(settings.db_path)
        try:
            db.insert_heartbeat(conn, row)
        finally:
            conn.close()
        return {"ok": True}

    return app
