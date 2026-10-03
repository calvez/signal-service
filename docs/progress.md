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

## T6 — LLM client (done)
- `app/llm.py`: OpenRouter chat completions via httpx. Model, provider order and `allow_fallbacks` from `config.yaml`; temperature 0; `response_format: json_object`; `provider.require_parameters` so only providers that honour it are used; 30 s timeout; one retry on network errors only (never on an HTTP status).
- Every call that reaches the network is stored in `llm_calls` (prompt version, full prompt, raw response, provider, latency, tokens, cost); the reader fills `parsed` and `validation` on the same row. The API key only travels in the Authorization header and is verified absent from the DB in a test.
- Budget guard: daily cap in USD per **UTC day**, based on the cost OpenRouter reports (`usage.include`). Once reached: no more calls, and one `llm_budget_exceeded` event per day (T8 turns it into the Telegram alert).
- Verified live with `google/gemini-2.5-flash-lite` (valid JSON, provider and cost logged, ~$0.0003 per read at ~2.8k tokens in). The OpenRouter key is in `.env` (git-ignored, mode 600); it has a $30 limit.

## T7 — Market read (done)
- `app/validate.py` implements all ten rules of `docs/protocol.md` §4. Any failure forces `action = none` and the reason is stored. Notes on the exact behaviour:
  - A `reason` longer than 300 characters counts as a schema failure (as specified, no truncation).
  - Rule 8 is implemented literally: counter-trend needs `trading_range` and price in the top **or** bottom 20%, regardless of direction. A counter-trend *long* at the top of the range would pass; consider making the edge direction-aware.
  - Grade B or a counter-trend alert becomes a silent `watch` (not skipped).
  - H1/D1 conflict is also enforced here, although the reader already skips those bars.
- `app/reader.py`: runs for each new closed M5 bar of a traded symbol. Skips without calling the LLM when: not a traded symbol, outside the session window, data older than two bar periods (bar close + 600 s), already read, budget exceeded, digits unknown, bar/history missing, H1/D1 conflict. Skips are logged; the routine ones (not traded, outside session) only to the console to keep `events` small.
- Any exception inside a read is caught, logged as `read_crashed`, and results in no alert.
- Stored in `reads`: one row per evaluated bar (UNIQUE per symbol and bar), including the final action, push flag, setup, model and validation text. An LLM error is stored as `action none`. `notified_at` is for the Telegram sender (T8).
- Wiring: `POST /v1/bars` starts a background read for the newest bar of an M5 batch of a traded symbol. Without an API key or with the placeholder model `SET-ME` the service still ingests but makes no reads.
- New table `symbol_meta` (digits per symbol, from the bars payload) for price rounding and tick size. Tick size is currently 10^-digits; FTMO's real tick size may be larger.
- Tests (139 total): each validation rule, prompt rendering without holes, **no future bar in the prompt**, every skip rule, garbage / error / bad-price answers never alert.
- **Open:** pick the model. Candidates priced per 1M tokens in/out: `google/gemini-3.5-flash` ($1.50/$9), `openai/gpt-5.4-mini` ($0.75/$4.50), `anthropic/claude-sonnet-5.5` ($2/$10), `google/gemini-2.5-flash-lite` ($0.10/$0.40). At ~72 reads a day with ~3k tokens each, even the dearest is under $1 a day. `config.yaml` still has `model: "SET-ME"`.

## T8 — Telegram (code done, **ASK pending**)
- Telegram credentials are in `.env`: bot `@clvztradebot`, chat id of Lorant's private chat (the only chat the bot answers). Verified live: the bot polled his earlier messages, answered, and sent a status text and a test chart (marked as test data).
- `app/telegram.py`: httpx wrapper (`TelegramApi`), long-polling `Bot` (offset kept in the DB so a restart does not replay updates), chat-id allowlist (others are ignored and logged as `telegram_rejected`), command router, feedback buttons → `feedback` table (latest choice per read counts). API errors are logged by exception type only, because the bot token is part of every URL (tested).
- Commands: `/status /today /brief [eu|us] /chart <SYMBOL> [M5|H1|D1] /pause [min] /resume /screenshot /help`. Every command is logged in `events`. None can trade. `/restart_mt5` answers "not enabled yet" (see ASK).
- `app/messages.py` (alert text per protocol §5, buttons), `app/charts.py` (1080×1350 dark PNG: EMA20, opening range, day high/low, entry/stop/target), `app/reports.py` (texts for /status, /today, brief, wrap, daily report), `app/status.py` (monitors and FTMO risk), `app/scheduler.py` (sending, schedule, threads).
- Sending: only validated `alert`/`watch` reads are sent; alerts have sound, watches (and grade B) are silent. Each read is claimed before sending so nothing goes out twice. Reads older than two bar periods are dropped (`alert_dropped`), and reads made while paused are muted (`alert_muted`) and not replayed after `/resume`. If the chart fails, the text is sent without it.
- Monitors (every 30 s): no heartbeat (180 s in a session / 600 s otherwise, counted from service start too), broker disconnected (2 heartbeats), stale M5 data per traded symbol (only judged during its own session), time-rule mismatch, LLM budget, 3 failing LLM calls, low disk, position changes. Each problem is announced once and once when it clears; the state lives in the DB, so a restart does not repeat it. Quiet hours (22:00–07:00 Budapest): ops alerts are silent unless a position is open.
- FTMO risk (approximate): day-start balance = first heartbeat after midnight Europe/Prague (tested for summer and winter time); warns once per level (50 %, 80 %) per day, only the highest level crossed; also news-with-open-position (10 min ahead) and Friday 21:45 Berlin. The "max loss" base is the configured initial balance.
- Scheduled messages: pre-session brief at `brief_at` (not sent late), session wrap within 30 min after the window, daily report 18:00 Berlin on trading days. Hypothetical R in the wrap/report says "coming with the outcome simulator" until T9.
- Tests: 180 total. New: chat-id filter, command parsing, feedback buttons, de-dup state machine incl. restart, quiet hours, position changes, FTMO day start across midnight Prague and DST, brief/wrap/daily timing.
- **ASK 1:** confirm the FTMO limits in `config.yaml` against the account's objectives (`initial_balance: 80000`, daily loss 5 %, max loss 10 %, reset at midnight Prague). The warnings use these numbers.
- **ASK 2:** `/screenshot` runs ImageMagick `import -display :99` as the service user. Inside the container the service will run as a different user than `mt5`, so the options are: run the service as `mt5`, or a narrow sudoers rule (`sudo -u mt5 import ...`). Which one? `/restart_mt5` (restart `mt5-terminal` after a confirm button) needs a sudoers rule too and is not built until you approve it.
- Note: the service start message and monitors use the real clock; on this host without MT5 running, a started service will report "MT5 not reporting" after 10 minutes. That is expected until T10.
