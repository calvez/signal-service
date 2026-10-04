"""Campaign state machine — docs/spec-brooks-ea.md §4–§8 (Lorant's spec).

A campaign is the initial position plus its add-ons. This module only DECIDES; it has no
database and no broker. The same object is driven by the backtest (app/backtest_campaign.py,
which simulates fills on M1 bars) and live (MT5 executes, the service feeds the results back):

    Campaign.start(...)        signal bar closed: the entry stop order to place
    campaign.on_fill(...)      an order was filled
    campaign.on_stop(...)      a position's stop (or take profit) was hit
    campaign.on_cancelled(...) the broker cancelled a pending order (SB low broken)
    campaign.on_bar_close(...) every closed M5 bar: returns the actions to perform

All prices are real prices; rules are written for longs and use `sign` (+1 long, -1 short) so
shorts are the exact mirror. Sizes are relative to the initial position (1.0); the executor turns
them into lots. Results are in R of the initial risk (initial entry - initial stop).
"""

from dataclasses import asdict, dataclass, field

import pandas as pd

from app.advisor import DecisionPoint, Option

M5_SEC = 300

# exit reasons (§9)
SL, BE, TIME, TRAIL, EOS, DAILY_LIMIT = "SL", "BE", "TIME", "TRAIL", "EOS", "DAILY_LIMIT"
FAILED_SETUP, ALWAYS_IN_FLIP, TP = "FAILED_SETUP", "ALWAYS_IN_FLIP", "TP"
AI_EXIT, AI_PARTIAL = "AI_EXIT", "AI_PARTIAL"  # the advisor closed early (all / adds only)
# cancel reasons (§9)
SB_LOW_BROKEN, EXPIRED = "SB_LOW_BROKEN", "EXPIRED"


@dataclass
class Order:
    id: str
    kind: str  # entry | add
    price: float  # stop-order price
    sl: float
    size: float  # x the initial position
    cancel_level: float  # cancel if price trades through it before the fill (SB low - 1 tick)
    expires_at: int  # UTC: cancel at the close of the following bar(s)
    sb_time: int


@dataclass
class Position:
    id: str
    kind: str
    entry: float
    sl: float
    size: float
    opened_at: int
    risk: float  # entry - initial sl of THIS position, in price (positive)
    tp: float | None = None
    best: float = 0.0  # best price move in favour since the fill (price units)


@dataclass
class Action:
    kind: str  # place | cancel | modify_sl | close_all
    order: Order | None = None
    sl: dict | None = None  # position id -> new stop
    reason: str = ""


@dataclass
class Campaign:
    id: str
    symbol: str
    direction: str
    tick: float
    spread: float  # at the signal bar
    session_end: int
    setup: str
    initial_entry: float
    initial_sl: float
    status: str = "pending"  # pending | open | closed | cancelled
    orders: list[Order] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)  # per ticket: entry, exit, size, r, reason
    adds: int = 0
    failed: bool = False
    max_r: float = 0.0  # MFE of the initial position, in R
    min_r: float = 0.0  # MAE of the initial position, in R
    first_fill_at: int | None = None
    bars_in_trade: int = 0
    last_trailed_low: float | None = None
    sb_low: float = 0.0  # SB low (long) / high (short) of the initial signal bar
    pullback_low: float | None = None
    exit_reason: str | None = None
    cancel_reason: str | None = None
    evidence: dict = field(default_factory=dict)
    log: list[tuple[int, str]] = field(default_factory=list)
    live: bool = False  # live: MT5 reports the real exits; backtest: book at the given price
    opp_streak: int = 0  # consecutive closed bars against the position
    peak_open_r: float = 0.0  # best OPEN result of the whole campaign, in R
    ai: list[dict] = field(default_factory=list)  # every advisor decision, for the log
    _seq: int = 0

    # ------------------------------------------------------------------ helpers
    @property
    def sign(self) -> float:
        return 1.0 if self.direction == "long" else -1.0

    @property
    def r_pts(self) -> float:
        return abs(self.initial_entry - self.initial_sl)

    def _next_id(self, kind: str) -> str:
        self._seq += 1
        return f"{self.id}-{kind}{self._seq}"

    def open_risk_r(self) -> float:
        """§8.3: open risk of the campaign in R, counting only positions whose stop is still
        on the losing side of their entry."""
        risk = 0.0
        for p in self.positions:
            loss = self.sign * (p.entry - p.sl)
            if loss > 0:
                risk += loss * p.size
        return risk / self.r_pts

    def result_r(self) -> float:
        return round(sum(c["r"] for c in self.closed), 4)

    def to_dict(self) -> dict:
        return asdict(self)

    # ------------------------------------------------------------------ start
    @classmethod
    def start(
        cls,
        cid: str,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tick: float,
        spread: float,
        sb_time: int,
        session_end: int,
        setup: str,
        cfg,
        evidence: dict | None = None,
        pullback_low: float | None = None,
    ) -> tuple["Campaign", Action]:
        """§4/§6: entry stop order with the SL attached, placed at the close of SB."""
        c = cls(cid, symbol, direction, tick, spread, session_end, setup, entry, sl,
                sb_low=sl + (tick if direction == "long" else -tick),
                pullback_low=pullback_low, evidence=evidence or {})  # fmt: skip
        order = Order(c._next_id("entry"), "entry", entry, sl, 1.0, sl,
                      sb_time + M5_SEC * (1 + cfg.max_pending_bars), sb_time)  # fmt: skip
        c.orders.append(order)
        c.log.append((sb_time + M5_SEC, f"place entry {entry} sl {sl}"))
        return c, Action("place", order=order)

    # ------------------------------------------------------------------ broker events
    def on_fill(self, order_id: str, price: float, t: int, cfg) -> list[Action]:
        order = next((o for o in self.orders if o.id == order_id), None)
        if order is None:
            return []
        self.orders.remove(order)
        risk = abs(order.price - order.sl)
        tp = None
        if cfg.exit_mode == "fixed_tp" and order.kind == "entry":
            tp = order.price + self.sign * cfg.min_target_r * risk
        pos = Position(order.id, order.kind, price, order.sl, order.size, t, risk, tp)
        self.positions.append(pos)
        if order.kind == "entry":
            self.status, self.first_fill_at = "open", t
        else:
            self.adds += 1
        self.log.append((t, f"fill {order.kind} {price} size {order.size}"))
        # §7 execution rule: a fill beyond the stop (gap/slippage) is closed immediately
        if self.sign * (price - order.sl) <= 0:
            return [self._close_all_action(t, FAILED_SETUP, price)]
        return []

    def on_stop(self, position_id: str, price: float, t: int) -> None:
        """The broker closed a position at its stop (or take profit)."""
        pos = next((p for p in self.positions if p.id == position_id), None)
        if pos is None:
            return
        if pos.tp is not None and self.sign * (price - pos.tp) >= 0:
            reason = TP
        elif self.sign * (pos.sl - pos.entry) >= 0:
            reason = TRAIL if self.last_trailed_low is not None else BE
        else:
            reason = SL
        self._book(pos, price, t, reason)
        if not self.positions:
            self._finish(t, reason)

    def on_cancelled(self, order_id: str, t: int, reason: str) -> None:
        order = next((o for o in self.orders if o.id == order_id), None)
        if order is None:
            return
        self.orders.remove(order)
        self.log.append((t, f"cancel {order.kind}: {reason}"))
        if order.kind == "entry":
            self.status, self.cancel_reason = "cancelled", reason

    # ------------------------------------------------------------------ bar close
    def on_bar_close(
        self,
        bar: pd.Series,
        t_close: int,
        ev,
        strategy,
        cfg,
        flat_due,
        adds_allowed: bool = True,
        advisor=None,
    ) -> list[Action]:
        """Decisions at the close of one M5 bar (§5, §7, §8). `bar`: the closed bar with
        feature columns (o h l c ema atr ... last_sl_price last_sh_price); `ev`: its Evaluation
        (for the context filter of adds; None if it could not be evaluated); `flat_due`: the
        flatten time (True / "EOS") or the daily loss guard ("DAILY_LIMIT") has been reached.
        `advisor`: optional AI judgement at three points (app/advisor.py). It runs only AFTER
        every hard rule and may only reduce risk: exit early, tighten a stop, veto an add."""
        acts: list[Action] = []
        s = self.sign

        # pending orders: lifetime / session end (§4, §5.1, §7)
        for o in list(self.orders):
            if flat_due or t_close >= o.expires_at:
                why = (DAILY_LIMIT if flat_due == DAILY_LIMIT else EOS) if flat_due else EXPIRED
                self.on_cancelled(o.id, t_close, why)
                acts.append(Action("cancel", order=o, reason=why))
        if not self.positions:
            return acts

        hi, lo, c = float(bar["h"]), float(bar["l"]), float(bar["c"])
        first = next((p for p in self.positions if p.kind == "entry"), None)
        if first is not None:
            fav = (hi if s > 0 else lo) - first.entry
            adv = (lo if s > 0 else hi) - first.entry
            self.max_r = max(self.max_r, s * fav / self.r_pts)
            self.min_r = min(self.min_r, s * adv / self.r_pts)
        for p in self.positions:
            p.best = max(p.best, s * ((hi if s > 0 else lo) - p.entry))
        self.bars_in_trade += 1
        open_r = sum(s * (c - p.entry) * p.size for p in self.positions) / self.r_pts
        self.peak_open_r = max(self.peak_open_r, open_r)
        self.opp_streak = self.opp_streak + 1 if s * (c - float(bar["o"])) < 0 else 0

        # end of session or daily limit: close everything (§7)
        if flat_due:
            reason = DAILY_LIMIT if flat_due == DAILY_LIMIT else EOS
            return acts + [self._close_all_action(t_close, reason, c)]
        if cfg.exit_mode == "fixed_tp":  # variant C: stop, 2R take profit, session end only
            return acts

        rng = hi - lo
        body = abs(c - float(bar["o"]))
        close_pos = (c - lo) / rng if rng > 0 else 0.5  # 1 = closed on the high
        opp_close_pos = (
            close_pos if s < 0 else 1 - close_pos
        )  # 1 = closed at the extreme against us
        strong_opp = (
            rng > 0
            and s * (c - float(bar["o"])) < 0
            and body >= 0.6 * rng
            and opp_close_pos >= 0.75
        )

        # §8.5 always-in flip: strong opposite bar closing beyond EMA20; §2/§5.3 rule:
        # consec_opp_bars consecutive opposite bars each closing in their extreme 25 %
        if strong_opp and s * (c - float(bar["ema"])) < 0:
            return acts + [self._close_all_action(t_close, ALWAYS_IN_FLIP, c)]
        if ev is not None and strategy is not None:
            ok_flip, _ = strategy.flip_ok(ev, self.direction)
            if not ok_flip:
                return acts + [self._close_all_action(t_close, ALWAYS_IN_FLIP, c)]

        # §5.3 close beyond the last higher low (long) / lower high (short)
        last_hl = bar["last_sl_price"] if s > 0 else bar["last_sh_price"]
        if pd.notna(last_hl) and s * (c - float(last_hl)) < 0:
            return acts + [self._close_all_action(t_close, FAILED_SETUP, c)]

        # §5.2 failed setup flag: close beyond SB low or the pullback low
        ref = (
            self.sb_low
            if self.pullback_low is None
            else (
                min(self.sb_low, self.pullback_low)
                if s > 0
                else max(self.sb_low, self.pullback_low)
            )
        )
        if s * (c - ref) < 0:
            self.failed = True
        # §5.2 optional early exit: the bar after the entry is a strong opposite bar
        if cfg.early_exit_on_strong_opp and self.bars_in_trade == 2 and strong_opp:
            return acts + [self._close_all_action(t_close, FAILED_SETUP, c)]

        # §7 time exit: not at +time_exit_min_r within max_bars_in_trade bars
        if self.bars_in_trade >= cfg.max_bars_in_trade and self.max_r < cfg.time_exit_min_r:
            return acts + [self._close_all_action(t_close, TIME, c)]

        # ---- AI exit point: the hard rules kept us in, but something looks wrong
        if advisor is not None and cfg.ai_exit:
            trig = self._exit_triggers(bar, c, open_r, strong_opp, cfg)
            if trig:
                point = DecisionPoint(
                    kind="exit",
                    triggers=trig,
                    options=[
                        Option("hold", "hold the whole position and keep managing it by the rules"),
                        Option("close_all", f"close the whole position now at about {c}"),
                        Option(
                            "close_adds",
                            f"close the add-ons now at about {c}, keep the first position",
                        ),
                    ],
                    rule_choice="hold",
                    safe_choice="hold",
                )
                choice = self._ask(advisor, point, ev, t_close)
                if choice == "close_all":
                    return acts + [self._close_all_action(t_close, AI_EXIT, c)]
                if choice == "close_adds" and self.adds:
                    for p in [x for x in self.positions if x.kind == "add"]:
                        self._book(p, c, t_close, AI_PARTIAL)
                    acts.append(Action("close_adds", reason=AI_PARTIAL))
                    if not self.positions:
                        self._finish(t_close, AI_PARTIAL)
                        return acts

        # §8.4 dynamic stop: one modification per bar for all tickets
        new_sl = self._stop_update(bar, c, cfg)
        if new_sl:
            for pid, v in new_sl.items():
                next(p for p in self.positions if p.id == pid).sl = v
            acts.append(Action("modify_sl", sl=new_sl))

        # ---- AI tighten point: well in profit but the stop is still far behind
        if advisor is not None and cfg.ai_tighten and not new_sl:
            tighter = self._tighten_options(bar, c, open_r, cfg)
            if tighter:
                point = DecisionPoint(
                    kind="tighten",
                    triggers=[
                        f"open {open_r:+.2f}R, peak {self.peak_open_r:+.2f}R, "
                        f"stop {abs(c - self.positions[0].sl) / self.r_pts:.2f}R behind the price"
                    ],
                    options=[Option("keep", "leave the stops where the rules put them"), *tighter],
                    rule_choice="keep",
                    safe_choice="keep",
                )
                choice = self._ask(advisor, point, ev, t_close)
                chosen = point.option(choice)
                if chosen is not None and chosen.sl is not None:
                    moved = {}
                    for p in self.positions:
                        if s * (chosen.sl - p.sl) > 0 and s * (c - chosen.sl) > 0:
                            p.sl = chosen.sl
                            moved[p.id] = chosen.sl
                    if moved:
                        acts.append(Action("modify_sl", sl=moved, reason="ai_tighten"))

        # §8 add-ons (the rules propose, the advisor may veto)
        add = self._maybe_add(bar, c, t_close, ev, strategy, cfg) if adds_allowed else None
        if add is not None and advisor is not None and cfg.ai_add:
            o = add.order
            point = DecisionPoint(
                kind="add",
                triggers=[
                    f"the rules allow add #{self.adds + 1}: stop order at {o.price}, "
                    f"stop {o.sl}, size {o.size:g}x the first position"
                ],
                options=[
                    Option("take", f"place the add-on stop order at {o.price}"),
                    Option("skip", "no add-on on this bar"),
                ],
                rule_choice="take",
                safe_choice="skip",
            )
            if self._ask(advisor, point, ev, t_close) != "take":
                self.orders.remove(o)
                self.log.append((t_close, "add vetoed by the advisor"))
                add = None
        if add is not None:
            acts.append(add)
        return acts

    # ------------------------------------------------------------------ advisor
    def _ask(self, advisor, point: "DecisionPoint", ev, t: int) -> str:
        choice, reason = advisor.decide(point, self, ev)
        if point.option(choice) is None:
            choice, reason = point.safe_choice, f"unknown option {choice!r}: safe choice"
        self.ai.append({"t": t, "point": point.kind, "triggers": point.triggers,
                        "rule_choice": point.rule_choice, "choice": choice, "reason": reason,
                        "advisor": getattr(advisor, "name", "?")})  # fmt: skip
        if choice != point.rule_choice:
            self.log.append((t, f"advisor {point.kind}: {choice} (rules: {point.rule_choice})"))
        return choice

    def _exit_triggers(self, bar, c: float, open_r: float, strong_opp: bool, cfg) -> list[str]:
        """Why a human would look up from the chart now. Empty = nothing to judge."""
        s, out = self.sign, []
        if strong_opp:
            out.append(f"strong bar against the position, closed at {c}")
        if self.opp_streak >= 2:
            out.append(f"{self.opp_streak} bars in a row closed against the position")
        rng = float(bar["h"]) - float(bar["l"])
        avg = float(bar.get("avg_range", float("nan")))
        with_trend = s * (c - float(bar["o"])) > 0
        if with_trend and pd.notna(avg) and avg > 0 and rng > cfg.climax_mult * avg:
            out.append(f"climax bar in the trend direction: range {rng / avg:.1f}x the average")
        if self.peak_open_r >= 1.0 and self.peak_open_r - open_r >= cfg.ai_give_back_r:
            out.append(f"given back {self.peak_open_r - open_r:.2f}R from the peak "
                       f"({self.peak_open_r:+.2f}R to {open_r:+.2f}R)")  # fmt: skip
        return out

    def _tighten_options(self, bar, c: float, open_r: float, cfg) -> list["Option"]:
        """Stops the advisor may move to. Only ever in the trade's direction and never through
        the price, so choosing one can only reduce risk."""
        s = self.sign
        if open_r < cfg.ai_tighten_min_r or not self.positions:
            return []
        widest = min((p.sl for p in self.positions), key=lambda x: s * x)
        if abs(c - widest) / self.r_pts < cfg.ai_tighten_gap_r:
            return []
        out = []
        lvl = float(bar["l"]) if s > 0 else float(bar["h"])
        candidates = {
            "tighten_bar": (
                lvl - s * self.tick,
                "below this bar's low" if s > 0 else "above this bar's high",
            ),
            "tighten_half": ((c + widest) / 2, "half way between the price and the current stop"),
        }
        for oid, (level, text) in candidates.items():
            if s * (level - widest) > 0 and s * (c - level) > 0:
                out.append(
                    Option(oid, f"move all stops to {round(level, 10)} ({text})", round(level, 10))
                )
        return out

    # ------------------------------------------------------------------ internals
    def _stop_update(self, bar: pd.Series, c: float, cfg) -> dict:
        s, tick = self.sign, self.tick
        common: float | None = None
        # 3. after +1R: each new confirmed higher low -> swing low - 1 tick
        swing = bar["last_sl_price"] if s > 0 else bar["last_sh_price"]
        if self.max_r >= 1.0 and pd.notna(swing):
            swing = float(swing)
            if self.last_trailed_low is None or s * (swing - self.last_trailed_low) > 0:
                common = swing - s * tick
                self.last_trailed_low = swing
        # 4. climax bar in the trend direction: stop below its low
        rng = float(bar["h"] - bar["l"])
        avg = float(bar.get("avg_range", float("nan")))
        with_trend_bar = s * (float(bar["c"]) - float(bar["o"])) > 0
        if self.max_r >= 1.0 and with_trend_bar and pd.notna(avg) and rng > cfg.climax_mult * avg:
            climax = (float(bar["l"]) if s > 0 else float(bar["h"])) - s * tick
            common = (
                climax
                if common is None
                else (max(common, climax) if s > 0 else min(common, climax))
            )
        out: dict = {}
        for p in self.positions:
            best = p.sl
            # 2. breakeven at +be_at_r x the position's own risk: entry + spread + commission
            if cfg.use_be and p.best >= cfg.be_at_r * p.risk:
                be = p.entry + s * (self.spread + cfg.commission_pts * tick)
                best = max(best, be) if s > 0 else min(best, be)
            if common is not None:
                best = max(best, common) if s > 0 else min(best, common)
            # 5. only in the trade's direction, and never through the current price
            if s * (best - p.sl) > 0 and s * (c - best) > 0:
                out[p.id] = round(best, 10)
        return out

    def _maybe_add(self, bar, c, t_close, ev, strategy, cfg) -> Action | None:
        """§8.1–8.3."""
        s = self.sign
        if (not cfg.enable_pyramiding or cfg.exit_mode != "trail" or self.failed
                or self.adds >= cfg.max_adds or any(o.kind == "add" for o in self.orders)
                or ev is None or strategy is None):  # fmt: skip
            return None
        first = next((p for p in self.positions if p.kind == "entry"), None)
        if first is None or s * (c - first.entry) < cfg.add_min_r * self.r_pts:
            return None
        if any(s * (p.sl - p.entry) < 0 for p in self.positions):  # all at breakeven or better
            return None
        if t_close > self.session_end - cfg.no_new_order_mins * 60:
            return None
        sig = strategy.add_signal(ev, self.direction)
        if sig is None:
            return None
        price, sl, pb_low = sig["entry"], sig["stop"], sig["pullback_low"]
        last_entry = self.positions[-1].entry
        if s * (pb_low - last_entry) <= 0:  # the pullback low must be a higher low
            return None
        factor = cfg.add_size_factors[min(self.adds, len(cfg.add_size_factors) - 1)]
        room = 1.0 - self.open_risk_r()  # campaign open risk <= the initial R
        per_unit = abs(price - sl) / self.r_pts
        size = min(factor, room / per_unit) if per_unit > 0 else 0.0
        if size <= 0.0:
            return None
        expires = t_close + M5_SEC * cfg.max_pending_bars
        order = Order(self._next_id("add"), "add", price, sl, round(size, 4),
                      sl + s * self.tick, expires, t_close - M5_SEC)  # fmt: skip
        self.orders.append(order)
        self.log.append((t_close, f"place add {price} sl {sl} size {order.size}"))
        return Action("place", order=order)

    def _book(self, p: Position, price: float, t: int, reason: str) -> None:
        r = self.sign * (price - p.entry) * p.size / self.r_pts
        self.closed.append({"id": p.id, "kind": p.kind, "entry": p.entry, "exit": price,
                            "size": p.size, "r": round(r, 4), "reason": reason,
                            "opened_at": p.opened_at, "closed_at": t})  # fmt: skip
        self.positions.remove(p)

    def _finish(self, t: int, reason: str) -> None:
        self.status, self.exit_reason = "closed", reason
        self.log.append((t, f"closed: {reason}, {self.result_r():+.2f}R"))

    def _close_all_action(self, t: int, reason: str, price: float) -> Action:
        """Close every position and cancel pending orders. Backtest: booked at `price` now.
        Live: MT5 closes at the market and the real exits are booked when it reports them."""
        if not self.live:
            for p in list(self.positions):
                self._book(p, price, t, reason)
            self.orders.clear()
            self._finish(t, reason)
        return Action("close_all", reason=reason)
