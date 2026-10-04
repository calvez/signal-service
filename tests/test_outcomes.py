import csv

import pandas as pd
import pytest

from app import db, outcomes, reports
from app.outcomes import simulate
from tests.test_telegram import CHAT, NOW, SYMBOL, TOKEN, FakeTelegram, seed_read, ts  # noqa: F401

S = ts(2026, 10, 5, 7, 25)  # signal bar open
LONG = {"direction": "long", "entry": 100.0, "stop": 90.0, "target": 120.0}  # 2R
SHORT = {"direction": "short", "entry": 100.0, "stop": 110.0, "target": 80.0}
FAR = ts(2026, 10, 5, 15, 30)  # EU cash close: horizon far away


def bars(rows, first=S + 300):
    """rows: (h, l) per consecutive M5 bar starting at `first` (open = close = midpoint)."""
    idx = pd.to_datetime([first + 300 * i for i in range(len(rows))], unit="s", utc=True)
    mids = [(h + low) / 2 for h, low in rows]
    return pd.DataFrame(
        {"o": mids, "h": [h for h, _ in rows], "l": [r[1] for r in rows], "c": mids}, index=idx
    )


def run(setup, rows, horizon=FAR, now=None, first=S + 300):
    return simulate(setup, S, bars(rows, first), horizon, horizon + 5000 if now is None else now)


def test_long_win_and_loss():
    win = run(LONG, [(101, 95), (110, 99), (121, 100)], now=S + 2000)
    assert (win.status, win.r, win.entry_t, win.exit_t) == ("win", 2.0, S + 300, S + 900)
    loss = run(LONG, [(101, 95), (110, 99), (105, 89)], now=S + 2000)
    assert (loss.status, loss.r) == ("loss", -1.0)


def test_same_bar_stop_and_target_counts_as_loss():
    assert run(LONG, [(101, 95), (125, 85)], now=S + 2000).status == "loss"


def test_entry_bar_that_also_hits_stop_is_a_loss_but_target_is_a_win():
    assert run(LONG, [(101, 89)], now=S + 2000).status == "loss"
    assert run(LONG, [(125, 95)], now=S + 2000).status == "win"


def test_no_entry_within_three_bars():
    quiet = [(99, 95)] * 3
    assert run(LONG, quiet + [(150, 80)], now=S + 2000).status == "no_entry"  # 4th bar too late
    assert run(LONG, quiet, now=S + 2000).status == "no_entry"  # 3rd bar closed: final already
    assert run(LONG, quiet[:2], now=S + 2000).status == "pending"  # window still open


def test_entry_on_the_third_bar_counts_fourth_does_not():
    assert run(LONG, [(99, 95), (99, 95), (101, 95), (125, 100)], now=S + 2000).status == "win"
    assert (
        run(LONG, [(99, 95), (99, 95), (99, 95), (101, 95), (125, 100)], now=S + 2000).status
        == "no_entry"
    )


def test_short_mirror():
    assert run(SHORT, [(105, 99), (101, 79)], now=S + 2000).status == "win"
    assert run(SHORT, [(105, 99), (111, 98)], now=S + 2000).status == "loss"
    assert run(SHORT, [(105, 101)] * 2, now=S + 2000).status == "pending"  # window still open
    r = run(SHORT, [(105, 99), (111, 79)], now=S + 2000)  # both in one bar
    assert (r.status, r.r) == ("loss", -1.0)


def test_unresolved_at_horizon_is_expired_marked_to_market():
    h = S + 300 * 5  # horizon right after 4 bars
    r = run(LONG, [(101, 95), (105, 99), (106, 100), (107, 104)], horizon=h, now=h)
    assert r.status == "expired"
    assert r.r == pytest.approx(0.55)  # last close 105.5 on entry 100, risk 10


def test_pending_until_the_data_is_complete():
    r = run(LONG, [(101, 95), (105, 99)], now=S + 900)
    assert r.status == "pending" and r.entry_t == S + 300 and r.r is None
    assert run(LONG, [], now=S + 100).status == "pending"


def test_gives_up_when_bars_never_arrive():
    assert run(LONG, [(101, 95)], horizon=S + 3000, now=S + 3700).status == "expired"
    assert run(LONG, [], horizon=S + 3000, now=S + 3700).status == "no_entry"


def test_bars_outside_the_replay_window_are_ignored():
    # a bar at the signal's own open would hit the target, and one after the horizon too
    df = pd.concat([bars([(125, 95)], first=S), bars([(99, 95), (99, 95), (99, 95)], first=S + 300),
                    bars([(125, 100)], first=FAR)])  # fmt: skip
    assert simulate(LONG, S, df, FAR, FAR + 5000).status == "no_entry"


# ------------------------------------------------------------------ database side
def put_bars(settings, rows, first=S + 300):
    c = db.connect(settings.db_path)
    df = bars(rows, first)
    db.upsert_bars(c, [{"symbol": SYMBOL, "tf": "M5", "t_server": 0, "t_utc": int(t.timestamp()),
                        "o": r.o, "h": r.h, "l": r.l, "c": r.c, "tv": 1, "sp": 1, "received_at": 0}
                       for t, r in df.iterrows()])  # fmt: skip
    c.close()


def make_read(settings, **kw):
    db.init_db(settings.db_path)
    return seed_read(settings, bar=S, **kw)  # entry 24325, stop 24298, target 24379 (2R)


def test_update_outcomes_stores_and_finalises(settings):
    rid = make_read(settings)
    cfg = settings.config
    c = db.connect(settings.db_path)
    # nothing yet -> pending
    outcomes.update_outcomes(c, cfg, S + 400)
    assert c.execute("SELECT status FROM outcomes").fetchone()[0] == "pending"
    put_bars(settings, [(24330, 24310), (24350, 24320)])
    outcomes.update_outcomes(c, cfg, S + 1000)
    assert c.execute("SELECT status FROM outcomes").fetchone()[0] == "pending"  # entered, open
    put_bars(settings, [(24330, 24310), (24350, 24320), (24380, 24340)])
    assert outcomes.update_outcomes(c, cfg, S + 1200) == 1
    row = c.execute("SELECT status, r FROM outcomes WHERE read_id = ?", (rid,)).fetchone()
    assert tuple(row) == ("win", 2.0)
    assert outcomes.update_outcomes(c, cfg, S + 1300) == 0  # final results are not redone
    c.close()


def test_none_reads_have_no_outcome(settings):
    db.init_db(settings.db_path)
    seed_read(settings, action="none", push=0, bar=S)
    c = db.connect(settings.db_path)
    assert outcomes.update_outcomes(c, settings.config, S + 400) == 0


def test_summary_and_his_picks(settings):
    db.init_db(settings.db_path)
    win = seed_read(settings, bar=S)
    loss = seed_read(settings, bar=S + 1800)
    put_bars(settings, [(24330, 24310), (24380, 24340)])  # first read wins
    put_bars(settings, [(24330, 24310), (24330, 24290)], first=S + 1800 + 300)  # second loses
    c = db.connect(settings.db_path)
    outcomes.update_outcomes(c, settings.config, S + 4000)
    db.add_feedback(c, win, "take", S + 400)
    db.add_feedback(c, loss, "skip", S + 2000)
    s = outcomes.summarize(c, 0, 2**40)
    assert (s["n"], s["win"], s["loss"], s["r"]) == (2, 1, 1, 1.0)
    mine = outcomes.summarize(c, 0, 2**40, only_taken=True)
    assert (mine["n"], mine["win"], mine["r"]) == (1, 1, 2.0)
    line = outcomes.summary_line("AI alerts", s)
    assert line.startswith("Simulated AI alerts: 2") and line.endswith("+1.0R")
    c.close()


def test_reports_show_simulated_numbers(settings):
    db.init_db(settings.db_path)
    seed_read(settings, bar=S)
    c = db.connect(settings.db_path)
    outcomes.update_outcomes(c, settings.config, S + 400)
    assert "Simulated AI alerts: 1 → 0 win · 0 loss · 0 expired · 0 no entry · 1 pending" in (
        reports.today_text(settings, c, NOW)
    )
    assert "Simulated" in reports.daily_report_text(settings, c, NOW)
    assert "Simulated" in reports.wrap_text(settings, c, "eu", S - 1500, S + 1500)
    c.close()


# ------------------------------------------------------------------ CSV
def test_csv_export_has_everything_needed_for_analysis(settings, tmp_path):
    db.init_db(settings.db_path)
    rid = seed_read(settings, bar=S)
    put_bars(settings, [(24330, 24310), (24380, 24340)])
    c = db.connect(settings.db_path)
    outcomes.update_outcomes(c, settings.config, S + 4000)
    db.add_feedback(c, rid, "take", S + 400)
    path = tmp_path / "x.csv"
    assert outcomes.write_csv(c, 0, 2**40, path) == 1
    (row,) = list(csv.DictReader(open(path, encoding="utf-8")))
    assert list(row) == outcomes.CSV_COLUMNS
    assert (row["symbol"], row["direction"], row["grade"], row["choice"]) == (
        SYMBOL,
        "long",
        "A",
        "take",
    )
    assert (row["sim_status"], row["sim_r"]) == ("win", "2.0")
    assert row["bar_time_utc"] == "2026-10-05T07:25:00Z" and row["entry"] == "24325.0"
    c.close()


def test_weekly_export_runs_once_on_saturday_morning(settings):
    import httpx

    from app.scheduler import TelegramService
    from app.telegram import TelegramApi

    settings.secrets.telegram_chat_id = CHAT
    db.init_db(settings.db_path)
    seed_read(settings, bar=S)
    fake = FakeTelegram()
    svc = TelegramService(
        settings, TelegramApi(TOKEN, httpx.MockTransport(fake)), started_at=NOW - 9999
    )
    c = db.connect(settings.db_path)
    sat_10 = ts(2026, 10, 10, 8, 5)  # 10:05 Berlin, Saturday
    assert svc.weekly_export(c, ts(2026, 10, 9, 8, 5)) is None  # Friday
    name, content = svc.weekly_export(c, sat_10)
    assert name == "reads_2026-W41.csv" and b"read_id" in content and b"GER40.cash" in content
    assert svc.weekly_export(c, sat_10 + 60) is None  # once per week
    c.close()


# ------------------------------------------------------------------ M1 bars (backtests)
def m1(rows, first=S + 300):
    idx = pd.to_datetime([first + 60 * i for i in range(len(rows))], unit="s", utc=True)
    mids = [(h + low) / 2 for h, low in rows]
    return pd.DataFrame(
        {"o": mids, "h": [h for h, _ in rows], "l": [r[1] for r in rows], "c": mids}, index=idx
    )


def test_m1_resolves_what_m5_has_to_call_a_loss():
    # One M5 bar touching target AND stop is a loss on M5 ...
    assert run(LONG, [(101, 95), (125, 85)], now=S + 2000).status == "loss"
    # ... but on M1 the target (125) came first and the stop (85) only later in that 5 minutes
    rows = [(101, 95)] * 5 + [(125, 100), (110, 100), (100, 85), (95, 90), (95, 90)]
    r = simulate(LONG, S, m1(rows), FAR, S + 4000, bar_seconds=60)
    assert (r.status, r.r) == ("win", 2.0)


def test_m1_entry_window_is_the_same_15_minutes():
    quiet = [(99, 95)] * 15  # three M5 bars without a trigger
    assert (
        simulate(LONG, S, m1(quiet + [(130, 99)]), FAR, S + 4000, bar_seconds=60).status
        == "no_entry"
    )
    late_trigger = [(99, 95)] * 14 + [(101, 95), (125, 100)]  # trigger in the last minute
    assert simulate(LONG, S, m1(late_trigger), FAR, S + 4000, bar_seconds=60).status == "win"
    assert simulate(LONG, S, m1([(99, 95)] * 10), FAR, S + 900, bar_seconds=60).status == "pending"


def test_m1_bars_inside_the_signal_bar_are_ignored():
    inside = m1([(130, 99)] * 4, first=S + 60)  # minutes 1-4 of the signal bar itself
    after = m1([(99, 95)] * 15 + [(99, 95)])
    r = simulate(LONG, S, pd.concat([inside, after]), FAR, S + 4000, bar_seconds=60)
    assert r.status == "no_entry"
