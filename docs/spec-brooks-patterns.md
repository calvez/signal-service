# Brooks-inspired algorithmic price-action research specification

Status: proposed research specification, v0.1, 2026-10-04. No trading code or configuration changes are authorized by this document.

Source: Al Brooks, [10 best price action trading patterns](https://www.brookstradingcourse.com/price-action/10-best-price-action-trading-patterns/), published August 18, 2020; read October 4, 2026.

Source summary: The article discusses major trend reversals, final flags, breakouts, High 2/Low 2 flags, wedges, channels, measured moves, range reversals, opening reversals, and support/resistance magnets. Context matters, patterns can overlap, and some concepts describe location or targets rather than independent entries. It describes discretionary trading, not a fully specified algorithm. Its probability estimates are not validated performance estimates for this implementation.

Everything below is an original engineering proposal for testing these ideas. Thresholds, state machines, order policies, and acceptance criteria are design choices, not rules supplied by the article. This document supplements `spec-brooks-ea.md`; it does not replace that existing specification or claim the proposed detectors are already implemented.

## 1. Objective and scope

Build deterministic detectors whose signals can be replayed identically in historical and live evaluation. Test each entry family separately before combining them. Establish whether results remain positive after execution costs and outside the development period.

Use configured instruments and sessions, M5 for decisions, closed H1/D1 bars for optional context, and M1 or ticks for execution simulation. UK100 remains context only under the project guide. Reuse configured session calendars and time zones; do not hard-code broker rules or fixed UTC offsets.

Initial research excludes LLM selection, pyramiding, and discretionary intervention so detector performance can be attributed. Evaluate those separately later. This spec does not expand live execution permissions.

## 2. Common feature definitions

At decision time `t`, use only observations available at that time. A bar timestamp must explicitly identify its open; availability is its close. Higher-timeframe bars are available only after their close.

Proposed defaults:

| Feature | Definition |
|---|---|
| `A` | Mean high-low range of the 20 closed bars preceding the signal bar; exclude the signal bar |
| EMA | EMA20 of closed decision bars, with a recorded initialization rule and at least 100 warm-up bars |
| Strong bull signal | Positive range, close > open, body/range >= 0.50, close position >= 0.75, upper tail/range <= 0.15 |
| Strong bear signal | Price-reflected strong bull signal |
| Signal size | Range between 0.5A and 1.5A; breakout bars have a separate size rule |
| Pivot | Strict high or low relative to two bars on either side; ties create no pivot; available two bars after the pivot |
| Separation | Consecutive same-type retained pivots must differ by at least 0.5A; retain the more extreme point until an opposite pivot qualifies |
| Tolerance | `max(2 * tick_size, 0.15A)` |
| Near a level | Absolute price distance <= 0.30A |
| Efficiency | `abs(C[t]-C[t-20]) / sum(abs(diff(C[t-20:t])))`; zero denominator gives zero |
| Bull regime | C > EMA, EMA[t] > EMA[t-5], last two retained highs and lows both rising, efficiency >= 0.30 |
| Bear regime | Price-reflected bull regime |
| Range regime | Efficiency <= 0.25 and at least six of the last ten adjacent bar pairs overlap by >= half the smaller positive range |
| Transition | All other contexts; no entry unless the detector explicitly permits it |

Reject insufficient history, nonfinite prices, invalid OHLC, missing required spreads, or zero-range signals. Freeze pattern geometry when the setup is armed; later bars may invalidate it but cannot rewrite its historical boundaries. Record feature version and all parameter values. Existing H2 features differ from these proposed defaults; any change requires a new strategy version and a separate comparison.

## 3. Detector contracts

All directional examples below describe longs. Reflect price and high/low comparisons for shorts, but preserve actual bid/ask execution semantics. Each detector emits its geometry, causal evidence, entry, structural invalidation, and optional target. A formation alone never creates an order: require a qualifying closed signal and sufficient room to the target.

### 3.1 Major trend reversal

State sequence: `bear_regime -> structure_break -> retest -> reversal_signal`.

1. Establish bear regime with a known last lower high and lowest low.
2. A close exceeds that lower high by tolerance; save the broken level and old low.
3. Within 20 bars, a subsequent pullback low lies within 0.5A of the old low. Permit a lower low, but reject an excursion more than 1A below it.
4. A strong bull signal closes above the previous bar high within five bars of the retest. Arm entry above this signal; stop below the entire retest low.

Expire after 25 bars from the structure break. This detector permits transition context. Classify the retest as lower/equal/higher low for separate reporting.

### 3.2 Final flag failure

Require a preceding bear regime and a consolidation of 3–10 completed bars, height <= 2A and efficiency <= 0.25 over that consolidation. Freeze its boundaries before evaluating the next bar.

A downside excursion of 0.1A–1A below the flag must close back inside within three bars. Require a strong bull signal and a known support level within 0.5A of the excursion low. Stop below the excursion. Reject a flag width too small to support a net 2R target. This is a failed continuation detector; do not label every consolidation a final flag retrospectively.

### 3.3 Breakout and breakout pullback

Reference resistance is the maximum high of the preceding 20 bars, excluding the breakout bar. Breakout requires range >= 1.2A, body/range >= 0.65, close position >= 0.80, and close >= resistance + 0.15A.

Test two exclusive variants:

- Immediate: arm above the breakout bar, stop below it.
- Pullback: within ten bars, price revisits within 0.3A of resistance; reject any close below resistance - 0.3A. Arm above a strong bull signal and stop below the pullback low.

Do not require a pre-existing bull regime for immediate breakouts. Use the saved range height as one target candidate. Compare breakout-only versus range-regime breakout eligibility separately.

### 3.4 H2/L2 continuation

Use the existing `BrooksH2` detector as the baseline rather than creating a competing count definition. Bull context, a bounded pullback, a failed first continuation attempt, and a strong new signal are required.

Maintain causal states `trend -> pullback_armed -> first_attempt_triggered -> renewed_pullback -> second_signal`. The pending stop above the signal bar is the second attempt; do not require that the signal bar already triggered H2. Equal highs do not count as a stop-order trigger unless the configured order price was crossed. Expire if the pullback exceeds ten bars or breaks the saved higher low. Test signal-bar versus pullback-extreme stops independently.

### 3.5 Wedge reversal / wedge continuation

Require three retained lows, separated by qualifying opposite pivots, within 30 bars. Each low must be within tolerance of or below the prior low; total first-to-third decline >= A. The third low must already be confirmed before a signal can be emitted.

Arm above a strong bull signal within five bars of third-low confirmation; stop below the lowest of the three lows. Label as continuation only when the formation is a pullback within saved bull context and does not break its higher low. Otherwise require a support magnet within 0.5A and label as reversal. Report converging and nonconverging geometry separately; do not require future confirmation of a fourth push.

### 3.6 Channel reversal / failed channel break

Fit ordinary least squares to closes of the preceding 30 bars. Bound the fitted line by the minimum low residual and maximum high residual. Freeze the projected boundaries at setup creation. Require positive slope and a total fitted rise >= 2A for a bull channel.

Long continuation: low reaches the projected lower boundary within tolerance, closes above it, and forms a strong bull signal. Stop below the test low.

Short failed breakout: high exceeds the upper boundary by >= 0.15A and a strong bear signal closes back inside within three bars. Stop above the excursion. Implement bear-channel reflections separately. Regression channels are a proposed reproducible proxy for discretionary lines.

### 3.7 Measured moves

Provide target features, not a standalone entry strategy:

- Range projection: saved upper boundary + saved range height for a long breakout.
- Leg projection: after confirmed low `P0`, high `P1`, and higher low `P2`, project `P2 + (P1-P0)`.

A projection becomes available only after all required pivots are confirmed. Save its origin, availability timestamp, and invalidation conditions. Compare fixed 2R exits against projection exits; never select a target retrospectively from the realized move.

### 3.8 Trading-range reversal

Require range regime. Freeze the preceding 30-bar high and low; require width >= 3A. For a long, price tests the lower boundary within 0.2A or sweeps below by at most 0.5A, then a strong bull signal closes inside the range. Stop below the excursion.

Target range midpoint initially; enter only when net reward/risk >= 2. Test a second-signal variant that waits for another boundary test within ten bars without a close more than 0.5A below the boundary. Expire on a decisive outside close. No entries from the middle 50% of the range.

### 3.9 Opening reversal

Use elapsed time from the configured exchange cash open, not from a data-file boundary. During the first 90 minutes, require a directional excursion >= 1.5A within at most six bars toward a level known before the excursion.

Long reversal: excursion reaches support within 0.3A; a strong bull signal closes above the preceding bar high within three bars. Stop below the opening excursion low. Eligible magnets are prior completed session low/close and a pre-open confirmed H1 pivot. Existing session order cutoffs still apply. Log gap direction separately; do not require a gap in the baseline.

### 3.10 Magnets

Maintain a causal level registry: prior completed session high/low/close, current session open and running extremes, confirmed pivots, frozen range/channel boundaries, EMA, and measured-move targets.

Store each level's origin and availability time. Merge levels within tolerance for scoring, retaining constituent evidence. Magnets supply location, room-to-target, and confluence features; touching a level alone is never an entry. Test confluence as an ablation rather than assuming more overlapping patterns improve results.

## 4. Orders and portfolio rules

Default research execution: one position per symbol, no add-ons, fixed 2R target, risk budget 0.25% of starting-day equity per entry, maximum three filled campaigns per configured trading day. These are research defaults, not changes to existing account configuration.

For bid OHLC and spread `s`, long buy-stop is signal high + tick + placement spread; long stop is structural bid low - tick. Short sell-stop is signal low - tick; convert structural bid high to an ask stop using placement spread plus tick. Trigger buy orders and short exits on ask, sells and long exits on bid. Actual spread can change between placement and fill.

Round prices outward to true broker tick size, not merely decimal point size. Size using stop distance / tick size * tick value, currency conversion, commission and anticipated slippage. Round volume down to lot step. Reject nonpositive risk, invalid broker distances, unaffordable margin, or volume below minimum. Reject entries whose planned reward/risk after costs is below 2; never widen risk after fill to restore the ratio.

Orders are eligible only after signal close and expire after one M5 bar. Cancel on structural invalidation, session cutoff, stale data, or risk lock. A pending order reserves portfolio risk. Keep existing configured daily equity guard and flatten schedule; losses include floating P&L and costs. External account restrictions must be supplied through verified account configuration rather than inferred from this article.

If multiple signals compete, collapse same-symbol/direction candidates with matching geometry into one order carrying multiple labels. Reject opposite-direction conflicts in the baseline. Choose remaining signals by a fixed configured priority, then symbol name for a tie; reserve risk before subsequent candidates. Log rejected alternatives. Evaluate every family independently before using any priority portfolio.

## 5. Exits and experiment matrix

Compare one change at a time:

| Variant | Exit / management |
|---|---|
| A | Fixed 2R, structural initial stop, no breakeven |
| B | Available measured-move or range target, minimum net 2R |
| C | No fixed TP; after +1R, trail to newly confirmed protective pivots; never loosen |
| D | Existing H2 campaign engine, including its configured breakeven, scratch exit and pyramiding |

All variants flatten at the configured deadline. Pivot trailing becomes effective after confirmation, never at the pivot's historical timestamp. Apply close-based exits at the next executable quote with latency/cost assumptions, not the already-observed bar close. Record gap exits at obtainable prices. Report target reached, realized winner, and final campaign R separately.

## 6. Repository integration and gaps

- `app/features.py`: causal feature calculations and state evidence.
- `app/strategies/`: independently versioned detectors implementing `candidates(Evaluation)`.
- `Candidate.evidence`: pattern ID, frozen levels, pivot availability, state transitions, filter outcomes and source parameters. Extend allowed setup labels only with matching schema/validator changes.
- `app/evaluation.py`: shared closed-bar and higher-timeframe availability rules.
- `app/backtest_campaign.py`: shared execution driver and portfolio scheduling.
- `app/campaign.py`: execution and management, without detector-specific pattern logic.

Current H2 support is a starting point, not coverage of all ten concepts. The backtester currently supports M1 simulation with an M5 fallback, pessimistic same-minute order/stop ordering, and gap fills. Its documented default omits commission and nongap slippage. A decimal `point` also must not be assumed to equal every symbol's tick size. Track these limitations in every run until execution metadata and cost modeling are implemented.

For ambiguous intrabar order events, record an ambiguity flag and pessimistic result; additionally report an optimistic bound. Do not invent a unique price path from OHLC. Detect missing M1 intervals; distinguish a complete minute path from partial coverage and coarse M5 fallback. Portfolio equity guard checks must use synchronized marks across symbols, including periods with no strategy signals.

## 7. Validation and acceptance

Required detector fixtures: valid long, reflected short, near miss, insufficient history, equal pivot values, expiry, invalidation, overlapping labels, and exact threshold boundaries. Required execution fixtures: spread expansion, gaps, same-bar trigger/stop, cancellation versus trigger, tick/point mismatch, costs, order expiry, portfolio cap, and session/DST boundaries.

No-lookahead invariant: evaluating any prefix must reproduce the features, candidates, and orders emitted over that prefix in a longer replay. Adding future data cannot alter historical pivots' availability, channel geometry, levels, or decisions. Re-run identical data/configuration and require identical output.

Use chronological development/validation/untouched test partitions (proposed 60/20/20), embargo overlapping campaigns at boundaries, and walk-forward evaluation within development/validation. Fit thresholds only there. Record data checksums, code revision, parameter hash and all trials; any retuning after viewing test results requires a new untouched period.

Report by pattern, instrument, session, direction and regime: signal counts, fills/cancellations, net expectancy in initial-risk R, profit factor, exposure, MAE/MFE, equity drawdown, worst day, costs, and ambiguous-fill proportion. Bootstrap uncertainty by trading day to preserve within-day dependence. Include rejected signals and aggregate campaign rather than ticket returns.

Proposed research gate: at least 200 out-of-sample filled campaigns per selected family; positive net expectancy with a positive lower 95% day-bootstrap bound; positive expectancy under doubled spread/slippage assumptions; and drawdown/worst-day performance within configured risk budgets. Insufficient samples means inconclusive, not a passed edge. These gates are selection criteria, not profitability guarantees.

## 8. Delivery order

1. Freeze and reproduce the existing H2 baseline, including its known execution limitations.
2. Add causal level registry, explicit tick/value metadata, cost assumptions, and coverage flags.
3. Add breakout/pullback and range-reversal detectors with independent reports.
4. Add wedge and opening-reversal detectors.
5. Add structure-reversal, final-flag and channel proxies.
6. Compare target/management variants, then combinations, then optional LLM selection and pyramiding.

Each stage must retain its independently versioned baseline and produce a reviewable report before advancing. Implementation of this proposal is a separate task.
