<!-- Lorant's specification, pasted on 2026-10-04. Kept verbatim as the reference.
     Implementation notes and the defaults chosen for gaps are in docs/strategy.md. -->

# EA spec — Brooks price action, stop-order entries

**Platform:** MetaTrader 5 · **Broker:** FTMO (2-step, €80k, hedging account)
**Symbols and session windows:** as defined in the signal-service spec
**Timeframe:** M5 (input)

All rules are written for longs. Shorts are the exact mirror image (high ↔ low, above ↔ below, bull ↔ bear, buy stop ↔ sell stop). All numbers in parentheses are default values to expose as EA inputs and tune in the backtest.

**Trade model in one sentence:** enter on a stop order above a strong signal bar in a trend, aim for at least 2R, but use no fixed target — keep adding at pullbacks and trailing the stop until the session ends or the setup is invalidated.

---

## 1. Definitions

| Term | Meaning |
|---|---|
| `SB` | Signal bar: the last closed bar when a setup is detected |
| `tick` | Symbol tick size |
| `Range(bar)` | `High − Low` |
| `Body(bar)` | `abs(Close − Open)` |
| `AvgRange` | Mean `Range` of the last `AvgRangeBars` (20) closed bars |
| `Entry` | `SB.High + 1 tick + spread` (buy stop) |
| `SL` | `SB.Low − 1 tick` |
| `R` | Initial risk: `Entry − SL`, in price and in money |
| Campaign | Initial position plus all its add-ons, sharing one `CampaignID` |

---

## 2. Context filter (proxy for "≥ 60% probability")

Brooks' probability estimate is a judgment of trend, location and signal bar together. The EA approximates it with the filter below. The real win rate per setup type is measured in the backtest.

A long setup is allowed only if **all** are true:

- **Trend:** close above EMA20, EMA20 rising over the last `EmaSlopeBars` (5) bars, last swing high higher than the previous swing high.
- **Setup type:** H2 pullback to around EMA20. H1 is added only after it has been tested separately (input `AllowH1`, default false).
- **Pullback depth:** no close below the last higher low; pullback lasts at most `MaxPullbackBars` (10) bars.
- **Not a trading range:** fewer than `RangeOverlapCount` (6) of the last `RangeLookback` (10) bars overlap the prior bar by more than 50% or are dojis.
- **No always-in flip:** fewer than `ConsecOppBars` (3) consecutive bear bars each closing in the bottom 25% of its range.
- **Room to target:** no prior swing high or session extreme within `MinTargetR × R` (2.0) above entry.

---

## 3. Signal bar (bull)

All must be true:

- Bull bar: `Close > Open`
- Body: `Body / Range ≥ 0.5`
- Closes near its high: `(Close − Low) / Range ≥ 0.75`
- Minimal upper tail: `(High − Close) / Range ≤ 0.15`
- Lower tail allowed (a reversal tail below is a plus)
- Closes above the prior bar's close
- Size: `MinSBRange` (points) `≤ Range ≤ 1.5 × AvgRange`
- Spread: `Spread ≤ 0.15 × Range`

---

## 4. Entry orders

- **Never enter at market.** Every entry is a pending stop order.
  - Long: `BuyStop = SB.High + 1 tick + spread` (MT5 triggers buy stops on Ask while the chart shows Bid; the spread makes the fill happen only when Bid breaks the high).
  - Short: `SellStop = SB.Low − 1 tick`.
- Place the order immediately on the close of `SB`, with the SL attached server-side. No TP.
- Order lifetime: `MaxPendingBars` (1). If unfilled at the close of the following bar, cancel.
- One campaign per symbol at a time. Add-ons only per section 8.
- Max `MaxCampaignsPerDay` (3) campaigns per day across all symbols.

### Timing

- Session windows per the signal-service spec.
- No new signals in the first `SkipOpenBars` (3) bars of the session.
- No new orders (initial or add-on) in the last `NoNewOrderMins` (30) minutes of the session.

---

## 5. Setup invalidation

### 5.1 Before entry: cancel the pending order

- Price trades `≤ SB.Low − 1 tick` before the buy stop fills → cancel, mark the setup dead.
- Not filled within `MaxPendingBars` → cancel.
- Never re-place an order at the old price. A new setup requires a new signal bar.

### 5.2 After entry: failed setup

- Hard stop at `SL`, server-side, placed with the order.
- A bar closes below `SB.Low` or the pullback low → set `FailedLong = true`. Optional: arm a short setup (a failed H2 often becomes a sell signal), input `TradeFailedSetups` (false).
- Optional early exit (`EarlyExitOnStrongOpp`, false): the bar after entry is a strong bear bar (`Body ≥ 0.6 × Range`, close in bottom 25%) → exit at its close.

### 5.3 Context invalidation: block new longs and close the campaign

- Always-in flip (section 2 rule): close the whole campaign.
- Close below the last higher low: close the whole campaign (the trailing stop normally handles this).
- Trading range filter triggers: no new entries or adds; open positions stay on the trailing stop.

---

## 6. Initial stop and target

- `SL = SB.Low − 1 tick`
- **No fixed TP.** 2R (`MinTargetR`) is the planning target: it is used for the room-to-target check (section 2) and in alerts, but no TP order is placed.
- The campaign ends only by trailing stop, invalidation (section 5) or session end (section 7).
- Position size from `RiskPerTradePct` of balance and the `Entry − SL` distance, rounded down to the broker's lot step. Skip the trade if below the minimum lot.

---

## 7. Trade management and exits

| Rule | Behavior | Input (default) |
|---|---|---|
| Breakeven | At `+BEAtR × R`, move SL to entry + spread + commission. Test on/off; too early turns winners into scratches. | `BEAtR` (1.0), `UseBE` (true) |
| Scratch / time exit | Not at +0.5R within `MaxBarsInTrade` bars → close at market. | `MaxBarsInTrade` (6) |
| Trailing | After +1R, trail below the last confirmed swing low, not bar by bar (details in 8.4). | always on |
| End of session | Close all positions and cancel pending orders `FlattenMins` before the session window ends. No overnight holds. | `FlattenMins` (15) |
| Daily loss guard | Internal daily limit hit (on equity, incl. floating P&L) → close all, lock EA until next trading day. | `DailyLossLimitPct` (below FTMO's) |

### Execution rules

- SL always server-side, never virtual, so it survives a terminal or VPS crash.
- At most one modification per position per bar (avoids FTMO hyperactivity flags).
- If a fill lands beyond the SL (gap or slippage), close immediately.

---

## 8. Pyramiding and dynamic stop

**Principle:** add only to a trend that is already working, never to a loser. The open risk of the whole campaign never exceeds the initial `R`.

### 8.1 Add-on allowed only if all true

- `EnablePyramiding` (true)
- Campaign is at least `+AddMinR` (1.0) R in profit on the first position.
- SL of all existing positions is at breakeven or better.
- Context filter (section 2) still valid.
- No `FailedLong`, no always-in flip, not in the no-new-orders window before session end.
- `AddsSoFar < MaxAdds` (2).

### 8.2 Add-on entry

- Same entry mechanics as section 4: a new with-trend signal bar (section 3) after a pullback (H1 or H2), buy stop at `SB.High + 1 tick + spread`.
- No market adds, no adds on breakouts without a pullback.
- The pullback low must be above the previous position's entry (a higher low has formed).
- Skip if the signal bar is a climax bar: `Range > ClimaxMult × AvgRange` (2.5).

### 8.3 Add-on sizing and risk cap

- Add-on SL = new pullback low − 1 tick.
- Campaign open risk = `Σ (Entry_i − SL_i) × Lots_i × TickValue`, counting only positions whose SL is below their entry.
- `Lots_add = min(AddSizeFactor × Lots_initial, lots keeping campaign open risk ≤ MaxCampaignRisk)`
  - `AddSizeFactor`: 0.5 for the first add, 0.25 for the second
  - `MaxCampaignRisk`: the initial `R` money amount
- Skip the add if the resulting size is below the broker minimum.

### 8.4 Dynamic stop (whole campaign)

1. **Before +1R:** initial SL, unchanged.
2. **At +1R:** all positions to breakeven (entry + spread + commission).
3. **After each confirmed higher low:** SL of all positions to `SwingLow − 1 tick`. A swing low is confirmed when `SwingConfirmBars` (2) bars on each side have higher lows.
4. **Climax tightening:** after a climax bar in the trend direction or a third push (wedge), SL to below that bar's low.
5. Stops only move in the trade's direction, never back.
6. At most one SL modification per bar, applied to all tickets in one pass.

### 8.5 Campaign exits

Close all positions at once if:

- the common trailing stop is hit
- a strong bear bar closes below EMA20 (always-in flip)
- end-of-session flatten or daily loss guard triggers

### 8.6 Bookkeeping

- All tickets of a campaign share a `CampaignID` (magic number + order comment). FTMO MT5 is a hedging account, so each add is a separate ticket.
- The daily cap counts campaigns, not tickets (input `CapCountsTickets`, false).

---

## 9. Logging

Per order / position:

- Setup type, signal bar OHLC and quality metrics, context filter values
- Entry, SL, lots, R in money, spread at placement and at fill, slippage
- Cancel reason for unfilled orders: `SB_LOW_BROKEN`, `EXPIRED`, `EOS`, `DAILY_LIMIT`
- Exit reason: `SL`, `BE`, `TIME`, `TRAIL`, `EOS`, `DAILY_LIMIT`, `FAILED_SETUP`, `ALWAYS_IN_FLIP`
- Result in R per ticket and per campaign, MAE and MFE, max R reached during the campaign

---

## 10. Backtest matrix

| Variant | Exit | Pyramiding |
|---|---|---|
| A (default) | Trailing until session end / invalidation | On |
| B | Trailing until session end / invalidation | Off |
| C (baseline) | Fixed 2R TP | Off |

Judge on expectancy (R per campaign), max drawdown and worst day versus FTMO limits, not on win rate alone. Measure win rate per setup type (H1, H2) against the 60% threshold, and check how often campaigns reach 2R.

---

## 11. Out of scope for now

- News handling (blackout, flatten). To be added before the funded account if FTMO's rules require it.
