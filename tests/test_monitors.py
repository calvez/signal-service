from datetime import UTC, datetime

import pytest

from app import db, reports
from app.status import MonitorEngine, day_start_balance, in_quiet_hours
from tests.test_telegram import CHAT, NOW, SYMBOL, TOKEN, FakeTelegram, seed_read, ts  # noqa: F401


@pytest.fixture
def env(settings):
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    engine = MonitorEngine(settings, started_at=NOW - 10_000)
    yield settings, conn, engine
    conn.close()


def hb(conn, received_at=NOW, **over):
    row = dict(ea_version="1.00", account_login=1, server="FTMO-Demo", company="FTMO", balance=80000.0,
               equity=80000.0, connected=1, trade_allowed=0, positions=0, floating_pl=0.0,
               currency="EUR", time_server=received_at + 10800, server_utc_offset_sec=10800,
               received_at=received_at)  # fmt: skip
    row.update(over)
    db.insert_heartbeat(conn, row)


def bar(conn, t_utc):
    db.upsert_bars(conn, [{"symbol": SYMBOL, "tf": "M5", "t_server": 0, "t_utc": t_utc, "o": 1, "h": 2,
                           "l": 0.5, "c": 1.5, "tv": 1, "sp": 1, "received_at": 0}])  # fmt: skip


def texts(msgs, word):
    return [m for m in msgs if word in m.text]


# ------------------------------------------------------------------ de-dup state machine
def test_silent_mt5_is_announced_once_and_recovery_once(env):
    settings, conn, engine = env
    first = engine.run(conn, NOW)
    assert (
        len(texts(first, "MT5 not reporting")) == 1
        and not texts(first, "MT5 not reporting")[0].silent
    )
    assert texts(engine.run(conn, NOW + 30), "MT5 not reporting") == []  # no repeat
    assert texts(engine.run(conn, NOW + 60), "MT5 not reporting") == []
    hb(conn, received_at=NOW + 90)
    back = engine.run(conn, NOW + 95)
    assert len(texts(back, "reporting again")) == 1 and back[0].silent
    assert texts(engine.run(conn, NOW + 100), "reporting again") == []
    # and a second outage is announced again
    again = engine.run(conn, NOW + 90 + 4000)
    assert len(texts(again, "MT5 not reporting")) == 1


def test_state_survives_a_restart(env):
    settings, conn, engine = env
    engine.run(conn, NOW)
    fresh_engine = MonitorEngine(settings, started_at=NOW - 10_000)  # as after a service restart
    assert texts(fresh_engine.run(conn, NOW + 30), "MT5 not reporting") == []


def test_heartbeat_timeout_is_longer_outside_sessions(env):
    settings, conn, _ = env
    night = ts(2026, 10, 5, 3)  # no session open
    engine = MonitorEngine(settings, started_at=night - 400)
    assert texts(engine.run(conn, night), "not reporting") == []  # 400 s < 600 s
    engine2 = MonitorEngine(settings, started_at=night - 700)
    assert texts(engine2.run(conn, night), "not reporting") != []


def test_disconnect_needs_two_heartbeats(env):
    settings, conn, engine = env
    hb(conn, NOW - 30, connected=0)
    assert texts(engine.run(conn, NOW), "disconnected") == []
    hb(conn, NOW, connected=0)
    assert len(texts(engine.run(conn, NOW), "disconnected")) == 1
    hb(conn, NOW + 30, connected=1)
    assert len(texts(engine.run(conn, NOW + 30), "connected to the broker again")) == 1


def test_stale_data_only_judged_during_its_session(env):
    settings, conn, engine = env
    hb(conn)
    assert len(texts(engine.run(conn, NOW), "Data stale for GER40")) == 1  # session open, no bars
    bar(conn, NOW - 300)
    assert len(texts(engine.run(conn, NOW + 30), "GER40 is flowing again")) == 1
    # after the session ends the (old) bar is not judged and nothing is announced
    assert texts(engine.run(conn, ts(2026, 10, 5, 12)), "stale") == []


def test_context_symbols_are_not_monitored(env):
    settings, conn, engine = env
    hb(conn)
    assert texts(engine.run(conn, NOW), "UK100") == []


def test_time_check_mismatch(env):
    settings, conn, engine = env
    hb(conn, server_utc_offset_sec=7200)  # EA says +2 h, ny_plus_7 in October gives +3 h
    assert len(texts(engine.run(conn, NOW), "Time check mismatch")) == 1
    hb(conn, NOW + 60)
    assert len(texts(engine.run(conn, NOW + 60), "Time check OK")) == 1


def test_llm_budget_and_failing_alerts(env):
    settings, conn, engine = env
    hb(conn)
    db.log_event(conn, "llm_budget_exceeded")
    assert len(texts(engine.run(conn, int(datetime.now(UTC).timestamp())), "LLM paused")) == 1
    for i in range(3):
        db.insert_llm_call(conn, {"ts_utc": NOW + i, "purpose": "market_read", "prompt_version": "v1",
                                  "model": "m", "prompt": "p", "status": "error", "error": "HTTP 500"})  # fmt: skip
    m = engine.run(conn, NOW + 40)
    assert len(texts(m, "LLM failing (last: HTTP 500)")) == 1
    assert texts(engine.run(conn, NOW + 70), "LLM failing") == []


def test_quiet_hours(env):
    settings, conn, engine = env
    assert in_quiet_hours(settings.config, ts(2026, 10, 5, 21))  # 23:00 Budapest
    assert in_quiet_hours(settings.config, ts(2026, 10, 5, 4))  # 06:00 Budapest
    assert not in_quiet_hours(settings.config, ts(2026, 10, 5, 5))  # 07:00 Budapest
    assert not in_quiet_hours(settings.config, ts(2026, 10, 5, 12))
    night = ts(2026, 10, 5, 21)
    eng = MonitorEngine(settings, started_at=night - 10_000)
    assert all(m.silent for m in texts(eng.run(conn, night), "not reporting"))  # silent at night
    hb(conn, night + 100, positions=1, floating_pl=-5.0)  # position open: sound even at night
    eng2 = MonitorEngine(settings, started_at=night - 10_000)
    db.kv_delete(conn, "mon:mt5_silent")
    m = eng2.run(conn, night + 4000)
    assert [x.silent for x in texts(m, "not reporting")] == [False]


# ------------------------------------------------------------------ positions
def test_position_change_alerts(env):
    settings, conn, engine = env
    hb(conn, NOW)
    engine.run(conn, NOW)  # first sighting only records
    hb(conn, NOW + 60, positions=1, floating_pl=-22.4)
    m = engine.run(conn, NOW + 60)
    assert len(texts(m, "Position change: 1 open, floating −22.40")) == 1 and not m[-1].silent
    assert texts(engine.run(conn, NOW + 90), "Position change") == []  # unchanged
    hb(conn, NOW + 120, positions=1, floating_pl=10.0)  # sign flipped
    assert len(texts(engine.run(conn, NOW + 120), "Position change")) == 1
    hb(conn, NOW + 180, positions=0, floating_pl=0.0)
    assert len(texts(engine.run(conn, NOW + 180), "Position change: 0 open")) == 1


# ------------------------------------------------------------------ FTMO risk
def test_day_start_balance_resets_at_midnight_prague_summer_and_winter(env):
    settings, conn, _ = env
    cfg = settings.config
    # Summer: midnight CEST = 22:00 UTC the evening before
    before = {"received_at": ts(2026, 10, 4, 21, 59), "balance": 1000.0}
    first_after = {"received_at": ts(2026, 10, 4, 22, 1), "balance": 1100.0}
    later = {"received_at": ts(2026, 10, 5, 9), "balance": 1300.0}
    assert day_start_balance(conn, cfg, ts(2026, 10, 4, 21, 59), before) == 1000.0  # day of 4 Oct
    assert day_start_balance(conn, cfg, ts(2026, 10, 4, 22, 1), first_after) == 1100.0  # new day
    assert day_start_balance(conn, cfg, ts(2026, 10, 5, 9), later) == 1100.0  # kept all day
    # Winter: midnight CET = 23:00 UTC; 22:30 UTC is still the previous Prague day
    db.kv_delete(conn, "ftmo_day")
    w_before = {"received_at": ts(2026, 11, 3, 22, 30), "balance": 2000.0}
    w_after = {"received_at": ts(2026, 11, 3, 23, 5), "balance": 2100.0}
    assert day_start_balance(conn, cfg, ts(2026, 11, 3, 22, 30), w_before) == 2000.0
    assert day_start_balance(conn, cfg, ts(2026, 11, 3, 23, 5), w_after) == 2100.0


def test_day_start_needs_a_heartbeat_after_midnight(env):
    settings, conn, _ = env
    stale = {"received_at": ts(2026, 10, 4, 12), "balance": 5.0}
    assert day_start_balance(conn, settings.config, ts(2026, 10, 5, 7), stale) is None
    assert day_start_balance(conn, settings.config, ts(2026, 10, 5, 7), None) is None


def test_risk_warns_once_per_level_and_only_the_highest(env):
    settings, conn, engine = env
    hb(conn, NOW, balance=80000.0, equity=80000.0)
    engine.run(conn, NOW)  # records the day-start balance
    hb(conn, NOW + 60, equity=78000.0)  # 2,000 of 4,000 = 50 %
    m = engine.run(conn, NOW + 60)
    assert len(texts(m, "FTMO daily loss at 50%")) == 1
    assert texts(engine.run(conn, NOW + 90), "FTMO") == []  # not repeated
    hb(conn, NOW + 120, equity=76500.0)  # 3,500 = 87.5 % -> the 80 % level
    assert len(texts(engine.run(conn, NOW + 120), "FTMO daily loss at 88%")) == 1
    assert texts(engine.run(conn, NOW + 150), "FTMO") == []


def test_max_loss_and_jump_straight_past_both_levels(env):
    settings, conn, engine = env
    hb(conn, NOW, equity=71000.0)  # 9,000 of 8,000 max... over 100 %: highest level only
    m = engine.run(conn, NOW)
    assert len(texts(m, "FTMO max loss")) == 1
    assert len(texts(engine.run(conn, NOW + 30), "FTMO max loss")) == 0


def test_news_and_friday_warnings_need_an_open_position(env):
    settings, conn, engine = env
    news_utc = ts(2026, 10, 6, 12, 30)  # example event 08:30 New York
    hb(conn, news_utc - 400, positions=0)
    assert texts(engine.run(conn, news_utc - 400), "high-impact news") == []
    hb(conn, news_utc - 300, positions=1, floating_pl=1.0)
    assert len(texts(engine.run(conn, news_utc - 300), "high-impact news in 5 min")) == 1
    assert texts(engine.run(conn, news_utc - 280), "high-impact news") == []  # once
    friday = ts(2026, 10, 9, 19, 50)  # 21:50 Berlin
    hb(conn, friday, positions=1, floating_pl=1.0)
    assert len(texts(engine.run(conn, friday), "Friday 21:45")) == 1
    assert texts(engine.run(conn, friday + 60), "Friday 21:45") == []


# ------------------------------------------------------------------ scheduled messages
@pytest.fixture
def svc(settings):
    import httpx

    from app.scheduler import TelegramService
    from app.telegram import TelegramApi

    settings.secrets.telegram_chat_id = CHAT
    db.init_db(settings.db_path)
    fake = FakeTelegram()
    s = TelegramService(
        settings, TelegramApi(TOKEN, httpx.MockTransport(fake)), started_at=NOW - 10_000
    )
    return s, fake


def due(svc_, now):
    conn = db.connect(svc_.s.db_path)
    try:
        return svc_.due_messages(conn, now)
    finally:
        conn.close()


def test_brief_is_sent_once_in_the_window_before_the_open(svc):
    s, _ = svc
    assert due(s, ts(2026, 10, 5, 6, 40)) == []  # 08:40 Berlin: before brief_at 08:45
    first = due(s, ts(2026, 10, 5, 6, 46))
    assert len(first) == 1 and first[0].text.startswith("🌅 EU session in 14 min")
    assert due(s, ts(2026, 10, 5, 6, 50)) == []  # once only
    assert due(s, ts(2026, 10, 5, 7, 5)) == []  # session already running: no late brief
    us = due(s, ts(2026, 10, 5, 13, 16))  # 09:16 New York
    assert len(us) == 1 and us[0].text.startswith("🌅 US session in 14 min")


def test_no_brief_on_a_holiday(svc):
    s, _ = svc
    assert due(s, ts(2026, 5, 1, 6, 46)) == []  # 1 May: XETR closed


def test_session_wrap_and_daily_report(svc):
    s, _ = svc
    seed_read(s.s)  # one alert in the EU window
    wrap = due(s, ts(2026, 10, 5, 9, 1))
    assert len(wrap) == 1 and wrap[0].text.startswith("🏁 EU session over · 1 reads · 1 alert")
    assert due(s, ts(2026, 10, 5, 9, 5)) == []
    assert due(s, ts(2026, 10, 5, 10, 30)) == []  # too late for the wrap, too early for the report
    report = due(s, ts(2026, 10, 5, 16, 1))  # 18:01 Berlin
    assert len(report) == 1 and "Daily report" in report[0].text and "alerts 1" in report[0].text
    assert due(s, ts(2026, 10, 5, 16, 30)) == []
    assert due(s, ts(2026, 10, 10, 16, 1)) == []  # Saturday: no report


def test_texts_render_with_data(svc):
    s, _ = svc
    c = db.connect(s.s.db_path)
    hb(c, NOW)
    rid = seed_read(s.s)
    db.add_feedback(c, rid, "take", NOW)
    today = reports.today_text(s.s, c, NOW + 400)
    assert "1 alert" in today and "GER40 LONG H2 A (alert) → take" in today
    status = reports.status_text(s.s, c, NOW + 10, NOW - 10_000)
    assert "balance 80,000.00" in status and "FTMO       daily loss used 0% of 4,000" in status
    brief = reports.brief_text(s.s, c, ts(2026, 10, 5, 6, 46), "eu")
    assert "GER40" in brief and "UK100  context only" in brief and "News: none in window" in brief
    c.close()
