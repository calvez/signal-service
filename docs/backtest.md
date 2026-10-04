# Python evaluation, backtests and the LLM recommendation

The market evaluation is done in Python. The LLM only makes the recommendation on top of it.

```
closed M5 bar ─▶ app/evaluation.py  (features, day context, H1/D1 trend, 60-min EMA, H1/D1 gate)
             ─▶ strategy.candidates(ev)   (app/strategies: entry, stop, target + evidence)
             ─▶ validate.check_setup      (price order, stop in ATR, entry near close, R:R, ...)
             ├─▶ backtest: outcomes.simulate on the FOLLOWING bars (M1), net of spread
             └─▶ live:     LLM gets the evaluation + candidates (prompt v3)
                           and answers take / watch / skip (+ grade, reason).
                           Prices always come from Python.
```

The live reader and the backtester call **the same** `evaluate_bar`, with the same windows (600 M5 / 300 H1 / 150 D1 bars). A backtest therefore measures exactly the code that runs live. Every decision at bar *t* sees only bars that had closed by *t*'s close; this is tested.

## 1. History data

- Exported once from MT5 with `deploy/mt5/history-dump.sh` (script `mt5/HistoryDump.mq5`), outside trading hours. It restarts the terminal twice and puts everything back.
- Imported with `scripts/import_history.py` into `data/history.db` (same `bars` schema as live, kept separate: it is large and can always be downloaded again; it is not in the nightly backup).
- What FTMO delivered on 2026-10-04 (server time, to Friday 2026-10-02):

| Symbol | M1 | M5 | H1 | D1 | from |
|---|---|---|---|---|---|
| GER40.cash | 1.63 M | 333 k | 32 k | 2,096 | 2017-12-28 |
| UK100.cash | 1.70 M | 350 k | 34 k | 2,268 | 2017-12-28 |
| US100.cash | 1.77 M | 359 k | 34 k | 2,264 | 2017-12-29 |
| US30.cash | 2.64 M | 528 k | 44 k | 1,957 | 2019-02-08 |

- Times are converted with `server_time_mode` (`ny_plus_7`). If the Oct 25 – Nov 1 check shows a different rule, re-run the import.
- To refresh later (e.g. monthly): run both scripts again; bars are upserted.

## 2. Writing a strategy

A strategy is a small class in `app/strategies/` that returns `Candidate`s for one `Evaluation`. `ev.last` is the evaluated bar with all feature columns (`ema`, `atr`, `bar_type`, `sig_long`, `h_count`, `h_bar`, `last_sl_price`, ...), `ev.ctx` the day context, `ev.alignment` the H1/D1 result, `ev.h1_ema` the 60-minute EMA.

```python
from app.strategies import Candidate


class H2Pullback:
    name = "h2_pullback"
    version = "1"  # bump on every rule change; stored with every signal

    def candidates(self, ev):
        bar = ev.last
        if ev.alignment != "aligned_bull" or not bar["h_bar"] or bar["h_count"] != 2:
            return []
        tick = 10**-ev.digits
        entry, stop = bar["h"] + tick, bar["l"] - tick
        return [
            Candidate(
                "H2",
                "long",
                entry,
                stop,
                entry + 2 * (entry - stop),
                True,
                evidence={"h_count": 2, "close_vs_ema": round(bar["c"] - bar["ema"], 1)},
            )
        ]
```

Register it in `get_strategy()` (`app/strategies/__init__.py`) and add unit tests on hand-built bars, like the features.
The example above is only an illustration of the interface, not a chosen strategy. `demo` exists only to test the plumbing.

## 3. Running a backtest

```bash
cd /opt/signal-service
sudo -u signal .venv/bin/python scripts/backtest.py --strategy h2_pullback \
    --from 2018-01-01 --to 2026-10-01 --split 2024-01-01 \
    [--symbols GER40.cash,US100.cash] [--all-signals] [--no-m1]
```

- **Default: his trade rules** — one position at a time per symbol, at most `rules.max_trades_per_day` entries per session day, `rules.cooldown_after_win_min` pause after a winner. `--all-signals` simulates every candidate on its own (the raw edge of the rule).
- Only bars inside the symbol's session window are evaluated (EU 09:00–11:00 Berlin for GER40, US 09:30–11:30 New York for US100/US30), holidays excluded, H1/D1 conflicts skipped — the same as live.
- Outcomes on **M1** bars (five times fewer "stop and target in the same bar" cases, which still count as a loss). Expiry at the cash close.
- **Net of spread**: the spread MT5 recorded on the signal bar, in R. Not modelled: slippage, commission, partial fills, news spikes.
- Report: overall, per setup, per symbol and — with `--split` — in-sample vs. out-of-sample. All signals go to `data/backtests/<strategy>.csv`.

Reading the numbers: choose rules on the in-sample period only and look at the out-of-sample result once at the end. A rule that only works in-sample is curve-fitted. Few trades (< ~100) say little.

## 4. Switching the live system to "Python evaluates, LLM recommends"

In `/opt/signal-service/config.yaml`:

```yaml
engine:
  strategy: "h2_pullback"
  prompt_version: "v3"
```

then `systemctl restart signal-service`. From then on:
- A bar with no valid candidate is **not** sent to the LLM (no cost, no alert).
- Otherwise the LLM gets prompt v3 (`prompts/market_read_v3.md`): the whole evaluation, the 60-minute EMA and the numbered candidates. It answers `take | watch | skip` with `candidate_id`, `grade` and `reason` (schema 2, `validate.validate_recommendation`). It cannot change prices or invent setups.
- Alerts, buttons, outcomes and reports work as before; the stored setup also carries `strategy`, `candidate_id` and `evidence`.

With `strategy: ""` (the default) the LLM reads the chart itself with `llm.prompt_version` (v2), as before.
