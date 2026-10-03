# Phase 1 — task list

Before T0: the host and the `trader` container are set up per `docs/server-setup.md` §2–4 (Lorant does this, or Claude Code on the host with his go-ahead per block). All tasks below run inside the container.

Work in order. Each task ends with passing tests and a short note in `docs/progress.md` (what was done, what was verified, open questions for Lorant). Stop and ask Lorant when a task says **ASK**.

## T0 — Skeleton
- `git init`, `.gitignore` (`.env`, `data/`, `*.db`, `.venv/`, `__pycache__/`, `deploy/mt5/startup.ini`, `deploy/mt5/BarPusher.set`), `pyproject.toml`, ruff + pytest configured.
- `.env.example` with the four secrets from CLAUDE.md, empty.
- `config.yaml` from `config.example.yaml`.
- FastAPI app with `/health` only. `pytest` runs green.

## T1 — Ingest
- SQLite schema (WAL): `bars(symbol, tf, t_server, t_utc, o, h, l, c, tv, sp, received_at)` with PK `(symbol, tf, t_utc)`; `heartbeats`; `events` (generic log).
- `POST /v1/bars` and `POST /v1/heartbeat` per `docs/protocol.md` §1–2, bearer auth, pydantic models, idempotent upsert.
- Tests: auth rejected without token, duplicate bars upsert cleanly, malformed payload → 422 and nothing stored.

## T2 — Server time
- `timeconv.py` with the three `SERVER_TIME_MODE` variants.
- Script `scripts/check_server_time.py`: reads the latest heartbeats and reports which mode matches.
- **ASK**: once real heartbeats arrive from the FTMO demo, report the observed offset and the matching mode; Lorant confirms before it goes in config.
- Tests: DST transition days for both Europe and US, including the Oct 25 – Nov 1 2026 gap.

## T3 — Sessions and calendar
- `sessions.py`: is a UTC time inside the EU or US window (exchange time from config); session open bar; holidays from `exchange_calendars` (XETR for EU, XNYS for US) — no session on holidays.
- News flags: a `config.yaml` list of high-impact event times (manual for now, e.g. US CPI, NFP, FOMC, ECB). Mark bars within ±2 min. Do not scrape anything yet.
- Tests: a few fixed dates incl. a holiday and a DST-gap day.

## T4 — Features (the important one)
Pure functions over a pandas DataFrame of closed bars, each returning a column, each with no-lookahead tests. Keep definitions in docstrings so Lorant can check them against Brooks.
- `ema(close, 20)`; `atr(14)`.
- Bar classification: body/range ratio, close position in range, `trend_bar_bull/bear` (body ≥ 50% of range, close in outer third), `doji` (body ≤ 25%), `inside`, `outside`.
- Signal-bar quality for longs/shorts (close near extreme, small tail on the trade side, not too large relative to ATR).
- Swing highs/lows with N-bar confirmation (default 2 right-side bars); confirmed only after those bars close.
- H/L leg counting: count pullback legs within a trend (H1/H2 in bull, L1/L2 in bear); reset rules documented. **ASK** Lorant to review the reset rules before relying on them.
- Day context: opening range (first 6 M5 bars of session), today's high/low, position of price in today's range, number of EMA crosses today, consecutive bars on one side of EMA, gap vs. previous close.
- Deterministic `day_type_hint` (tight channel / trend / trading range / unclear) from the above — a hint for the LLM and the validator, not a verdict.

## T5 — Higher timeframe
- `htf.py`: H1 and D1 trend state from EMA20 slope + price vs. EMA + last confirmed swing structure. Returns `bull | bear | neutral` per timeframe and `aligned_bull | aligned_bear | conflict`.
- Neutral on either timeframe counts as `conflict` (skip rule). Make that configurable.

## T6 — LLM client
- `llm.py`: OpenRouter chat completions via httpx. Model id, provider order and `allow_fallbacks: false` from config. Temperature 0, JSON response format where supported, 30 s timeout, one retry on network error only.
- Budget guard: daily USD cap from config; when exceeded, stop calling and alert once.
- Log every call per CLAUDE.md non-negotiable 5.

## T7 — Market read
- `prompts/market_read_v1.md` is the starting prompt; fill its placeholders from the features.
- `reader.py`: on each new closed M5 bar of a traded symbol inside a session window, build the prompt (last 36 M5 bars as a compact table + feature summary + HTF state + session info), call the LLM, validate (`docs/protocol.md` §4), store `reads`.
- Skip the call entirely (log reason) when: outside session, HTF conflict, data stale, budget exceeded.

## T8 — Telegram (the whole UI — follow `docs/telegram.md`)
- Bot with long polling, chat-id allowlist, command router, `events` logging.
- `/status`, `/today`, `/brief`, `/chart`, `/pause`, `/resume`, `/help` first. Then `/screenshot` (ImageMagick `import -display :99 -window root`; the service runs as user `mt5` or via a narrow sudoers rule — **ASK**).
- Trade alerts and watches with chart PNGs (`charts.py`) and feedback buttons → `feedback` table.
- Pre-session briefs, session wraps, daily report.
- Monitors in `status.py`, checked every 30 s: heartbeat age, broker connection, stale data, time check, LLM budget/errors, disk, position changes, FTMO risk levels (§3 — **ASK** Lorant to confirm limits and initial balance).
- Alert de-duplication (once on problem, once on recovery) and quiet hours.
- Tests: chat-id filter, command parsing, de-dup state machine, FTMO day-start balance across midnight Prague and DST.

## T9 — Outcomes and daily report
- `outcomes.py`: for every `alert`/`watch` read, simulate: did the stop entry trigger within the next 3 bars; then which was hit first, stop or target, using M5 high/low (if both in the same bar, count as loss). Record R result. This is hypothetical, clearly labelled as such.
- Daily report at 18:00 Berlin: reads, alerts, his feedback, hypothetical results, LLM cost.
- Weekly CSV export of `reads` + `feedback` + `outcomes` for Lorant to analyse.

## T10 — Deploy
- systemd unit (`signal-service.service`) running uvicorn on 127.0.0.1:8000 as a dedicated user.
- MT5 headless stack from `deploy/mt5/` per `docs/mt5-linux.md`. **ASK** Lorant before running `install.sh` (root; he fills in `startup.ini` and `BarPusher.set` himself — never ask him to paste the FTMO password into the chat).
- Verify end to end: bars arrive, `/status` is green, and after `systemctl restart mt5-terminal` the EA is back (startup.ini reattaches it) and Telegram reported the outage and the recovery.
- Backups: install `deploy/backup/` (script + timer), set up `/root/.restic-env` with Lorant, run once by hand, then test a restore into a scratch container (`docs/server-setup.md` §6). **ASK** for the Storage Box details.
- Reboot test: reboot the host; the container autostarts, MT5 logs back in, Telegram shows the outage and the recovery.
- Walk through the checklist in `docs/server-setup.md` §7 and report each item.

## Done when
Two weeks of real sessions on the FTMO free trial with: no missed bars, no time-check alerts, every read logged, every alert answered with a button, and every MT5 outage reported in Telegram within 3 minutes. Then Lorant and Claude review the numbers before any phase-2 work.
