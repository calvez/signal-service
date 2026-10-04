# GER40 (DAX) backtest report — 2026-10-04

Strategy: Lorant's Brooks spec (`docs/spec-brooks-ea.md`), campaign engine, M1 fills, net of spread.
Data: FTMO history, GER40.cash M1/M5/H1/D1, 2019-03-01 to 2026-10-02 (29,400 session bars).
Risk per campaign 0.3 % of the balance = 1R. H1/D1 rule off; 60-minute EMA20 in the trend filter.
All numbers are SIMULATED.

## 1. Bottom line

**The spec as written produces almost no trades on DAX: 5 signals in 7.5 years.** The result cannot
say anything about profitability (n = 4 entered trades). It does show that the strategy as specified
cannot reach 2–3 trades per session, and that loosening the filters alone does not get there either.

| | Value |
|---|---|
| Signals / entered | 5 / 4 (1 cancelled: SB low broken) |
| Expectancy | +0.39 R per campaign |
| Total | +1.57 R = +0.47 % of the account |
| Win rate | 75 % (3 of 4) — meaningless at n = 4 |
| Profit factor | 3.19 |
| Reached 2R | 25 % (1 of 4) |
| Best / worst | +1.42 R / −0.72 R |
| Max drawdown / worst day | 0.22 % / −0.22 % (FTMO limits −10 % / −5 %: never close) |
| Pyramiding | 0 adds — never triggered |
| Exits | EOS 2, TRAIL 1, ALWAYS_IN_FLIP 1 |
| In-sample (before 2024) | **0 signals in 5 years** |
| Out-of-sample (2024 on) | 5 signals, all of them |

**Target check:** 0.5–1 % per day needs about +1.7 to +3.3 R per day. This backtest made +1.57 R in
total over 7.5 years.

## 2. The bug found on the way

The H2 test required the signal bar to *be* the H2 bar. Brooks places the buy stop above a bar while
the second leg is still armed, and the bar that fills it *becomes* the H2. The old code was therefore
looking for H3 entries. Fixed (commit 4c86d9d, with tests). Effect on DAX: small (3 → 4 setups in 21
months), so it was necessary but not the main cause.

## 3. Why so few: the filters multiply

DAX, Jan 2025 – Sep 2026: 13,318 bar-directions inside the trading window. Pass rate of each rule on
its own:

| Rule | Passes | Rule | Passes |
|---|---|---|---|
| H2 setup state | 5.5 % | not a trading range | **19.4 %** |
| §3 signal-bar quality | 10.9 % | trend (both EMAs) | 26.9 % |
| pullback depth | 44.5 % | room to 2R | 93.9 % |
| no always-in flip | 98.0 % | | |

Product if independent: 0.013 % = about 2 setups. Actual: 4. So the rules are roughly independent;
there is simply no slack.

Two interactions worth knowing:
- **Setup vs signal bar conflict.** Where the H2 state holds, only 6.1 % of bars also have a §3 signal
  bar (against 10.9 % alone). An armed pullback means the bar did not break the previous high, while
  §3 wants a strong bull bar closing near its high. A strong bullish *inside* bar is a rare shape.
- **Trading-range filter is the strictest rule** (19.4 % pass) and it is my interpretation of an
  ambiguous sentence: "overlap the prior bar by more than 50 %" — I measured overlap against the bar's
  own range. On 5-minute bars most trending bars overlap their predecessor by more than half, so this
  rejects genuine trends.

## 4. Sweep: what loosening does (DAX, same 10,651 session bars, 444 sessions)

| Variant | Setups | Per session |
|---|---|---|
| spec as written | 4 | 0.01 |
| softer signal bar (body 0.4, close 0.6, tail 0.3) | 5 | 0.01 |
| also allow H1 entries | 6 | 0.01 |
| no 2R room rule | 4 | 0.01 |
| shorter EMA slope window | 4 | 0.01 |
| **trading-range filter 6 → 8 of 10** | **14** | **0.03** |
| all of the above together | 51 | **0.11** |

Everything together reaches 0.11 setups per session. The target is 2–3. **That is 20 times short.**
No tuning of this strategy's thresholds closes that gap.

## 5. What this means

1. The spec is a *selective* strategy: one A-grade H2 pullback entry every ~10 sessions on DAX. That is
   a legitimate style, but it is not "2–3 trades per session".
2. To trade 2–3 times per session, the strategy needs **more kinds of entry**, not looser thresholds:
   with-trend signal bars after any pullback, breakout-pullbacks, second entries, failed-breakout
   reversals, opening-range breaks. That is a strategy decision for Lorant.
3. Even the loosened variant (51 setups in 21 months) has not been tested for profitability.
4. The out-of-sample logic needs far more trades before any conclusion: roughly 100 per variant.

## 6. Decisions needed from Lorant

1. **Which additional setups should count as entries** (point 2 above)? Describe them in your words.
2. **Trading-range filter:** how should "overlap" be measured? My suggestion: count only dojis and
   overlap above 75 % of the *smaller* of the two bars, and require 7 of 10.
3. **Are 2–3 trades per session a hard target, or a hope?** If trade count matters more than
   selectivity, the system becomes a different strategy and the expectancy will be lower per trade.
4. The 60-minute EMA filter changed 25 → 23 signals on three symbols with no measurable effect. Keep it?

## 7. Not measured here

LLM trade management (built, tested; the DAX backtest with the AI advisor has not run — with 4 trades
there is nothing for it to manage), live execution on the demo account (EA module compiled, not
deployed), pyramiding (never triggered), news handling (out of scope per spec §11).

## 8. Earlier multi-symbol runs (before the H2 fix; GER40 + US100 + US30, 2019–2026)

| Variant | Entered | Expectancy | Total | Adds |
|---|---|---|---|---|
| A trail + pyramid | 20 | −0.02 R | −0.11 % | 0 |
| B trail, no pyramid | 20 | −0.02 R | −0.11 % | — |
| C fixed 2R | 20 | −0.25 R | −1.51 % | — |
| A2 + two-EMA filter | 18 | −0.00 R | −0.01 % | 0 |

Fixed 2R (C) is clearly worse than trailing (A/B): in this small sample the 2R target is rarely
reached (reached_2r 0–5 %), so trailing helped. Sample too small to rely on.
