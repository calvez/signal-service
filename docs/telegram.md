# Telegram — the only interface

There is no GUI and no web dashboard. Everything Lorant needs to see or do goes through one Telegram bot in one private chat.

- **Transport:** long polling (`getUpdates`) via httpx. No webhook, so nothing on the server is exposed.
- **Who can talk to it:** only `TELEGRAM_CHAT_ID`. Messages from anyone else are ignored and logged.
- **No trading commands.** Phase 1 has no command that can open, modify or close a position.
- **Times** are shown in Budapest time (Europe/Budapest), with the exchange time in brackets where it matters.
- **Rate:** at most ~1 message per 5-minute bar per symbol, plus ops alerts. Coalesce duplicates; an ops problem is announced once, then once when it clears.

## 1. Push messages

| Type | When | Sound |
|---|---|---|
| **Trade alert** | Validated `alert`, grade A (`docs/protocol.md` §5), with a chart PNG | yes |
| **Watch** | Validated `watch`, or grade B | silent |
| **Pre-session brief** | 15 min before each session (config) | silent |
| **Session wrap** | When each session window ends: reads, alerts, his feedback, hypothetical R so far | silent |
| **Daily report** | 18:00 Berlin | silent |
| **Position change** | Heartbeat shows positions count or floating P/L sign change (he trades manually) | yes |
| **Risk warning** | See §3 | yes |
| **Ops alert** | See §4 | yes, except quiet hours |
| **Ops recovered** | When a problem clears | silent |

### Chart image
Every trade alert and watch carries a PNG rendered server-side (mplfinance/matplotlib): last ~60 M5 bars, EMA20, opening range, today's high/low, and for setups the entry, stop and target lines. Keep it readable on a phone: 1080×1350, dark background, large labels.

### Pre-session brief (example)
```
🌅 EU session in 15 min (09:00 Berlin)
GER40  H1 bull · D1 bull → aligned bull · prev day 24,180–24,415 · gap +38
UK100  context only · H1 bear · D1 neutral
News: none in window
LLM: kimi-… · prompt v1 · spend today $0.00 / $3.00
MT5: connected · heartbeat 12 s ago · balance 160,000.00 EUR
```

## 2. Commands

| Command | Does |
|---|---|
| `/status` | One-screen health and account view (example below) |
| `/today` | Today's reads, alerts, feedback, hypothetical results, LLM spend |
| `/brief [eu\|us]` | Pre-session brief on demand |
| `/chart <SYMBOL> [M5\|H1\|D1]` | Chart PNG, e.g. `/chart GER40` (short names map to config symbols) |
| `/screenshot` | PNG of the MT5 virtual display, for when MT5 is stuck on a dialog |
| `/pause [minutes]` | Mute trade alerts and watches (ops and risk alerts keep coming). Default until next session |
| `/resume` | Unmute |
| `/restart_mt5` | Restart the `mt5-terminal` service after a confirm button. **ASK** before building (needs a narrow sudoers rule) |
| `/help` | List of commands |

Unknown commands get the help text. Every command and its result is logged in `events`.

### `/status` (example)
```
🟢 All good · 21:14 Budapest
MT5        connected · heartbeat 8 s ago · EA 1.00
Data       GER40 M5 21:10 ✓ · US100 M5 21:10 ✓ · US30 M5 21:10 ✓ · UK100 M5 21:10 ✓
Account    balance 80,412.50 · equity 80,390.10 EUR · 1 position (−22.40)
Today      +412.50 closed · −22.40 open
FTMO       daily loss used 0% of 8,000 · max loss used 0% of 16,000
Signals    3 reads · 1 alert · 0 watch · paused: no
LLM        $0.42 / $3.00 today · last call 21:10 OK (2.8 s)
Next       EU session Mon 09:00 Berlin
```
The header turns 🟡 for warnings and 🔴 when MT5 is down, disconnected or data is stale.

## 3. Risk warnings (approximate — FTMO's own dashboard is authoritative)

Configured limits (2-step defaults; **ASK** Lorant to confirm against his account's objectives):
- `daily_loss_pct: 5` — measured from the balance at **midnight Europe/Prague**. Store that balance from the first heartbeat after midnight; equity (incl. floating) below `day_start_balance − limit` breaches.
- `max_loss_pct: 10` — from the initial balance (`initial_balance: 160000`).

Warn once per level per day when the used share of either limit crosses **50 %** and **80 %**. Also warn when positions are open within 10 minutes of a configured high-impact news time, and when positions are still open at 21:45 Berlin on a Friday.

## 4. Ops alerts

| Condition | Alert |
|---|---|
| No heartbeat for 3 min while any market session is open (10 min otherwise) | 🔴 MT5 not reporting |
| Heartbeat says `connected: false` for 2 heartbeats | 🔴 MT5 disconnected from broker |
| Latest M5 bar for a traded symbol older than 2 bar periods during a session | 🟡 Data stale for SYMBOL |
| Server-time rule doesn't match the reported offset | 🟡 Time check mismatch |
| LLM budget exceeded | 🟡 LLM paused for today |
| 3 consecutive LLM errors or validation failures | 🟡 LLM failing (last error …) |
| Service start | 🟢 signal-service started (version, git commit) |
| Disk < 10 % free | 🟡 Disk low |

Quiet hours (22:00–07:00 Budapest, config): ops alerts are silent **unless** positions are open.
