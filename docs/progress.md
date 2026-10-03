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

## T2 — Server time (code done, **ASK pending**)
- `scripts/check_server_time.py`: reads the latest heartbeats, lists which candidate rule (`ny_plus_7`, several European zones, fixed offsets) explains each reported `server_utc_offset_sec`, and compares `time_server` against the real receive time.
- `timeconv.offset_matches()` is the check the heartbeat monitor will use in T8.
- Tests cover summer/winter and the 25 Oct – 1 Nov 2026 gap.
- **ASK (needs FTMO demo heartbeats):** run the script on a normal day, again in the Oct 25 – Nov 1 gap, then confirm the mode before it goes in `config.yaml`. `server_time_mode` is still the placeholder `ny_plus_7`.

## T3 — Sessions and calendar (done)
- `app/sessions.py`: session windows in exchange time via `zoneinfo`, holidays from `exchange_calendars` (XETR for EU, XNYS for US), `active_session`, `bar_index_in_session`, `previous_trading_day`, `next_session_start`, and `news_flag` (bar overlapping ±`news_window_min` of a configured event).
- A bar belongs to a session when its **open** time is inside the window (09:00–11:00 → bars opening 09:00 … 10:55).
- Config change: sessions got a `cash_close` time (eu 17:30, us 16:00) so "gap vs. prior close" can use the cash close. Check these two values.
- Tests: fixed dates incl. the Oct and Mar DST gaps, a holiday that closes only one exchange (1 May), Thanksgiving, Christmas, weekends, far-future dates (fail closed).
- FTMO's `.cash` CFDs may trade longer than the cash market; `cash_close` only defines which price counts as the "prior close".

## T4 — Features (code done, **ASK pending**)
- `app/features.py`, pure functions over closed-bar DataFrames: `ema`, `atr` (Wilder), `classify_bars` (shape ratios, trend bars, doji, inside/outside, bar-type label, `sig_long`/`sig_short`), `confirmed_swings` (shown on the *confirmation* row, N bars after the swing), `leg_counts` (H1/H2, L1/L2), `day_context`, `day_type_hint`, `compute_features` (all per-bar columns).
- Every definition is in its docstring; thresholds are named constants at the top.
- **No-lookahead tests:** all bars after t are overwritten with junk and nothing at or before t may change; the same for truncated vs. full history, for the day context and for the HTF state.
- **ASK — please review the H/L leg-count reset rules** (`leg_counts` docstring). Summary for the bull side: a pullback starts with a lower-high bar; the next bar with a higher high is H1; the next pullback plus higher-high bar is H2; the count resets when (a) a new day starts, (b) price makes a new high of the day (above `ref_high`), or (c) a bar's low undercuts the last confirmed swing low (bull structure broken). The bear side is the mirror image. Questions: is "new high of the session day" the right reset for (b), or should it be a new swing high? Should the count also reset when the close is below the EMA for N bars?
- Also check: `signal bar` rule (close in outer third, tail ≤ 25 % of range, range ≤ 2 ATR) and the `day_type_hint` thresholds (tight channel = 10 bars on one side of the EMA with ≤ 2 touches; range = 3+ EMA crosses; trend = ≤ 1 cross, range ≥ 3 ATR, price in the outer quarter).

## T5 — Higher timeframe (done)
- `app/htf.py`: `htf_state` per timeframe from three votes (EMA20 slope, price vs. EMA, last two confirmed swings), `alignment` → `aligned_bull | aligned_bear | conflict`. Neutral counts as conflict by default (`rules.htf_neutral_counts_as_conflict`). Only bars that have **closed** by `asof` are used.
- Slope and swing comparisons use a tolerance of 0.15 × ATR so sideways noise is not read as a trend (found by a test on perfectly flat data).
- `db.load_bars()` returns a DataFrame for the features (tested).
