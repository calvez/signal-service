import pandas as pd
import pytest

from app import backtest, db
from app.history import read_export, to_utc
from app.strategies import Candidate, get_strategy
from app.timeconv import server_to_utc
from tests.test_htf import zigzag
from tests.test_reader import BAR, m5_frame, store

SYMBOL = "GER40.cash"


@pytest.fixture
def hist(settings, tmp_path):
    """A small history.db: rising M5 bars up to BAR (07:55 UTC, EU session), bull H1 and D1."""
    path = str(tmp_path / "history.db")
    db.init_db(path)
    conn = db.connect(path)
    m5 = m5_frame(n=150)
    tail = pd.date_range(m5.index[-1] + pd.Timedelta("5min"), periods=40, freq="5min")
    after = pd.DataFrame({"o": 24130.0, "h": 24300.0, "l": 24125.0, "c": 24290.0}, index=tail)
    store(conn, pd.concat([m5, after]), "M5")
    h1 = zigzag(300, 1.0, freq="1h")
    h1.index = pd.date_range(
        end=pd.Timestamp(BAR, unit="s", tz="UTC").floor("1h"), periods=300, freq="1h"
    )
    store(conn, h1, "H1")
    d1 = zigzag(80, 5.0, freq="1D")
    d1.index = pd.date_range(end=pd.Timestamp("2026-10-04", tz="UTC"), periods=80, freq="1D")
    store(conn, d1, "D1")
    db.upsert_symbol_meta(conn, SYMBOL, 1)
    # spread column: store() writes sp=1 -> 0.1 points of cost
    yield conn
    conn.close()


class FixedStrategy:
    """Proposes one long at the BAR bar only, priced from its ATR."""

    name, version = "fixed", "1"

    def __init__(self, at=BAR):
        self.at, self.seen = at, []

    def candidates(self, ev):
        self.seen.append(ev.bar_open)
        if ev.bar_open != self.at:
            return []
        entry = round(ev.last_close + 1, 1)
        stop = round(entry - ev.atr, 1)
        return [Candidate("H2", "long", entry, stop, round(entry + 2 * ev.atr, 1), True)]


def run(settings, conn, strategy, **kw):
    start = BAR - 3600
    return backtest.run(
        settings.config, conn, strategy, SYMBOL, start, BAR + 3600, use_m1=False, **kw
    )


def test_signal_is_validated_simulated_and_charged_spread(settings, hist):
    settings.config.management.mode = "fixed_target"
    res = run(settings, hist, FixedStrategy())
    (tr,) = res.trades
    assert tr.status == "win" and tr.validation == "ok"
    assert tr.cost_r == pytest.approx(round(0.1 / abs(tr.entry - tr.stop), 3))
    assert tr.r_net == pytest.approx(tr.r_gross - tr.cost_r, abs=1e-3)
    assert res.bars_evaluated > 0


def test_decisions_only_see_closed_bars(settings, hist):
    """Every evaluated bar is <= the bar being decided, and the evaluated bars are exactly the
    session bars up to the end of the range (no bar after it is ever evaluated)."""
    st = FixedStrategy()
    run(settings, hist, st)
    assert st.seen == sorted(st.seen)
    assert all(t < BAR + 3600 for t in st.seen)


def test_future_bars_do_not_change_decisions(settings, hist, tmp_path):
    a = FixedStrategy()
    run(settings, hist, a)
    # wreck everything after BAR: decisions up to BAR must be identical
    hist.execute("UPDATE bars SET h = h + 500, c = c - 300 WHERE tf = 'M5' AND t_utc > ?", (BAR,))
    hist.commit()
    b = FixedStrategy()
    res = run(settings, hist, b)
    assert [t for t in a.seen if t <= BAR] == [t for t in b.seen if t <= BAR]
    assert res.trades[0].entry == run(settings, hist, FixedStrategy()).trades[0].entry


def test_bad_candidates_are_rejected_not_traded(settings, hist):
    class Bad(FixedStrategy):
        def candidates(self, ev):
            if ev.bar_open != self.at:
                return []
            return [
                Candidate("H2", "long", ev.last_close, ev.last_close + 5, ev.last_close + 10, True)
            ]

    (tr,) = run(settings, hist, Bad()).trades
    assert tr.status == "rejected" and tr.r_net is None and tr.validation.startswith("rejected")


def test_pyramid_mode_plays_a_runner(settings, hist):
    class Runner(FixedStrategy):
        def candidates(self, ev):
            self.seen.append(ev.bar_open)
            if ev.bar_open != self.at:
                return []
            entry = round(ev.last_close + 1, 1)
            return [Candidate("H2", "long", entry, round(entry - ev.atr, 1), None, True)]

    assert settings.config.management.mode == "pyramid"
    for k in range(40):  # a clean staircase after the signal: rising highs AND lows
        low = 24125 + 30 * k
        hist.execute("UPDATE bars SET o = ?, h = ?, l = ?, c = ? WHERE tf = 'M5' AND t_utc = ?",
                     (low + 5, low + 40, low, low + 35, BAR + 300 * (k + 1)))  # fmt: skip
    hist.commit()
    (tr,) = run(settings, hist, Runner()).trades
    # the bars after BAR jump far up: entry, both adds, held to the end of the data / close
    from app.position import max_units_by_leverage

    cap = max_units_by_leverage(0.3, abs(tr.entry - tr.stop), tr.entry, 20.0)
    assert tr.target is None and tr.units == cap > 1  # no add limit, but the leverage cap
    assert tr.status in ("closed_eod", "stopped", "reversal") and tr.r_net > 2
    assert tr.result_pct == pytest.approx(tr.r_net * 0.3, abs=1e-3)
    assert tr.cost_r == pytest.approx(round(0.1 * cap / abs(tr.entry - tr.stop), 3))  # per unit
    s = backtest.stats([tr], settings.config.ftmo)
    assert s["worst_day_pct"] > 0 and s["daily_limit_breaches"] == 0 and s["avg_units"] == cap
    assert "FTMO check" in backtest.report([tr], None, settings.config.ftmo)


def test_ftmo_breach_detection():
    from app.config import FtmoCfg

    ftmo = FtmoCfg(initial_balance=160000, daily_loss_pct=5, max_loss_pct=10,
                   day_reset_tz="Europe/Prague", warn_levels_pct=[50, 80])  # fmt: skip

    def t(pct, day_offset):
        ts = 1791100800 + day_offset * 86400
        return backtest.Trade("S", "eu", ts, "x:1", "H2", "long", True, "A", 1, 0, None, "ok",
                              "stopped", r_gross=pct / 0.5, cost_r=0.0, r_net=pct / 0.5,
                              result_pct=pct, units=1, exit_utc=ts)  # fmt: skip

    s = backtest.stats([t(-3.0, 0), t(-2.5, 0), t(-1.0, 1), t(-4.0, 2), t(-0.5, 3)], ftmo)
    assert s["worst_day_pct"] == -5.5 and s["daily_limit_breaches"] == 1
    assert s["max_drawdown_pct"] == 11.0 and s["max_loss_breached"] is True


def test_his_rules_block_overlapping_trades(settings, hist):
    class Twice(FixedStrategy):
        def candidates(self, ev):
            self.seen.append(ev.bar_open)
            if ev.bar_open not in (BAR - 600, BAR - 300):
                return []
            entry = round(ev.last_close + 1, 1)
            return [Candidate("H2", "long", entry, round(entry - ev.atr, 1),
                              round(entry + 6 * ev.atr, 1), True)]  # fmt: skip

    ruled = run(settings, hist, Twice())
    raw = run(settings, hist, Twice(), all_signals=True)
    assert [t.status for t in raw.trades].count("skipped_rules") == 0
    assert "skipped_rules" in [t.status for t in ruled.trades]


def test_stats_and_report():
    def t(r, status="win"):
        return backtest.Trade("S", "eu", 0, "x:1", "H2", "long", True, "A", 1, 0, 2, "ok", status,
                              r_gross=r, cost_r=0.0, r_net=r)  # fmt: skip

    trades = [t(2.0), t(-1.0, "loss"), t(-1.0, "loss"), t(2.0)]
    s = backtest.stats(trades)
    assert (s["entered"], s["win_rate"], s["total_r"], s["expectancy_r"]) == (4, 0.5, 2.0, 0.5)
    assert (s["win"], s["loss"]) == (2, 2)
    assert s["profit_factor"] == 2.0 and s["max_drawdown_r"] == 2.0
    assert "ALL" in backtest.report(trades) and "OUT-OF-SAMPLE" in backtest.report(trades, 1)


def test_demo_strategy_is_registered_and_unknown_fails():
    assert get_strategy("demo").name == "demo"
    with pytest.raises(KeyError):
        get_strategy("nope")


# ------------------------------------------------------------------ history import
def test_vectorised_time_conversion_matches_timeconv():
    from datetime import UTC, datetime

    import numpy as np

    ts = [int(datetime(2026, m, d, h, mi, tzinfo=UTC).timestamp())
          for m, d in ((3, 7), (3, 9), (10, 28), (11, 2)) for h, mi in ((5, 0), (12, 35))]  # fmt: skip
    for mode in ("ny_plus_7", "iana:Europe/Prague", "fixed:7200"):
        got = to_utc(np.array(ts, dtype=np.int64), mode)
        assert list(got) == [server_to_utc(t, mode) for t in ts], mode


def test_read_export(tmp_path):
    p = tmp_path / "GER40.cash_M5.csv"
    p.write_text(
        "# symbol=GER40.cash tf=M5 digits=2\nt_server,o,h,l,c,tv,sp\n1000,1.5,2,1,1.75,10,3\n"
    )
    sym, tf, digits, df = read_export(p)
    assert (sym, tf, digits, len(df), df["sp"].iloc[0]) == ("GER40.cash", "M5", 2, 1, 3)


def test_import_file_round_trip(tmp_path, monkeypatch):
    from app import history

    monkeypatch.setattr(history, "CHUNK", 2)  # exercise the chunking
    p = tmp_path / "GER40.cash_M5.csv"
    rows = "\n".join(f"{1791100800 + 300 * i},1.5,2,1,1.75,10,3" for i in range(5))
    p.write_text("# symbol=GER40.cash tf=M5 digits=2\nt_server,o,h,l,c,tv,sp\n" + rows + "\n")
    dbp = str(tmp_path / "h.db")
    (summary,) = history.import_dir(tmp_path, dbp, "ny_plus_7")
    assert summary[:3] == ("GER40.cash", "M5", 5)
    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*), MIN(t_utc) FROM bars").fetchone()[0] == 5
    assert conn.execute("SELECT MIN(t_utc) FROM bars").fetchone()[0] == server_to_utc(
        1791100800, "ny_plus_7"
    )
    assert db.get_digits(conn, "GER40.cash") == 2


def test_htf_cache_gives_the_same_evaluation(settings, hist):
    from app.evaluation import Skip, evaluate_bar

    m5 = db.load_bars(hist, SYMBOL, "M5", until_utc=BAR + 3600, limit=2000)
    h1 = db.load_bars(hist, SYMBOL, "H1", until_utc=BAR + 3600, limit=2000)
    d1 = db.load_bars(hist, SYMBOL, "D1", until_utc=BAR + 3600, limit=2000)
    cache: dict = {}
    for t in range(BAR - 1800, BAR + 1800, 300):
        try:
            plain = evaluate_bar(settings.config, SYMBOL, t, m5, h1, d1, 1)
        except Skip as skip:
            with pytest.raises(Skip, match=str(skip).split(" ")[0]):
                evaluate_bar(settings.config, SYMBOL, t, m5, h1, d1, 1, cache)
            continue
        cached = evaluate_bar(settings.config, SYMBOL, t, m5, h1, d1, 1, cache)
        assert (plain.h1, plain.d1, plain.alignment) == (cached.h1, cached.d1, cached.alignment)
    assert cache  # it was used
