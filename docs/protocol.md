# Protocol

All requests are JSON over HTTP on 127.0.0.1 (MT5 runs in the same container). All endpoints except `/health` require `Authorization: Bearer <INGEST_TOKEN>`.

## 1. `POST /v1/bars` — closed bars from the EA

```json
{
  "schema": 1,
  "source": "mt5",
  "account_login": 1234567,
  "server": "FTMO-Demo",
  "symbol": "GER40.cash",
  "timeframe": "M5",
  "digits": 2,
  "server_utc_offset_sec": 10800,
  "bars": [
    {"t": 1759480200, "o": 24310.5, "h": 24322.0, "l": 24301.2, "c": 24318.7, "tv": 1834, "sp": 120}
  ]
}
```

- `timeframe` is one of `M5`, `H1`, `D1`.
- `t` is the bar **open** time in MT5 **server time**, as a raw epoch (a naive server clock read as if it were UTC). Convert it with `timeconv.py`.
- Only **closed** bars are sent. A bar can arrive again after a backfill or retry, so upsert on `(symbol, timeframe, t)`, which makes the endpoint idempotent.
- `tv` is tick volume and `sp` is spread in points.
- A backfill sends up to 500 bars per request.
- Response: `200 {"accepted": <n>}`. Anything other than 200 makes the EA retry later from the same point.

## 2. `POST /v1/heartbeat` — every 60 s

```json
{
  "schema": 1,
  "ea_version": "1.00",
  "account_login": 1234567,
  "server": "FTMO-Demo",
  "company": "FTMO S.R.O.",
  "balance": 80000.0,
  "equity": 80000.0,
  "connected": true,
  "trade_allowed": false,
  "positions": 1,
  "floating_pl": -22.40,
  "currency": "EUR",
  "time_server": 1759484100,
  "server_utc_offset_sec": 10800
}
```

Store every heartbeat (they drive `/status`, the risk warnings and the ops alerts in `docs/telegram.md` §3–4). `positions` and `floating_pl` are read-only summaries; the EA has no order code.

## 3. `GET /health`

`200 {"ok": true, "last_bar_utc": {...per symbol/tf...}, "last_heartbeat_utc": "..."}`. Unauthenticated; the service binds to 127.0.0.1 only.

## 4. LLM market read — response schema

The model must return **only** this JSON object. Request JSON output from OpenRouter where the provider supports it, and always parse and validate regardless.

```json
{
  "schema": 1,
  "symbol": "GER40.cash",
  "bar_time_utc": "2026-10-05T07:25:00Z",
  "context": {
    "htf_alignment": "aligned_bull | aligned_bear | conflict",
    "day_type": "trend_from_open | spike_and_channel | trading_range | broad_channel | tight_channel | unclear",
    "always_in": "long | short | neutral"
  },
  "action": "none | watch | alert",
  "setup": null,
  "reason": "max 300 chars, plain language"
}
```

When `action` is `alert` or `watch`, `setup` is:

```json
{
  "direction": "long | short",
  "type": "H1 | H2 | L1 | L2 | wedge | failed_breakout | breakout_pullback | double_bottom | double_top | other",
  "with_trend": true,
  "entry_type": "stop",
  "entry": 24325.0,
  "stop": 24298.0,
  "target": 24379.0,
  "grade": "A | B"
}
```

### Validation (`validate.py`)

The service applies these rules. If any rule fails, force `action = none` and log the reason.

1. The JSON parses and matches the schema exactly. Unknown enum values are a failure.
2. `symbol` and `bar_time_utc` equal the request's values. This catches stale or mixed-up responses.
3. If the deterministic HTF check says `conflict`, the action is `none`, whatever the model says. This is his H1/D1 rule.
4. Price order: long requires `stop < entry < target`; short requires `target < entry < stop`.
5. Stop distance `|entry − stop|` falls between 0.3 × ATR14(M5) and 3.0 × ATR14(M5).
6. Entry is within 1.0 × ATR14(M5) of the last close.
7. Reward to risk: `|target − entry| / |entry − stop| ≥ 1.0`.
8. Counter-trend (`with_trend: false`) is allowed only when the deterministic `day_type` hint is a trading range *and* price is in the top or bottom 20% of today's range. Otherwise it's downgraded to `watch`.
9. Only `grade: A` produces a push alert. `B` is logged and shown as a silent message, or skipped, depending on config.
10. Prices are rounded to the symbol's digits.

## 5. Telegram alert format

Full bot behaviour (commands, status, risk and ops alerts) is in `docs/telegram.md`. The alert itself:

```
🟢 GER40 LONG · H2 · A          (07:25 UTC / 09:25 Berlin)
Context: bull trend from open, always-in long, H1/D1 aligned bull
Entry  24325.0 (buy stop)
Stop   24298.0  (27.0 pts, 0.9 ATR)
Target 24379.0  (2.0R)
Why: <reason>
Model: <model id> · prompt v1 · read #1234
[ I'd take it ]  [ Skip ]  [ Unsure ]
```

The buttons write a `feedback` row (`read_id`, `choice`, timestamp). The daily report compares the AI's calls, his choices and the simulated outcomes.

## 6. Phase 2 (not now)

These will be added later: `GET /v1/orders/pending` and `POST /v1/orders/{id}/ack`. The EA polls for approved orders and enforces its own risk guard before any `OrderSend`. Don't build them in phase 1.
