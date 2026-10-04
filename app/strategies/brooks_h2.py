"""Brooks H2 pullback with stop-order entries — docs/spec-brooks-ea.md §2 (context filter),
§3 (signal bar) and §4 (entry price). Lorant's spec; all inputs in config.yaml `brooks:`.

Rules are written for LONGS, exactly as in the spec. Shorts use the same code on a MIRRORED
copy of the bars (prices negated, high <-> low, swing highs <-> swing lows, H <-> L counts), so
the short side is the exact mirror image and can never drift from the long side.

Every rule is one small function returning (ok, value) so the evidence can be logged (§9).
The evaluated bar is the signal bar SB: the last CLOSED bar.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.strategies import Candidate

# columns that swap between the long and the mirrored (short) view
_SWAP = {
    "h": "l", "l": "h", "sh_price": "sl_price", "sl_price": "sh_price",
    "last_sh_price": "last_sl_price", "last_sl_price": "last_sh_price",
    "h_count": "l_count", "l_count": "h_count", "h_bar": "l_bar", "l_bar": "h_bar",
    "h_pb": "l_pb", "l_pb": "h_pb",
}  # fmt: skip
_PRICES = ["o", "h", "l", "c", "ema", "sh_price", "sl_price", "last_sh_price", "last_sl_price"]


def mirror(feats: pd.DataFrame) -> pd.DataFrame:
    """Bars of a short setup, turned into the equivalent long setup: prices negated and the
    high/low pairs swapped. Spreads and counts stay positive."""
    m = feats.rename(columns=_SWAP).copy()
    for col in _PRICES:
        if col in m:
            m[col] = -m[col]
    return m


@dataclass(frozen=True)
class View:
    """The bars up to and including SB, seen from the trade's side (long or mirrored)."""

    bars: pd.DataFrame  # feature columns, last row = SB
    sign: float  # +1 long, -1 short (to map prices back)
    tick: float  # symbol tick (here: one point)

    @property
    def sb(self) -> pd.Series:
        return self.bars.iloc[-1]

    def back(self, price: float) -> float:
        """A price of this view as a real price."""
        return self.sign * price


def avg_range(v: View, n: int) -> float:
    """AvgRange (§1): mean high-low of the last n closed bars (SB included)."""
    r = (v.bars["h"] - v.bars["l"]).tail(n)
    return float(r.mean())


# --------------------------------------------------------------------------- §3 signal bar
def signal_bar(v: View, cfg, point: float) -> tuple[bool, dict]:
    """§3 bull signal bar. Returns (ok, metrics)."""
    b = v.bars
    sb, prev = b.iloc[-1], b.iloc[-2]
    rng = sb["h"] - sb["l"]
    ar = avg_range(v, cfg.avg_range_bars)
    spread = float(sb.get("sp", 0) or 0) * point
    m = {
        "range": round(rng, 6),
        "avg_range": round(ar, 6),
        "body_ratio": round(abs(sb["c"] - sb["o"]) / rng, 3) if rng > 0 else 0.0,
        "close_pos": round((sb["c"] - sb["l"]) / rng, 3) if rng > 0 else 0.0,
        "upper_tail": round((sb["h"] - sb["c"]) / rng, 3) if rng > 0 else 1.0,
        "spread": round(spread, 6),
    }
    ok = (
        rng > 0
        and sb["c"] > sb["o"]
        and m["body_ratio"] >= cfg.sb_body_min
        and m["close_pos"] >= cfg.sb_close_pos_min
        and m["upper_tail"] <= cfg.sb_tail_max
        and sb["c"] > prev["c"]
        and rng >= cfg.min_sb_range_pts * point
        and rng <= cfg.max_sb_range_avg * ar
        and spread <= cfg.max_spread_sb_range * rng
    )
    return bool(ok), m


# --------------------------------------------------------------------------- §2 context
def two_emas(close: float, ema_m5: float, ema_h1: float | None, mode: str) -> tuple[bool, dict]:
    """Lorant's two EMAs (in the view's prices, i.e. already mirrored for shorts):
    price_above = close above the M5 AND the 60-minute EMA20; ema_order = M5 EMA20 above the
    60-minute EMA20; both = both; off = no 60-minute condition. No 60-minute EMA yet -> fails
    (fail closed), unless mode is off."""
    if mode == "off":
        return True, {}
    if ema_h1 is None or np.isnan(ema_h1):
        return False, {"h1_ema": None}
    above = close > ema_h1
    order = ema_m5 > ema_h1
    ok = {"price_above": above, "ema_order": order, "both": above and order}[mode]
    return bool(ok), {"above_h1_ema": bool(above), "m5_ema_above_h1_ema": bool(order)}


def trend(v: View, cfg, ema_h1: float | None = None) -> tuple[bool, dict]:
    """Close above EMA20, EMA20 rising over ema_slope_bars, last swing high > previous one, and
    the 60-minute EMA20 condition (htf_ema_filter). `ema_h1` is in the view's prices."""
    b = v.bars
    highs = b["sh_price"].dropna().to_numpy()
    rising = (
        len(b) > cfg.ema_slope_bars and b["ema"].iloc[-1] > b["ema"].iloc[-1 - cfg.ema_slope_bars]
    )
    hh = len(highs) >= 2 and highs[-1] > highs[-2]
    close, ema = float(b["c"].iloc[-1]), float(b["ema"].iloc[-1])
    ok_h1, h1m = two_emas(close, ema, ema_h1, cfg.htf_ema_filter)
    ok = close > ema and rising and hh and ok_h1
    details = {"above_ema": close > ema, "ema_rising": bool(rising), "higher_high": bool(hh)}
    return bool(ok), {**details, **h1m}


@dataclass(frozen=True)
class Pullback:
    start: int  # position (in v.bars) of the high the pullback started from
    bars: int  # bars from that high up to and including SB
    low: float  # lowest low of the pullback (SB included)
    low_pos: int


def pullback(v: View, cfg) -> Pullback | None:
    """The pullback that SB ends: from the highest high of the last max_pullback_bars + 1 bars
    before SB, down to SB. None if SB itself made the high (no pullback)."""
    b = v.bars
    n = len(b)
    look = b.iloc[max(0, n - 1 - (cfg.max_pullback_bars + 1)) : n - 1]
    if look.empty:
        return None
    start = n - 1 - len(look) + int(np.argmax(look["h"].to_numpy()))
    seg = b.iloc[start + 1 : n]  # bars after the high, SB included
    if seg.empty:
        return None
    low_pos = start + 1 + int(np.argmin(seg["l"].to_numpy()))
    return Pullback(start, n - 1 - start, float(seg["l"].min()), low_pos)


def setup_type(v: View, cfg) -> tuple[bool, str]:
    """Which H entry would a buy stop above SB create?

    Brooks counts an H bar when price trades above the previous bar's high while a pullback is
    armed. The entry is a stop order ABOVE the signal bar, so the bar that fills it becomes the
    next H bar: buying above SB creates H(h_count + 1). An H2 entry therefore needs a signal bar
    with h_count == 1 (H1 has already come and failed) and an armed pullback (features.leg_counts
    `h_pb`). Requiring SB to BE the H2 bar would be an H3 entry.
    """
    sb = v.sb
    if not bool(sb.get("h_pb", False)):
        return False, "none"  # no pullback armed: a break of SB's high is not an H entry
    n = int(sb.get("h_count", 0)) + 1
    if n == 2:
        return True, "H2"
    if n == 1 and cfg.allow_h1:
        return True, "H1"
    return False, f"H{n}"


def pullback_depth(v: View, pb: Pullback, cfg) -> tuple[bool, dict]:
    """No close below the last higher low during the pullback; at most max_pullback_bars bars;
    the pullback reached EMA20 (low within ema_touch_avg_range x AvgRange of it)."""
    b = v.bars
    seg = b.iloc[pb.start + 1 :]
    last_hl = b["last_sl_price"].iloc[-1]
    held = bool(np.isnan(last_hl) or (seg["c"] >= last_hl).all())
    ema_at_low = float(b["ema"].iloc[pb.low_pos])
    touch = pb.low <= ema_at_low + cfg.ema_touch_avg_range * avg_range(v, cfg.avg_range_bars)
    ok = held and pb.bars <= cfg.max_pullback_bars and touch
    return bool(ok), {"pullback_bars": pb.bars, "held_higher_low": held, "reached_ema": bool(touch)}


def not_trading_range(v: View, cfg) -> tuple[bool, int]:
    """Fewer than range_overlap_count of the last range_lookback bars overlap the prior bar by
    more than 50 % (of their own range) or are dojis (body <= 25 % of the range)."""
    b = v.bars.tail(cfg.range_lookback + 1)
    h, low, o, c = (b[k].to_numpy() for k in ("h", "l", "o", "c"))
    count = 0
    for i in range(1, len(b)):
        rng = h[i] - low[i]
        overlap = min(h[i], h[i - 1]) - max(low[i], low[i - 1])
        doji = rng <= 0 or abs(c[i] - o[i]) <= 0.25 * rng
        if doji or (rng > 0 and overlap > 0.5 * rng):
            count += 1
    return count < cfg.range_overlap_count, count


def no_always_in_flip(v: View, cfg) -> tuple[bool, int]:
    """Fewer than consec_opp_bars consecutive bear bars, each closing in its bottom 25 %, ending
    at SB (counting back from SB)."""
    b = v.bars
    n = 0
    for _, bar in b.iloc[::-1].iterrows():
        rng = bar["h"] - bar["l"]
        if rng > 0 and bar["c"] < bar["o"] and (bar["c"] - bar["l"]) / rng <= 0.25:
            n += 1
        else:
            break
    return n < cfg.consec_opp_bars, n


def room_to_target(
    v: View,
    entry: float,
    risk: float,
    session_start: pd.Timestamp,
    cfg,
    pb_high: float | None = None,
) -> tuple[bool, float | None]:
    """No prior swing high or session extreme between the entry and entry + min_target_r x R.
    With room_ignores_pullback_high, levels at or below the high the pullback started from
    (`pb_high`) are not counted: breaking that high is the trade. Returns (ok, nearest level
    in the way)."""
    b = v.bars
    today = b[b.index >= session_start]
    levels = list(today["sh_price"].dropna().to_numpy())
    if not today.empty:
        levels.append(float(today["h"].max()))  # the session extreme so far
    if cfg.room_ignores_pullback_high and pb_high is not None:
        levels = [x for x in levels if x > pb_high]
    goal = entry + cfg.min_target_r * risk
    blocking = [x for x in levels if entry < x < goal]
    return not blocking, (min(blocking) if blocking else None)


def entry_and_stop(v: View, point: float) -> tuple[float, float]:
    """§4: Entry = SB.High + 1 tick + spread (buy stop), SL = SB.Low - 1 tick. In the mirrored
    view this gives SellStop = SB.Low - 1 tick and SL = SB.High + 1 tick... plus the spread,
    which the caller removes for shorts (the spec's sell stop has no spread)."""
    sb = v.sb
    spread = float(sb.get("sp", 0) or 0) * point
    return sb["h"] + v.tick + spread, sb["l"] - v.tick


# --------------------------------------------------------------------------- the strategy
class BrooksH2:
    """docs/spec-brooks-ea.md §2–§4. One candidate per direction at most."""

    name = "brooks_h2"
    version = "1"

    def __init__(self, cfg):
        self.cfg = cfg  # config.brooks

    @staticmethod
    def h1_ema_in_view(ev, v: View) -> float | None:
        return None if ev.h1_ema is None else v.sign * ev.h1_ema

    def view(self, ev, direction: str) -> View:
        tick = 10**-ev.digits
        if direction == "long":
            return View(ev.feats, 1.0, tick)
        return View(mirror(ev.feats), -1.0, tick)

    def timing_ok(self, ev) -> tuple[bool, str]:
        """§4 timing: not in the first skip_open_bars bars, no new order in the last
        no_new_order_mins minutes of the session window."""
        if ev.bar_index <= self.cfg.skip_open_bars:
            return False, "first bars of the session"
        order_time = ev.bar_open + 300  # placed at the close of SB
        if order_time > ev.session_end - self.cfg.no_new_order_mins * 60:
            return False, "too close to the session end"
        return True, ""

    def check(self, ev, direction: str, purpose: str = "entry") -> tuple[bool, dict]:
        """All §2/§3 rules for one direction. purpose 'add' relaxes nothing here; the campaign
        engine adds its own add-on rules (§8)."""
        cfg = self.cfg
        v = self.view(ev, direction)
        point = 10**-ev.digits
        ev_ = {"direction": direction}
        ok_sb, sbm = signal_bar(v, cfg, point)
        ev_["signal_bar"] = sbm
        ok_trend, tm = trend(v, cfg, self.h1_ema_in_view(ev, v))
        ev_["trend"] = tm
        ok_type, kind = setup_type(v, cfg)
        ev_["setup"] = kind
        pb = pullback(v, cfg)
        ok_pb, pbm = (False, {}) if pb is None else pullback_depth(v, pb, cfg)
        ev_["pullback"] = pbm
        ev_["pullback_low"] = None if pb is None else round(v.back(pb.low), 6)
        ok_range, overlaps = not_trading_range(v, cfg)
        ev_["range_bars"] = overlaps
        ok_flip, opp = no_always_in_flip(v, cfg)
        ev_["opposite_bars"] = opp
        entry, stop = entry_and_stop(v, point)
        risk = entry - stop
        pb_high = None if pb is None else float(v.bars["h"].iloc[pb.start])
        start = pd.Timestamp(ev.session_start, unit="s", tz="UTC")
        ok_room, level = room_to_target(v, entry, risk, start, cfg, pb_high)
        ev_["room_blocked_by"] = None if level is None else round(v.back(level), 6)
        checks = {"signal_bar": ok_sb, "trend": ok_trend, "setup": ok_type, "pullback": ok_pb,
                  "not_range": ok_range, "no_flip": ok_flip, "room": ok_room}  # fmt: skip
        ev_["failed"] = [k for k, ok in checks.items() if not ok]
        ev_["entry"], ev_["stop"] = round(v.back(entry), 6), round(v.back(stop), 6)
        return all(checks.values()), ev_

    # ---- used by the campaign (app/campaign.py)
    def flip_ok(self, ev, direction: str) -> tuple[bool, int]:
        """§2/§5.3 always-in flip rule against `direction`."""
        return no_always_in_flip(self.view(ev, direction), self.cfg)

    def add_signal(self, ev, direction: str) -> dict | None:
        """§8.2 add-on entry at this bar, or None. Context still valid (trend, no trading range,
        no flip), a §3 signal bar after a pullback (H1 or H2), not a climax bar. Room to target
        and the pullback-to-EMA rule are for the first entry only (adds come in strong trends).
        Returns real prices: entry, stop (= pullback low - 1 tick) and the pullback low."""
        cfg = self.cfg
        v = self.view(ev, direction)
        point = 10**-ev.digits
        h1 = self.h1_ema_in_view(ev, v)
        if not (
            trend(v, cfg, h1)[0] and not_trading_range(v, cfg)[0] and no_always_in_flip(v, cfg)[0]
        ):
            return None
        if not signal_bar(v, cfg, point)[0]:
            return None
        if not setup_type(v, cfg.model_copy(update={"allow_h1": True}))[0]:
            return None
        sb = v.sb
        if (sb["h"] - sb["l"]) > cfg.climax_mult * avg_range(v, cfg.avg_range_bars):
            return None
        pb = pullback(v, cfg)
        if pb is None:
            return None
        entry, _ = entry_and_stop(v, point)
        stop = pb.low - v.tick
        out = {"entry": v.back(entry), "stop": v.back(stop), "pullback_low": v.back(pb.low)}
        if direction == "short":
            out["entry"] += float(sb.get("sp", 0) or 0) * point  # sell stop without spread
        return {k: round(x, ev.digits) for k, x in out.items()}

    def candidates(self, ev) -> list[Candidate]:
        ok_time, _ = self.timing_ok(ev)
        if not ok_time:
            return []
        out = []
        point = 10**-ev.digits
        for direction in ("long", "short"):
            ok, evidence = self.check(ev, direction)
            if not ok:
                continue
            entry, stop = evidence["entry"], evidence["stop"]
            if direction == "short":
                # the spec's sell stop is SB.Low - 1 tick, without the spread (§4)
                entry += float(ev.last.get("sp", 0) or 0) * point
            out.append(Candidate(evidence["setup"], direction, round(entry, ev.digits),
                                 round(stop, ev.digits), None, True, "A", evidence))  # fmt: skip
        return out
