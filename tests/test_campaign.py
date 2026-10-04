"""docs/spec-brooks-ea.md §4-§8: the campaign state machine on hand-built bars."""

import math

import pandas as pd
import pytest

from app import campaign as C
from app.backtest_campaign import sim_broker
from app.config import BrooksCfg

CFG = BrooksCfg()
T = 1_791_180_000  # signal bar open (multiple of 300)
END = T + 7200  # session end
TICK = 0.1


def start(direction="long", entry=100.0, sl=90.0, cfg=CFG, spread=0.0):
    c, act = C.Campaign.start("X-1", "GER40.cash", direction, entry, sl, TICK, spread, T, END,
                              "H2", cfg)  # fmt: skip
    return c, act


def bar(o, h, low, c, ema=50.0, last_sl=math.nan, last_sh=math.nan, avg_range=10.0):
    return pd.Series({"o": o, "h": h, "l": low, "c": c, "ema": ema, "last_sl_price": last_sl,
                      "last_sh_price": last_sh, "avg_range": avg_range})  # fmt: skip


def minute_bars(rows, first, sp=0.0):
    idx = pd.to_datetime([first + 60 * i for i in range(len(rows))], unit="s", utc=True)
    return pd.DataFrame([dict(o=o, h=h, l=lo, c=c, sp=sp) for o, h, lo, c in rows], index=idx)


def fill_now(c, price=100.0, t=T + 300):
    c.on_fill(c.orders[0].id, price, t, CFG)


class NoContext:
    """Strategy stub: no flip, no add signal."""

    def flip_ok(self, ev, direction):
        return True, 0

    def add_signal(self, ev, direction):
        return None


# ------------------------------------------------------------------ entry order (§4)
def test_entry_order_with_stop_and_lifetime():
    c, act = start()
    assert act.kind == "place" and act.order.price == 100 and act.order.sl == 90
    assert act.order.expires_at == T + 600  # cancel at the close of the following bar
    acts = c.on_bar_close(bar(99, 99.5, 95, 99), T + 600, None, None, CFG, None)
    assert (
        [a.kind for a in acts] == ["cancel"]
        and c.status == "cancelled"
        and c.cancel_reason == "EXPIRED"
    )


def test_sb_low_broken_before_the_fill_cancels():
    c, _ = start()
    sim_broker(c, minute_bars([(95, 96, 89.9, 92)], T + 300), 1.0, CFG)  # trades through 90 first
    assert c.status == "cancelled" and c.cancel_reason == "SB_LOW_BROKEN"


def test_fill_and_stop_in_the_same_minute_is_a_loss():
    c, _ = start()
    sim_broker(c, minute_bars([(95, 101, 89, 92)], T + 300), 1.0, CFG)
    assert c.status == "closed" and c.result_r() == pytest.approx(-1.0) and c.exit_reason == "SL"


def test_buy_stop_triggers_on_the_ask():
    c, _ = start()
    sim_broker(c, minute_bars([(98, 99.6, 97, 99)], T + 300, sp=5), 0.1, CFG)  # bid 99.6 + 0.5
    assert c.status == "open" and c.positions[0].entry == 100.0


def test_gap_fills_at_the_open():
    c, _ = start()
    sim_broker(c, minute_bars([(103, 104, 102, 103)], T + 300), 1.0, CFG)
    assert c.positions[0].entry == 103.0


# ------------------------------------------------------------------ stop management (§7, §8.4)
def test_breakeven_at_plus_one_r():
    c, _ = start(spread=0.5)
    fill_now(c)
    acts = c.on_bar_close(bar(100, 110.5, 99, 108), T + 600, None, NoContext(), CFG, None)
    assert acts[-1].kind == "modify_sl" and c.positions[0].sl == pytest.approx(100.5)  # + spread


def test_trailing_behind_new_higher_lows_only_after_one_r():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(100, 105, 99, 104, last_sl=97), T + 600, None, NoContext(), CFG, None)
    assert c.positions[0].sl == 90  # below +1R: unchanged
    c.on_bar_close(bar(104, 111, 103, 110, last_sl=103), T + 900, None, NoContext(), CFG, None)
    assert c.positions[0].sl == pytest.approx(102.9)  # max(BE 100, swing 103 - tick)
    c.on_bar_close(bar(110, 112, 108, 111, last_sl=101), T + 1200, None, NoContext(), CFG, None)
    assert c.positions[0].sl == pytest.approx(102.9)  # lower swing low: never back


def test_climax_bar_tightens_the_stop():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(100, 111, 99, 110), T + 600, None, NoContext(), CFG, None)
    c.on_bar_close(bar(110, 140, 109, 139, avg_range=10), T + 900, None, NoContext(), CFG, None)
    assert c.positions[0].sl == pytest.approx(108.9)  # range 31 > 2.5 x 10: below its low


def test_stop_hit_after_breakeven_is_reported_as_be():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(100, 111, 99, 108), T + 600, None, NoContext(), CFG, None)
    sim_broker(c, minute_bars([(105, 106, 99, 100)], T + 600), 1.0, CFG)
    assert c.exit_reason == "BE" and c.result_r() == pytest.approx(0.0)


# ------------------------------------------------------------------ exits (§5, §7, §8.5)
def test_time_exit():
    c, _ = start()
    fill_now(c)
    for k in range(6):  # never reaches +0.5R
        c.on_bar_close(bar(100, 103, 98, 101), T + 600 + 300 * k, None, NoContext(), CFG, None)
    assert c.status == "closed" and c.exit_reason == "TIME"


def test_end_of_session_and_daily_limit_close_everything():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(100, 104, 99, 103), T + 600, None, NoContext(), CFG, "EOS")
    assert c.exit_reason == "EOS" and c.result_r() == pytest.approx(0.3)
    d, _ = start()
    d.on_bar_close(bar(99, 99.5, 95, 99), T + 600, None, NoContext(), CFG, "DAILY_LIMIT")
    assert d.status == "cancelled" and d.cancel_reason == "DAILY_LIMIT"


def test_always_in_flip_strong_bear_bar_below_ema():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(104, 104.5, 95, 95.5, ema=97), T + 600, None, NoContext(), CFG, None)
    assert c.exit_reason == "ALWAYS_IN_FLIP"


def test_close_below_the_last_higher_low_fails_the_setup():
    c, _ = start()
    fill_now(c)
    c.on_bar_close(bar(100, 101, 95, 96, last_sl=97), T + 600, None, NoContext(), CFG, None)
    assert c.exit_reason == "FAILED_SETUP"


def test_fill_beyond_the_stop_is_closed_at_once():
    c, _ = start()
    c.on_fill(c.orders[0].id, 89.0, T + 300, CFG)  # gap below the stop
    assert c.status == "closed" and c.exit_reason == "FAILED_SETUP"


def test_variant_c_fixed_take_profit():
    cfg = CFG.model_copy(update={"exit_mode": "fixed_tp", "enable_pyramiding": False})
    c, _ = start(cfg=cfg)
    c.on_fill(c.orders[0].id, 100.0, T + 300, cfg)
    assert c.positions[0].tp == 120.0
    for k in range(8):  # no time exit, no breakeven in variant C
        c.on_bar_close(bar(100, 104, 98, 101), T + 600 + 300 * k, None, NoContext(), cfg, None)
    assert c.status == "open" and c.positions[0].sl == 90
    sim_broker(c, minute_bars([(110, 121, 109, 120)], T + 3000), 1.0, cfg)
    assert c.exit_reason == "TP" and c.result_r() == pytest.approx(2.0)


# ------------------------------------------------------------------ adds (§8)
class Adds(NoContext):
    def __init__(self, signal):
        self.signal = signal

    def add_signal(self, ev, direction):
        return self.signal


def test_add_needs_one_r_breakeven_and_a_higher_low():
    c, _ = start()
    fill_now(c)
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    acts = c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(sig), CFG, None)
    add = [a for a in acts if a.kind == "place"]
    assert add and add[0].order.kind == "add" and add[0].order.size == 0.5
    # sizing: campaign open risk stays <= 1R; the first position is at breakeven -> 0 open risk
    assert c.open_risk_r() == 0.0


def test_no_add_below_one_r_or_without_breakeven():
    c, _ = start()
    fill_now(c)
    sig = {"entry": 108.0, "stop": 101.9, "pullback_low": 102.0}
    acts = c.on_bar_close(bar(100, 106, 101, 105), T + 600, object(), Adds(sig), CFG, None)
    assert not [a for a in acts if a.kind == "place"]  # only +0.5R


def test_add_skipped_without_a_higher_low():
    c, _ = start()
    fill_now(c)
    sig = {"entry": 116.0, "stop": 98.9, "pullback_low": 99.0}  # below the first entry
    acts = c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(sig), CFG, None)
    assert not [a for a in acts if a.kind == "place"]


def test_add_size_capped_by_campaign_risk():
    """§8.3: every stop is at breakeven before an add (§8.1), so the cap binds through the add's
    own risk: with a 3R-wide add stop, 0.5 x would risk 1.5R; it is cut to 1R -> 0.333 x."""
    c, _ = start()
    fill_now(c)
    sig = {"entry": 116.0, "stop": 86.0, "pullback_low": 105.0}  # 30 points = 3R per unit
    acts = c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(sig), CFG, None)
    (add,) = [a for a in acts if a.kind == "place"]
    assert add.order.size == pytest.approx(1 / 3, abs=1e-3)


def test_no_add_while_a_stop_is_below_its_entry():
    c, _ = start()
    fill_now(c)
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    cfg = CFG.model_copy(update={"use_be": False})  # nothing moves the stop to breakeven
    acts = c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(sig), cfg, None)
    assert not [a for a in acts if a.kind == "place"]


def test_two_adds_then_stop_and_sizes():
    c, _ = start()
    fill_now(c)
    s1 = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(s1), CFG, None)
    c.on_fill(c.orders[0].id, 116.0, T + 700, CFG)
    c.on_bar_close(bar(110, 117, 110, 116), T + 900, object(), Adds(None), CFG, None)
    for p in c.positions:  # the add needs its own breakeven before a second add
        p.sl = max(p.sl, p.entry)
    s2 = {"entry": 130.0, "stop": 118.9, "pullback_low": 119.0}
    c.on_bar_close(bar(116, 129, 120, 128), T + 1200, object(), Adds(s2), CFG, None)
    assert c.orders and c.orders[0].size == 0.25
    c.on_fill(c.orders[0].id, 130.0, T + 1300, CFG)
    assert c.adds == 2
    s3 = {"entry": 140.0, "stop": 128.9, "pullback_low": 129.0}
    c.on_bar_close(bar(130, 139, 131, 138), T + 1500, object(), Adds(s3), CFG, None)
    assert not c.orders  # max_adds = 2


def test_adds_blocked_by_flag():
    c, _ = start()
    fill_now(c)
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    acts = c.on_bar_close(bar(100, 111, 101, 110), T + 600, object(), Adds(sig), CFG, None,
                          adds_allowed=False)  # fmt: skip
    assert not [a for a in acts if a.kind == "place"]


# ------------------------------------------------------------------ shorts (mirror)
def test_short_campaign_mirror():
    c, act = start("short", entry=100.0, sl=110.0)
    assert act.order.cancel_level == 110.0
    sim_broker(c, minute_bars([(101, 101.5, 99.5, 100)], T + 300, sp=0), 1.0, CFG)
    assert c.status == "open"
    c.on_bar_close(bar(100, 101, 89.5, 90.5, ema=150), T + 600, None, NoContext(), CFG, None)
    assert c.positions[0].sl == pytest.approx(100.0)  # breakeven
    sim_broker(c, minute_bars([(91, 100.5, 90, 99)], T + 600), 1.0, CFG)
    assert c.exit_reason == "BE" and c.result_r() == pytest.approx(0.0)


# ------------------------------------------------------------------ the driver on a small history
def test_driver_runs_a_campaign_end_to_end(settings, tmp_path):
    from app import backtest_campaign as bt
    from app import db
    from app.strategies import Candidate
    from tests.test_backtest import hist as _hist  # noqa: F401  (fixture factory below)
    from tests.test_reader import BAR, m5_frame, store

    path = str(tmp_path / "h.db")
    db.init_db(path)
    conn = db.connect(path)
    m5 = m5_frame(n=150)
    tail = pd.date_range(m5.index[-1] + pd.Timedelta("5min"), periods=24, freq="5min")
    base = [24120 + 6 * k for k in range(24)]  # the first bar already trades above the entry
    up = pd.DataFrame({"o": base, "h": [x + 8 for x in base], "l": [x - 2 for x in base],
                       "c": [x + 7 for x in base]}, index=tail, dtype=float)  # fmt: skip
    store(conn, pd.concat([m5, up]), "M5")
    from tests.test_htf import zigzag

    h1 = zigzag(300, 1.0, freq="1h")
    h1.index = pd.date_range(
        end=pd.Timestamp(BAR, unit="s", tz="UTC").floor("1h"), periods=300, freq="1h"
    )
    store(conn, h1, "H1")
    d1 = zigzag(80, 5.0, freq="1D")
    d1.index = pd.date_range(end=pd.Timestamp("2026-10-04", tz="UTC"), periods=80, freq="1D")
    store(conn, d1, "D1")
    db.upsert_symbol_meta(conn, "GER40.cash", 1)

    class Once(NoContext):
        name, version = "once", "1"

        def candidates(self, ev):
            if ev.bar_open != BAR:
                return []
            entry = round(float(ev.last["h"]) + 0.1, 1)
            stop = round(entry - ev.atr, 1)
            return [
                Candidate("H2", "long", entry, stop, None, True, "A", {"pullback_low": stop + 0.1})
            ]

    cfg = settings.config.model_copy(update={"rules": settings.config.rules.model_copy(
        update={"require_htf_alignment": False})})  # fmt: skip
    res = bt.run(cfg, conn, Once(), ["GER40.cash"], BAR - 3600, BAR + 3 * 3600, use_m1=False)
    (c,) = res.campaigns
    assert c.status == "closed" and c.exit_reason in ("EOS", "TRAIL", "BE", "TIME")
    df = bt.campaign_rows(res.campaigns, 0.3)
    s = bt.stats(df, cfg)
    assert s["entered"] == 1 and "worst_day_pct" in s and s["max_loss_breached"] is False


# ------------------------------------------------------------------ AI trade management
class Fixed:
    """Advisor stub: always answers `choice`, and records what it was asked."""

    name = "stub"

    def __init__(self, choice):
        self.choice = choice
        self.points = []

    def decide(self, point, campaign, ev):
        self.points.append(point)
        return self.choice, "stub"


def running(cfg=CFG):
    """A campaign that is open, +1R, with the stop at breakeven."""
    c, _ = start(cfg=cfg)
    fill_now(c)
    c.on_bar_close(bar(100, 110.5, 99, 108), T + 600, None, NoContext(), cfg, None)
    return c


def test_advisor_is_only_asked_after_the_hard_rules():
    """A hard rule (always-in flip) closes the trade without consulting the advisor."""
    c = running()
    adv = Fixed("hold")
    c.on_bar_close(bar(104, 104.5, 95, 95.5, ema=97), T + 900, None, NoContext(), CFG, None,
                   advisor=adv)  # fmt: skip
    assert c.exit_reason == "ALWAYS_IN_FLIP" and adv.points == []


def test_advisor_can_close_early_on_a_warning_sign():
    c = running()
    adv = Fixed("close_all")
    # strong bar against us, but above the EMA: no hard rule fires
    c.on_bar_close(bar(108, 108.5, 103, 103.5, ema=95), T + 900, None, NoContext(), CFG, None,
                   advisor=adv)  # fmt: skip
    assert c.exit_reason == "AI_EXIT" and c.status == "closed"
    (p,) = adv.points
    assert p.kind == "exit" and p.rule_choice == "hold" and p.safe_choice == "hold"
    assert any("strong bar against" in t for t in p.triggers)
    assert c.ai[-1]["choice"] == "close_all" and c.ai[-1]["rule_choice"] == "hold"


def test_advisor_hold_keeps_the_spec_behaviour():
    c = running()
    c.on_bar_close(bar(108, 108.5, 103, 103.5, ema=95), T + 900, None, NoContext(), CFG, None,
                   advisor=Fixed("hold"))  # fmt: skip
    assert c.status == "open" and c.exit_reason is None


def test_invalid_advisor_answer_falls_back_to_the_safe_choice():
    c = running()
    c.on_bar_close(bar(108, 108.5, 103, 103.5, ema=95), T + 900, None, NoContext(), CFG, None,
                   advisor=Fixed("sell_everything_now"))  # fmt: skip
    assert c.status == "open" and c.ai[-1]["choice"] == "hold"


def test_advisor_can_close_only_the_add_ons():
    c = running()
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    c.on_bar_close(bar(108, 111, 104, 110), T + 900, object(), Adds(sig), CFG, None)
    c.on_fill(c.orders[0].id, 116.0, T + 1000, CFG)
    assert c.adds == 1 and len(c.positions) == 2
    c.on_bar_close(bar(116, 116.5, 111, 111.5, ema=95), T + 1200, None, NoContext(), CFG, None,
                   advisor=Fixed("close_adds"))  # fmt: skip
    assert len(c.positions) == 1 and c.positions[0].kind == "entry" and c.status == "open"
    assert c.closed[-1]["reason"] == "AI_PARTIAL"


def test_advisor_can_veto_an_add():
    c = running()
    adv = Fixed("skip")
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    acts = c.on_bar_close(bar(108, 111, 104, 110), T + 900, object(), Adds(sig), CFG, None,
                          advisor=adv)  # fmt: skip
    assert not [a for a in acts if a.kind == "place"] and not c.orders
    assert [p.kind for p in adv.points] == ["add"] and adv.points[0].rule_choice == "take"
    assert adv.points[0].safe_choice == "skip"  # an AI failure never adds risk


def test_advisor_approving_an_add_matches_the_rules():
    c = running()
    sig = {"entry": 116.0, "stop": 104.9, "pullback_low": 105.0}
    acts = c.on_bar_close(bar(108, 111, 104, 110), T + 900, object(), Adds(sig), CFG, None,
                          advisor=Fixed("take"))  # fmt: skip
    assert [a.kind for a in acts if a.kind == "place"] == ["place"]


def test_advisor_tighten_only_moves_the_stop_forward():
    cfg = CFG.model_copy(update={"ai_tighten_gap_r": 0.5})
    c, _ = start(cfg=cfg)
    fill_now(c)
    c.on_bar_close(bar(100, 110.5, 99, 108), T + 600, None, NoContext(), cfg, None)  # BE at 100
    adv = Fixed("tighten_bar")
    # +1.15R open, stop still 1.15R behind the price -> a tighten point is offered
    c.on_bar_close(bar(108, 112, 106, 111.5), T + 900, None, NoContext(), cfg, None, advisor=adv)
    (p,) = [x for x in adv.points if x.kind == "tighten"]
    assert p.rule_choice == "keep" and p.safe_choice == "keep"
    assert c.positions[0].sl == pytest.approx(105.9)  # bar low 106 - 1 tick, above the old 100
    # a later bar cannot move it back
    c.on_bar_close(bar(108, 109, 101, 108.5), T + 1200, None, NoContext(), cfg, None,
                   advisor=Fixed("tighten_bar"))  # fmt: skip
    assert c.positions[0].sl >= 105.9


def test_no_tighten_point_when_the_stop_is_already_close():
    cfg = CFG.model_copy(update={"ai_tighten_gap_r": 5.0})
    c = running(cfg)
    adv = Fixed("keep")
    c.on_bar_close(bar(108, 109, 107, 108.5), T + 900, None, NoContext(), cfg, None, advisor=adv)
    assert not [x for x in adv.points if x.kind == "tighten"]


def test_give_back_trigger():
    c = running()
    adv = Fixed("hold")
    c.on_bar_close(bar(108, 125, 107, 124), T + 900, None, NoContext(), CFG, None, advisor=adv)
    c.on_bar_close(bar(124, 124.5, 112, 113), T + 1200, None, NoContext(), CFG, None, advisor=adv)
    assert any("given back" in t for p in adv.points for t in p.triggers)
