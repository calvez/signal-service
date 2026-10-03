# Signal Service — project guide for Claude Code

## What this is

A phase-1 market observer for Lorant's FTMO day trading (€80k account, 2-step evaluation, MetaTrader 5).

- Everything runs in one Incus container (`trader`, Ubuntu 24.04) on a Hetzner bare-metal host in Finland running Ubuntu 26.04 (`docs/server-setup.md`). Don't upgrade the container to 26.04: WineHQ and `install.sh` target 24.04.
- MetaTrader 5 runs **headless in that container** under Wine on a virtual display nobody looks at (Xvfb, see `docs/mt5-linux.md`). Its Expert Advisor (`mt5/BarPusher.mq5`) pushes closed bars to this Python service over 127.0.0.1.
- The service stores the bars and computes Al Brooks price-action features **in code**.
- It asks an LLM (via OpenRouter) for a context read and a possible setup, validates the answer, and sends Telegram alerts.
- **Telegram is the only user interface:** alerts, statuses, briefs, reports and commands (`docs/telegram.md`). There is no GUI or web dashboard.

**Phase 1 never places trades.** There is no code path from this service to order execution.

Read `docs/protocol.md`, `docs/telegram.md` and `docs/tasks-phase1.md` before writing code (and `docs/server-setup.md` before touching the host), then work through the tasks in order.

## Architecture

```
Host: Hetzner bare metal (Ubuntu 26.04) → Incus container "trader" (Ubuntu 24.04) (no GUI access)
┌───────────────────────────────┐            ┌──────────────────────────────────────┐
│ Xvfb :99 + openbox (virtual)  │            │ uvicorn 127.0.0.1:8000               │
│ wine terminal64.exe (MT5)     │ ─────────▶ │ FastAPI ingest → SQLite              │
│  started from startup.ini     │ POST /v1/… │ features → LLM read → validator      │
│  └ BarPusher EA: bars,        │ 127.0.0.1  │ status/risk/ops monitors             │
│    heartbeat + account state  │            │ Telegram bot (long polling) ◀──▶ 📱   │
└───────────────────────────────┘            └──────────────────────────────────────┘
```

## Non-negotiables

1. **No execution in phase 1.** No endpoint, queue or message that could make MT5 trade.
2. **Fail closed.** On any error, timeout, invalid JSON or stale data, send no alert and log it. Never alert on data older than two bar periods.
3. **Code does mechanics, the LLM does context.** EMA, swings, bar types and H/L counts are computed deterministically. The LLM never computes indicators.
4. **LLM output is untrusted.** Validate the schema and price sanity (`docs/protocol.md` §4) before anything reaches Telegram.
5. **Log everything.** For every LLM call, store the prompt version, the full prompt, the raw response, the parsed result, the validation outcome, the model, the provider, latency, tokens and cost.
6. **Secrets.** Keep secrets only in `.env`, which is git-ignored: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `INGEST_TOKEN`. Never log them. The FTMO login lives only in MT5's `config/startup.ini` inside its Wine prefix (mode 600, user `mt5`); this service never reads, stores or logs it.
7. **Network.** Bind only to 127.0.0.1. Nothing from this project is reachable from the internet. The Telegram bot uses long polling, not a webhook. Bot commands are accepted only from `TELEGRAM_CHAT_ID`, and no command can trade. Every endpoint except `/health` requires `Authorization: Bearer <INGEST_TOKEN>`.
8. **Time.** Store all times in UTC. Session logic uses exchange time zones via `zoneinfo` (`Europe/Berlin`, `America/New_York`), never fixed offsets. Europe and the US switch DST on different dates (late Oct vs. early Nov, mid-Mar vs. late Mar).
9. **Host vs. container.** Project work happens inside the `trader` container. Commands on the host (Incus, firewall, partitions) only with Lorant's explicit go-ahead, and snapshot the container before any Wine, MT5 or OS upgrade.
10. **No lookahead.** A feature at bar *t* uses only bars ≤ *t*. A swing point exists only once its right-side bars have closed. Test this explicitly.

## Stack

Python 3.12 · FastAPI + uvicorn · pydantic v2 · SQLite (WAL mode) · pandas/numpy · httpx (OpenRouter and the Telegram Bot API) · `exchange_calendars` for holidays · pytest · ruff · systemd. Charts: mplfinance/matplotlib (PNG for Telegram). MT5 runs under WineHQ stable (pinned) on Xvfb with openbox; headless install, start config and units are in `deploy/mt5/`. Add nginx on 127.0.0.1:80 only if MT5 WebRequest rejects port 8000.

Suggested layout:

```
app/
  main.py          FastAPI app, routes
  config.py        settings from .env + config.yaml
  db.py            schema, migrations, upserts
  timeconv.py      MT5 server time → UTC
  sessions.py      session windows, holidays, DST
  features.py      Brooks mechanics (pure functions)
  htf.py           H1/D1 context, alignment
  llm.py           OpenRouter client, budget guard
  reader.py        builds prompt, calls LLM, validates
  validate.py      schema + sanity checks
  telegram.py      bot: long polling, commands, alerts, feedback buttons
  status.py        /status, health, risk (FTMO limits) and ops monitors
  charts.py        PNG charts for alerts and /chart
  outcomes.py      hypothetical outcome simulator
  scheduler.py     triggers (new bar, pre-session, daily report)
prompts/market_read_v1.md
config.yaml
data/signal.db     (git-ignored; backed up nightly, deploy/backup/)
tests/
```

## Trading context

- **Method:** Al Brooks price action on the 5-minute chart with EMA20. He prefers trading with the trend over counter-trend signals.
- **Instruments:** these are FTMO MT5 symbols; verify them in Market Watch and keep them configurable.
  - `GER40.cash` (DAX)
  - `UK100.cash` (FTSE)
  - `US100.cash` (Nasdaq)
  - `US30.cash` (Dow)
- **Sessions:** times are in exchange time and come from `config.yaml`.
  - **EU, 09:00–11:00 Europe/Berlin:** GER40 is the traded instrument; UK100 is context only.
  - **US, 09:30–11:30 America/New_York:** US100 and US30 are both candidates. He trades one per session.
- **His rules:**
  - max 3 trades per day
  - 30-minute cooldown after a winner
  - skip an instrument when H1 and D1 disagree
  - stop for the day once the daily target is hit

  The phase-2 EA enforces these. In phase 1, alerts must respect the H1/D1 rule, and the daily report counts the rest.
- **Risk:** FTMO resets the daily loss limit at midnight CE(S)T. On funded accounts, high-impact news blackouts apply (about ±2 min). This matters in phase 2, but record the news flags now.
- **Load:** no high-frequency anything. Expect a few hundred requests a day at most.

## MT5 server time — verify, don't assume

MT5 bar timestamps are broker server time, not UTC.

- **Payload:** the EA sends raw server times plus the *current* `server_utc_offset_sec` in every payload and heartbeat.
- **Historical bars:** the offset changes with DST, so older bars need a rule, not today's offset.
- **Config:** support `SERVER_TIME_MODE`:
  - `ny_plus_7`: server time = New York local + 7h. This is common among brokers.
  - `iana:<Zone>`: for example `iana:Europe/Prague`.
  - `fixed:<seconds>`
- **Determine FTMO's actual rule in task T2:** compare the heartbeat offset against each candidate, and re-check during the Oct 25 – Nov 1 gap.
- **Monitoring:** on every heartbeat, assert that the conversion rule matches the reported offset. Alert in Telegram on a mismatch.

## Phases

1. **Alerts only (this repo now).** Collect evidence and log the AI's call next to Lorant's own call (Telegram buttons).
2. **One-tap approve.** The EA gets the risk guard and executes only approved orders, with pre-set SL and TP.
3. **Full auto.** Only after several hundred logged signals show an edge on demo, *and* FTMO confirms in writing that the setup complies with their rules on AI tools.

## Conventions

- Small commits. Every feature function gets unit tests on hand-built bar fixtures, including no-lookahead tests.
- Prompts live in `prompts/` as versioned files (`_v1`, `_v2`…). The version is stored with every call.
- Model ID and provider order live in config, never hard-coded.
- Prefer boring, readable code over clever code. Lorant reads the code; he's a PHP/Laravel developer with a strong Linux background.
