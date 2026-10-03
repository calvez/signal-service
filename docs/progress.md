# Progress

## T0 — Skeleton (done)
- `git init`, `.gitignore`, `pyproject.toml` (uv, Python 3.12, ruff, pytest), `.env.example`, `config.yaml` (copy of the example), `app/config.py` (secrets from `.env`, everything else from `config.yaml`, strict pydantic models).
- Verified: `pytest` green, `ruff check` and `ruff format --check` clean, `uvicorn app.main:create_app --factory` serves `/health` on 127.0.0.1:8000.
- Dev environment: this machine is the bare-metal host, which has no Incus container yet. Python 3.12 comes from `uv` (user space, `~/.local`), the venv is `.venv/`. Run everything with `uv run ...`. Hardware line in `docs/server-setup.md` corrected (i7-7700, 62 GiB RAM, 3 × NVMe in RAID5).

## T1 — Ingest (done)
- SQLite (WAL) schema: `bars`, `heartbeats`, `events` in `app/db.py`. All times are UTC epoch seconds; `bars.t_server` keeps MT5's raw value. Inspect with `select datetime(t_utc,'unixepoch') ...`.
- `POST /v1/bars` (idempotent upsert on `(symbol, tf, t_utc)`, max 500 bars, OHLC sanity check, symbol must be in `config.yaml`), `POST /v1/heartbeat`, `GET /health`. Bearer auth with constant-time compare; an empty `INGEST_TOKEN` rejects everything (fail closed).
- `app/timeconv.py` already exists with all three `SERVER_TIME_MODE` variants because `t_utc` is part of the bars primary key. T2 still owes the real-heartbeat check and `scripts/check_server_time.py`.
- Decisions to check: (1) unknown payload fields → 422 (`extra="forbid"`); (2) `bar_time_utc` in the LLM schema will mean the **open time** of the latest closed M5 bar.
- Open: `.env` currently holds a throw-away dev `INGEST_TOKEN`; replace with a long random value before the EA connects (T10).
