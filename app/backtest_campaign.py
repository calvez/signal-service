"""Backtest of the campaign engine (docs/spec-brooks-ea.md) over history — §10.

For every M5 bar inside a symbol's session window, in time order across ALL symbols (so the
daily campaign cap of §4 works across symbols):

  1. SimBroker plays the M1 bars of this M5 bar against the active campaign: pending stop
     orders fill or are cancelled (price traded through the SB low first), stops and take
     profits are hit. Bars are BID prices; the spread of each M1 bar is added where MT5 uses
     the ask (buy stop triggers, the stop of a short). Inside one M1 bar the worse case comes
     first: an order that triggers in the same minute its cancel/stop level is reached is
     filled AND stopped out.
  2. At the M5 close: evaluate_bar (the live code), then campaign.on_bar_close (stops, exits,
     adds), then — if no campaign is active for the symbol — strategy.candidates and
     validate.check_setup for a new campaign.

Results are in R of the initial risk per campaign and in % of the balance (R x
risk_per_trade_pct), net of spread (it is inside the fill prices). No commission (0 by default),
no slippage beyond gaps (a gap fills at the bar's open).
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app import db, sessions
from app.campaign import DAILY_LIMIT, EOS, SB_LOW_BROKEN, Campaign
from app.config import AppConfig
from app.evaluation import D1_WINDOW, H1_WINDOW, M5_WINDOW, Skip, evaluate_bar
from app.validate import check_setup

M5_SEC = 300
ISO = "%Y-%m-%dT%H:%M:%SZ"
WARMUP = {"M5": 4 * 86400, "H1": 40 * 86400, "D1": 420 * 86400}


@dataclass
class SymbolData:
    symbol: str
    session: str
    digits: int
    m5: pd.DataFrame
    h1: pd.DataFrame
    d1: pd.DataFrame
    m1: pd.DataFrame | None
    m5_t: np.ndarray = field(default_factory=lambda: np.array([]))
    h1_t: np.ndarray = field(default_factory=lambda: np.array([]))
    d1_t: np.ndarray = field(default_factory=lambda: np.array([]))
    m1_t: np.ndarray = field(default_factory=lambda: np.array([]))

    @property
    def point(self) -> float:
        return 10**-self.digits

    def minute_bars(self, t_open: int) -> pd.DataFrame:
        """The bars inside the M5 bar opening at t_open (M1 if available, else the M5 bar)."""
        if self.m1 is None:
            i = int(np.searchsorted(self.m5_t, t_open))
            return self.m5.iloc[i : i + 1]
        lo = int(np.searchsorted(self.m1_t, t_open))
        hi = int(np.searchsorted(self.m1_t, t_open + M5_SEC))
        return self.m1.iloc[lo:hi]


def load(conn, cfg: AppConfig, symbol: str, start: int, end: int, use_m1: bool) -> SymbolData:
    def get(tf: str) -> pd.DataFrame:
        return db.load_bars_between(conn, symbol, tf, start - WARMUP.get(tf, 0), end + 86400)

    m1 = get("M1") if use_m1 else None
    d = SymbolData(
        symbol,
        cfg.symbols[symbol].session,
        db.get_digits(conn, symbol) or 2,
        get("M5"),
        get("H1"),
        get("D1"),
        None if m1 is None or m1.empty else m1,
    )
    d.m5_t, d.h1_t, d.d1_t = (x.index.as_unit("s").asi8 for x in (d.m5, d.h1, d.d1))
    if d.m1 is not None:
        d.m1_t = d.m1.index.as_unit("s").asi8
    return d


# --------------------------------------------------------------------------- broker
def sim_broker(c: Campaign, bars: pd.DataFrame, point: float, cfg) -> None:
    """Play the bars (M1, bid prices) against the campaign's orders and positions."""
    s = c.sign
    for ts, bar in bars.iterrows():
        t = int(ts.timestamp())
        o_, h, lo = float(bar["o"]), float(bar["h"]), float(bar["l"])
        spread = float(bar.get("sp", 0) or 0) * point
        ask_h, ask_o = h + spread, o_ + spread
        for o in list(c.orders):
            if s > 0:
                trig, broken = ask_h >= o.price, lo <= o.cancel_level
                fill = max(o.price, ask_o)
            else:
                trig, broken = lo <= o.price, h >= o.cancel_level
                fill = min(o.price, o_)
            if broken and not trig:
                c.on_cancelled(o.id, t, SB_LOW_BROKEN)
                continue
            if trig:
                c.on_fill(o.id, fill, t, cfg)
        for p in list(c.positions):
            if s > 0:
                stop_hit, stop_fill = lo <= p.sl, min(p.sl, o_)
                tp_hit = p.tp is not None and h >= p.tp
            else:
                stop_hit, stop_fill = ask_h >= p.sl, max(p.sl, ask_o)
                tp_hit = p.tp is not None and lo + spread <= p.tp
            if stop_hit:  # the stop first: worse case
                c.on_stop(p.id, stop_fill, t)
            elif tp_hit:
                c.on_stop(p.id, p.tp, t)
        if c.status in ("closed", "cancelled"):
            return


def floating_r(c: Campaign, price: float) -> float:
    return sum(c.sign * (price - p.entry) * p.size for p in c.positions) / c.r_pts


# --------------------------------------------------------------------------- driver
@dataclass
class Result:
    campaigns: list[Campaign]
    skips: Counter
    bars: int


def run(
    cfg: AppConfig, conn, strategy, symbols: list[str], start: int, end: int, use_m1: bool = True
) -> Result:
    bc = cfg.brooks
    mgmt_cfg = cfg.model_copy(
        update={"rules": cfg.rules.model_copy(update={"require_htf_alignment": False})}
    )
    gate = cfg.rules.require_htf_alignment
    data = {s: load(conn, cfg, s, start, end, use_m1) for s in symbols}
    prague = ZoneInfo(cfg.ftmo.day_reset_tz)

    events: list[tuple[int, str, int]] = []
    for s, d in data.items():
        day = sessions.local_date(cfg, d.session, start)
        last = sessions.local_date(cfg, d.session, end)
        while day <= last:
            win = sessions.session_window_utc(cfg, d.session, day)
            if win:
                lo = int(np.searchsorted(d.m5_t, max(win[0], start)))
                hi = int(np.searchsorted(d.m5_t, min(win[1], end)))
                events += [(int(d.m5_t[i]), s, i) for i in range(lo, hi)]
            day += pd.Timedelta(days=1)
    events.sort()

    active: dict[str, Campaign] = {}
    done: list[Campaign] = []
    skips: Counter = Counter()
    day_campaigns: Counter = Counter()
    day_r: Counter = Counter()  # closed results per FTMO day, in R
    blocked_days: set = set()
    htf_cache: dict = {}
    limit_r = bc.daily_loss_limit_pct / bc.risk_per_trade_pct  # the daily guard in R
    last_close: dict[str, float] = {}

    def finish(c: Campaign) -> None:
        done.append(c)
        active.pop(c.symbol, None)
        if c.status == "closed":
            d = datetime.fromtimestamp(c.closed[-1]["closed_at"], tz=UTC).astimezone(prague).date()
            day_r[d] += c.result_r()

    for t, sym, i in events:
        d = data[sym]
        t_close = t + M5_SEC
        day = datetime.fromtimestamp(t, tz=UTC).astimezone(prague).date()
        c = active.get(sym)

        if c is not None:  # 1. what happens inside this M5 bar
            sim_broker(c, d.minute_bars(t), d.point, bc)
            if c.status in ("closed", "cancelled"):
                finish(c)
                c = None

        # 2. the evaluation at the close (gate off here; the H1/D1 rule is applied below)
        j = int(np.searchsorted(d.h1_t, t_close, side="right"))
        k = int(np.searchsorted(d.d1_t, t_close, side="right"))
        m5_win = d.m5.iloc[max(0, i - M5_WINDOW + 1) : i + 1]
        h1_win = d.h1.iloc[max(0, j - H1_WINDOW - 1) : j]
        d1_win = d.d1.iloc[max(0, k - D1_WINDOW - 1) : k]
        try:
            ev = evaluate_bar(mgmt_cfg, sym, t, m5_win, h1_win, d1_win, d.digits, htf_cache)
        except Skip as skip:
            skips[str(skip).split(" ")[0]] += 1
            ev = None
        conflict = ev is not None and gate and ev.alignment == "conflict"
        if conflict:
            skips["htf_conflict"] += 1

        # daily loss guard on closed + floating results of the FTMO day
        last_close[sym] = float(d.m5["c"].iloc[i])
        float_r = sum(
            floating_r(x, last_close.get(x.symbol, x.initial_entry)) for x in active.values()
        )
        if day not in blocked_days and day_r[day] + float_r <= -limit_r:
            blocked_days.add(day)
        flat_due = None
        if day in blocked_days:
            flat_due = DAILY_LIMIT
        elif ev is not None and t_close >= ev.session_end - bc.flatten_mins * 60:
            flat_due = EOS

        if c is not None:  # campaign management at the close
            if ev is None and not flat_due:
                continue  # cannot evaluate this bar: leave the campaign on its server-side stop
            bar = ev.feats.iloc[-1].copy() if ev is not None else d.m5.iloc[i].copy()
            if ev is not None:
                bar["avg_range"] = float(
                    (ev.feats["h"] - ev.feats["l"]).tail(bc.avg_range_bars).mean()
                )
            c.on_bar_close(bar, t_close, ev, strategy, bc, flat_due, adds_allowed=not conflict)
            if c.status in ("closed", "cancelled"):
                finish(c)
                c = None

        # 3. a new campaign
        can_start = c is None and ev is not None and not conflict and not flat_due
        if can_start and sym not in active and day_campaigns[day] < bc.max_campaigns_per_day:
            for cand in strategy.candidates(ev):
                out = check_setup(cand.as_setup(), "alert", ev.expected(), cfg.rules)
                if out.action == "none":
                    skips["check_setup:" + out.summary.split(":")[1].strip()[:30]] += 1
                    continue
                spread = float(ev.last.get("sp", 0) or 0) * d.point
                nc, _ = Campaign.start(
                    f"{sym}-{t}",
                    sym,
                    cand.direction,
                    cand.entry,
                    cand.stop,
                    d.point,
                    spread,
                    t,
                    ev.session_end,
                    cand.setup,
                    bc,
                    cand.evidence,
                    cand.evidence.get("pullback_low"),
                )
                active[sym] = nc
                day_campaigns[day] += 1
                break
    for c in list(active.values()):  # end of the data
        if c.positions:
            last = float(data[c.symbol].m5["c"].iloc[-1])
            c._close_all_action(end, EOS, last)
        else:
            c.status, c.cancel_reason = "cancelled", EOS
        finish(c)
    return Result(done, skips, len(events))


# --------------------------------------------------------------------------- statistics
def campaign_rows(campaigns: list[Campaign], risk_pct: float) -> pd.DataFrame:
    def iso(t: int | None) -> str:
        return "" if t is None else datetime.fromtimestamp(t, tz=UTC).strftime(ISO)

    rows = []
    for c in campaigns:
        r = c.result_r() if c.status == "closed" else None
        rows.append({
            "campaign": c.id, "symbol": c.symbol, "direction": c.direction, "setup": c.setup,
            "signal_utc": iso(int(c.id.rsplit("-", 1)[1])), "entry": c.initial_entry,
            "stop": c.initial_sl, "status": c.status, "cancel_reason": c.cancel_reason or "",
            "exit_reason": c.exit_reason or "", "tickets": len(c.closed), "adds": c.adds,
            "r": r, "pct": None if r is None else round(r * risk_pct, 4),
            "mfe_r": round(c.max_r, 3), "mae_r": round(c.min_r, 3), "reached_2r": c.max_r >= 2.0,
            "first_fill_utc": iso(c.first_fill_at),
            "exit_utc": iso(c.closed[-1]["closed_at"]) if c.closed else "",
            "evidence": str(c.evidence),
        })  # fmt: skip
    return pd.DataFrame(rows)


def stats(df: pd.DataFrame, cfg: AppConfig) -> dict:
    """§10: expectancy per campaign, drawdown and worst day vs the FTMO limits, win rate per
    setup type, how often campaigns reach 2R."""
    ent = df[df["status"] == "closed"].copy()
    cancelled = df.loc[df["status"] == "cancelled", "cancel_reason"]
    out = {"signals": len(df), "entered": len(ent), "cancelled": dict(Counter(cancelled))}
    if ent.empty:
        return out
    r = ent["r"].to_numpy()
    eq = np.cumsum(ent["pct"].to_numpy())
    dd = float(np.max(np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]))
    prague = ZoneInfo(cfg.ftmo.day_reset_tz)
    ent["day"] = [
        datetime.strptime(x, ISO).replace(tzinfo=UTC).astimezone(prague).date()
        for x in ent["exit_utc"]
    ]
    per_day = ent.groupby("day")["pct"].sum()
    out.update({
        "expectancy_r": round(float(r.mean()), 3),
        "total_r": round(float(r.sum()), 2),
        "total_pct": round(float(ent["pct"].sum()), 2),
        "win_rate": round(float((r > 0).mean()), 3),
        "win_rate_by_setup": {
            k: round(float((g["r"] > 0).mean()), 3) for k, g in ent.groupby("setup")
        },
        "reached_2r": round(float(ent["reached_2r"].mean()), 3),
        "profit_factor": (
            round(float(r[r > 0].sum() / -r[r < 0].sum()), 2) if (r < 0).any() else None
        ),
        "max_drawdown_pct": round(dd, 2),
        "worst_day_pct": round(float(per_day.min()), 2),
        "daily_limit_breaches": int((per_day <= -cfg.ftmo.daily_loss_pct).sum()),
        "max_loss_breached": bool(eq.min() <= -cfg.ftmo.max_loss_pct) if len(eq) else False,
        "exit_reasons": dict(Counter(ent["exit_reason"])),
        "avg_adds": round(float(ent["adds"].mean()), 2),
        "best_r": round(float(r.max()), 2),
        "worst_r": round(float(r.min()), 2),
    })  # fmt: skip
    return out
