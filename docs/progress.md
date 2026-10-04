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

## T9 — Outcomes and daily report (done)
- `app/outcomes.py`: hypothetical outcome of every alert **and** watch, stored in `outcomes` and re-simulated until final. Rules (also in the module docstring):
  - Entry is a stop order that must trigger within the next 3 M5 bars after the signal bar; otherwise `no_entry` (not counted in R). Fill = the entry price (no slippage or spread).
  - Then stop or target, whichever the M5 high/low reaches first. A bar touching both counts as a **loss**, including the entry bar (if it triggers the entry and also reaches the stop, the order inside the bar is unknown, so it is a loss).
  - win = +reward/risk, loss = −1 R. Still open at the cash close of that session day (`cash_close` in config): `expired`, marked to the last close. Not enough bars yet: `pending`.
- Telegram: `/today`, the session wrap and the daily report now show "Simulated AI alerts: n → wins · losses · expired · no entry · pending · ±R", plus the same for the alerts he answered "I'd take it". The word "Simulated" is always there. The wrap at the end of the window shows mostly "pending" because trades run until the cash close; the 18:00 daily report settles the EU ones, the US ones settle after 22:00 Berlin.
- The scheduler updates outcomes every 60 s.
- Weekly CSV: Saturday 10:00 Berlin the bot sends `reads_<year>-W<week>.csv` (reads + his latest answer + simulated outcome; columns in `outcomes.CSV_COLUMNS`) and keeps a copy in `data/exports/`. Manual export: `uv run python scripts/export_csv.py --days 7 --out reads.csv`.
- Tests: 196 in total. New: win/loss/same-bar/entry-bar cases for long and short, entry window edges (3rd bar counts, 4th does not), expiry and pending, bars outside the replay window ignored, database updates, summaries, CSV content, weekly job once per week.
- Limits to keep in mind when reading the numbers: M5 high/low cannot show the order of events inside a bar (hence the conservative loss rule); there is no spread, slippage or commission; the 3-bar entry window and "hold until cash close" are my reading of the task and easy to change in `outcomes.py`.

## Decisions (after T9)
- **Model:** `moonshotai/kimi-k3` on OpenRouter ($0.72 in / $13 out per 1M tokens). Live check on synthetic bars: valid JSON every time, ~4.4 s, ~2.2k tokens in / ~180 out, **~$0.0033 per read** (about $0.25 a day for ~72 reads, against the $3 cap). No reasoning tokens are billed. Provider order is still empty; pin one once real days show which provider is stable.
- **Prompt v2** (`prompts/market_read_v2.md`, now the configured version): v1 plus two explicit rules, because K3 first wrote a `reason` longer than 300 characters and once answered `watch` without a setup (both are rejected by the validator, correctly). v1 stays in the repo. With v2: 5 of 5 answers valid; 2 of them were then rejected by the price rules (reward/risk, stop distance), which is the validator doing its job.
- **FTMO limits** (`initial_balance 80000`, daily loss 5 %, max loss 10 %, reset midnight Prague): confirmed by Lorant, no change.
- **Server time:** to be determined from the real FTMO demo heartbeats (T10): run `scripts/check_server_time.py` once MT5 reports, and again between 25 Oct and 1 Nov 2026.

## Account (from Lorant, after T9)
- FTMO 2-step, **€160,000**: max daily loss −€8,000 (5 %), max loss −€16,000 (10 %), profit target €8,000 (this is the 5 % figure shown for the step he pasted), minimum 2 trading days. `ftmo.initial_balance` in `config.yaml` is now 160000 (it was the placeholder 80000); the percentages already matched. Docs examples updated.
- `CLAUDE.md` still says "€80k account" in its first section. It is read-only for me, so Lorant should edit that line.
- The profit target and the minimum trading days are not tracked in phase 1.
- The free-trial demo account that MT5 will log into may show a different balance. The first heartbeat shows it, and the risk percentages are always computed against `initial_balance`, so compare the two then.

## T10 — Deploy (in progress)
Done on the host (with Lorant's go-ahead, 2026-10-03/04):
- Host hardening (`docs/server-setup.md` §2): packages updated, timezone UTC, SSH keys only (`/etc/ssh/sshd_config.d/10-signal-hardening.conf`; the host was being brute-forced with passwords, all of Lorant's logins were key-based), `ufw` active with only OpenSSH allowed in, plus the Incus bridge rules. chrony synced.
- Incus 6.0.5 with a btrfs loop pool (200 GiB) and a NAT bridge `incusbr0` (no IPv6, nothing exposed over the network). Container `trader` (Ubuntu 24.04.5, Python 3.12.3): 4 CPUs, 8 GiB, autostart, daily snapshots kept 14 days. Snapshot `after-service-deploy` taken.
- The `incus config set` lines in `docs/server-setup.md` §4 used a syntax Incus 6 rejects; fixed to `key=value`.
Done in the container:
- Service deployed to `/opt/signal-service` as the unprivileged user `signal` (`deploy/deploy.sh`, unit `deploy/signal-service.service`), listening on 127.0.0.1:8000 only. `.env` there has a new random `INGEST_TOKEN` (the same value must go into `BarPusher.set`), mode 600. All 198 tests pass inside the container. Telegram polling from the container works (it answered Lorant's earlier commands).
- Backup files now point at `/opt/signal-service`.
- Logging: console logging for journald, with `httpx`/`httpcore` at WARNING because their INFO lines contain the Telegram bot token in the URL (test added).
Findings worth knowing:
- `rsync` hangs on exit inside the container on this host (files are copied, the processes never end, and cannot be killed from another `incus exec` session because AppArmor blocks signals between the container profile and exec-spawned processes). `deploy.sh` uses `tar` instead. Restarting the container clears such processes.
- A manual snapshot gets the 14-day expiry too; set `incus snapshot create trader <name> --expiry ...` or edit it if one should be kept longer.
Still to do: MT5 install (**ASK**), end-to-end check, restic backups (**ASK** Storage Box), reboot test, §7 checklist.

### MT5 install (2026-10-03/04) — what it took
- **Wine 10.0, not 11:** with Wine 11 the MT5 installer stops with "A debugger has been found running in your system" (MetaQuotes anti-debug; known Wine 11 issue). `install.sh` pins `winehq-stable=10.0.0.0~noble-1`.
- **Mono/Gecko prompts** block `wineboot` on a display nobody watches: `WINEDLLOVERRIDES=mscoree,mshtml=`.
- **AppArmor on this host** (kernel 7.0) blocked signals between processes inside the container, so systemd could not stop Wine processes. Fix: `incus config set trader raw.apparmor='signal (send) peer="incus-trader_**",'` (signals only within this container). Added to `docs/server-setup.md`-worthy notes; restart the container after setting it.
- **`/portable`** for terminal and MetaEditor (otherwise MT5 uses AppData and ignores the files `install.sh` places), **relative paths** in `/config:` and `/compile:` (Wine quotes paths with spaces and MT5 keeps the closing quote).
- **FTMO servers:** the generic MetaQuotes installer does not know `FTMO-Demo`. FTMO's own installer (`https://download.mql5.com/cdn/web/ftmo.global.markets/mt5/ftmo5setup.exe`, linked from ftmo.com) ships `Config/servers.dat`; copied into our install. Login works: "authorized on FTMO-Demo", account "€160k FTMO Free Trial 2-Step", 166 symbols.
- **Config folder:** the installer creates `Config`, the old script created `config` (Linux is case-sensitive); merged into `Config`.
- **Graceful stop:** `ExecStop` now closes MT5 with `taskkill` (no `/f`) before `wineserver -w`, so MT5 saves settings and profile.
- **MT5 build 6235 starts an MCP server** (Tools > Options > MCP, "Enable internal server", 127.0.0.1:22346) through which AI tools could control the terminal, including trading. Disabled, because phase 1 must have no path to execution.
- **Data:** first heartbeat and the full backfill arrived (4 symbols × 2000 M5 / 500 H1 / 250 D1 bars). Server offset +3 h; matches `ny_plus_7` (and Athens/Nicosia until the Oct 25 – Nov 1 check).
- **OPEN — WebRequest allow-list:** MT5 only accepts it from Tools > Options (stored encrypted), and every start with `/config` resets all Expert options to the start-config values. So the EA's HTTP calls are blocked after each restart. Decision needed (see chat).

### EA transport switched to files (decision by Lorant, 2026-10-04)
- MT5 resets the WebRequest allow-list on every start with a start config, and it can only be set in the GUI, so the EA's HTTP calls stopped after each restart. Lorant chose files instead of HTTP.
- **BarPusher 1.10** writes each payload (same JSON as before) into `MQL5\Files\signal` → `/var/spool/signal-mt5` (owner `mt5`, group `signal`, 2770). It has no network code any more, and on start it closes older duplicate GER40 M5 charts (the start config opens a new one each time).
- **`app/spool.py`** reads the folder every 2 s with the HTTP endpoints' validation (shared in `app/ingest.py`); bad files go to `rejected/` with a `spool_rejected` event. Market reads run in a 2-thread pool. Tests in `tests/test_spool.py`.
- **Algo trading off:** start config `[Experts] Enabled=0, AllowLiveTrading=0`. Verified: the EA keeps running and heartbeats report `trade_allowed=false`. New red Telegram alert if a heartbeat ever reports it on.
- Verified live: EA 1.10 heartbeats and the full backfill arrive through the spool, nothing rejected, one GER40 chart left.
- `docs/protocol.md` §0, `docs/mt5-linux.md` and `docs/server-setup.md` updated. **`CLAUDE.md` still shows `POST /v1/…` from the EA in its architecture diagram; it is read-only for me, so Lorant should update that line.**

### T10 status (2026-10-04 05:45 UTC)
Verified live in the container:
- MT5 (Wine 10.0, build 6235) logged in to FTMO-Demo, €160k Free Trial 2-Step; EA 1.10 delivers heartbeats and bars through the spool; algo trading off (`trade_allowed=false`); MT5's MCP server off.
- `systemctl restart mt5-terminal`: heartbeats back after ~20 s, still one GER40 chart, settings kept.
- `/status` and the EU brief render from live data (example: GER40 H1 bull, D1 bear → conflict, so reads would be skipped).
- `/screenshot` through the sudo rule works for user `signal`; any other `systemctl` call is refused.
- Snapshots: `before-mt5-install` (30 d), `mt5-working-2` (60 d), daily snapshots running. One `incus snapshot create` hung once in Incus (killed the client; the retry took a second).
Still open:
- **Reboot test** of the host (it ends this SSH session, so Lorant runs it or approves it explicitly).
- **Backups** (restic to a Hetzner Storage Box): needs the Storage Box details from Lorant.
- **Server time:** offset +3 h matches `ny_plus_7`; re-run `scripts/check_server_time.py` between 25 Oct and 1 Nov 2026 to rule out Athens/Nicosia.
- First real session on Monday 2026-10-05 (EU 09:00 Berlin): watch reads, alerts and the session wrap.
- Rotate the secrets that were pasted into the chat (OpenRouter key, Telegram bot token, FTMO trial password).

## Backtest and LLM preparation (2026-10-04) — direction from Lorant
"Python evaluates, the LLM makes the recommendation." The first strategy is Lorant's choice (next step). Prepared:
- **History:** `deploy/mt5/history-dump.sh` + `mt5/HistoryDump.mq5` exported everything FTMO provides: M1/M5/H1/D1 from 2017-12-28 (US30 from 2019-02-08) to 2026-10-02, 14.7 M bars, imported into `data/history.db` (739 MB, separate from the live DB, not backed up, can be re-downloaded).
- **Shared evaluation:** `app/evaluation.py` (`evaluate_bar`) is used by both the live reader and the backtester, same windows, no lookahead.
- **Strategy slot:** `app/strategies` (`Candidate`, `Strategy`, `get_strategy`). Only `demo` exists (plumbing test, not a trading idea).
- **Backtester:** `app/backtest.py`, `scripts/backtest.py`: session bars only, H1/D1 gate, `check_setup`, outcomes on M1, spread charged in R, his trade rules (or `--all-signals`), in/out-of-sample split, CSV. Speed: ~14 s per symbol and 4 months (a full 9-year run over 4 symbols ≈ 25 min). Further speed-up possible by vectorising the swing detection.
- **LLM on top of Python:** `engine.strategy` in `config.yaml` (empty = off = today's behaviour). When set: no candidate → no LLM call; otherwise prompt v3 with the evaluation and numbered candidates, answer `take | watch | skip` (schema 2, `validate_recommendation`); prices always from Python.
- Guide: `docs/backtest.md`.
- Observation from the smoke test (GER40, Jun–Sep 2026): the H1/D1 gate with "neutral counts as conflict" blocked 72% of session bars. Worth deciding deliberately when the strategy is chosen.
