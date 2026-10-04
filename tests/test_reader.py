import json
import sqlite3
from datetime import UTC, datetime

import httpx
import numpy as np
import pandas as pd
import pytest

from app import db
from app import features as F
from app.llm import LlmClient
from app.reader import load_prompt, render, run_read
from tests.test_htf import zigzag

SYMBOL = "GER40.cash"
BAR = int(datetime(2026, 10, 5, 7, 55, tzinfo=UTC).timestamp())  # EU session, bar 12
BAR_ISO = "2026-10-05T07:55:00Z"
NOW = BAR + 300 + 20  # 20 s after the bar closed


def m5_frame(n=150, future=0):
    """Rising zigzag M5 bars ending at BAR (+ `future` junk bars after it)."""
    idx = pd.date_range(end=pd.Timestamp(BAR, unit="s", tz="UTC"), periods=n, freq="5min")
    c = pd.Series(24000 + 0.8 * np.arange(n), index=idx)
    c = c + 4 * pd.Series([(-1) ** i for i in range(n)], index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    df = pd.DataFrame({"o": o, "h": pd.concat([o, c], axis=1).max(axis=1) + 1.5,
                       "l": pd.concat([o, c], axis=1).min(axis=1) - 1.5, "c": c})  # fmt: skip
    if future:
        fidx = pd.date_range(df.index[-1] + pd.Timedelta("5min"), periods=future, freq="5min")
        junk = pd.DataFrame({"o": 9999.0, "h": 9999.5, "l": 9998.5, "c": 9999.0}, index=fidx)
        df = pd.concat([df, junk])
    return df


def store(conn, df, tf, symbol=SYMBOL):
    rows = [
        {"symbol": symbol, "tf": tf, "t_server": 0, "t_utc": int(ts.timestamp()),
         "o": r.o, "h": r.h, "l": r.l, "c": r.c, "tv": 1, "sp": 1, "received_at": 0}
        for ts, r in df.iterrows()
    ]  # fmt: skip
    db.upsert_bars(conn, rows)


def seed(settings, d1_drift=5.0, future=0):
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    store(conn, m5_frame(future=future), "M5")
    store(conn, zigzag(300, 1.0, freq="1h").set_axis(
        pd.date_range(end=pd.Timestamp(BAR, unit="s", tz="UTC").floor("1h"), periods=300, freq="1h")), "H1")  # fmt: skip
    d1 = zigzag(80, d1_drift, freq="1D")
    d1.index = pd.date_range(end=pd.Timestamp("2026-10-04", tz="UTC"), periods=80, freq="1D")
    store(conn, d1, "D1")
    db.upsert_symbol_meta(conn, SYMBOL, 1)
    conn.close()


class FakeOpenRouter:
    def __init__(self, content=None, status=200):
        self.content, self.status, self.requests = content, status, []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "x"})
        body = {"provider": "P", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
                "choices": [{"message": {"content": self.content}}]}  # fmt: skip
        return httpx.Response(200, json=body)


def good_answer(settings, **setup_over):
    m5 = m5_frame()
    close = float(m5["c"].iloc[-1])
    atr = float(F.atr(m5).iloc[-1])
    setup = {"direction": "long", "type": "H2", "with_trend": True, "entry_type": "stop",
             "entry": round(close + 1, 1), "stop": round(close - atr, 1),
             "target": round(close + 2.5 * atr, 1), "grade": "A", **setup_over}  # fmt: skip
    return json.dumps({
        "schema": 1, "symbol": SYMBOL, "bar_time_utc": BAR_ISO,
        "context": {"htf_alignment": "aligned_bull", "day_type": "trend_from_open", "always_in": "long"},
        "action": "alert", "setup": setup, "reason": "H2 buy in a bull trend"})  # fmt: skip


def make(settings, fake, db_seeded=True, **seed_kw):
    settings.secrets.openrouter_api_key = "sk-or-test"
    settings.config.llm.model = "test/model"
    if db_seeded:
        seed(settings, **seed_kw)
    return LlmClient(settings, httpx.MockTransport(fake), now=lambda: NOW), fake


def q(settings, sql):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ prompt
def test_render_fills_everything_and_rejects_holes():
    assert render("a {x} b {y}", {"x": 1, "y": "{x}"}) == "a 1 b {x}"  # inserted text not rescanned
    with pytest.raises(KeyError):
        render("a {x} {missing}", {"x": 1})
    assert render('{"json": 1}', {}) == '{"json": 1}'  # JSON braces are left alone


def test_prompt_v1_loads():
    system, user = load_prompt("v1")
    assert "Al Brooks" in system and "{symbol}" in user and "{schema_json}" in user


def test_prompt_v2_keeps_v1_and_adds_the_limits():
    s1, u1 = load_prompt("v1")
    s2, u2 = load_prompt("v2")
    assert u1 == u2 and "250 characters" in s2 and "ALWAYS come with a complete setup" in s2


# ------------------------------------------------------------------ happy path
def test_full_read_stores_alert_and_logs_call(settings):
    fake = FakeOpenRouter()
    llm, fake = make(settings, fake)
    fake.content = good_answer(settings)
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.status == "stored" and r.action == "alert", r
    (read,) = q(settings, "SELECT * FROM reads")
    assert read["push"] == 1 and read["grade"] == "A" and read["validation"] == "ok"
    assert read["htf_alignment"] == "aligned_bull" and read["model_action"] == "alert"
    assert json.loads(read["setup"])["direction"] == "long"
    (call,) = q(settings, "SELECT * FROM llm_calls")
    assert call["validation"] == "ok" and json.loads(call["parsed"])["action"] == "alert"
    assert read["llm_call_id"] == call["id"]


def test_prompt_contents_and_no_future_bars(settings):
    fake = FakeOpenRouter()
    llm, fake = make(settings, fake, future=5)  # 5 bars AFTER the evaluated one are in the db
    fake.content = good_answer(settings)
    run_read(settings, llm, SYMBOL, BAR, now=NOW)
    user = fake.requests[0]["messages"][1]["content"]
    assert "9999" not in user  # no future bar leaks into the prompt
    assert BAR_ISO in user and "H1 bull, D1 bull -> aligned_bull".replace("->", "→") in user
    assert not __import__("re").search(r"\{[a-z_0-9]+\}", user)  # no unfilled placeholder
    table = user.split("Columns:")[1].split("\n", 1)[1].split("Return JSON")[0].strip().splitlines()
    assert len(table) == 36 and table[-1].startswith("09:55")  # local time, newest last
    assert fake.requests[0]["temperature"] == 0


def test_read_is_idempotent_per_bar(settings):
    llm, fake = make(settings, FakeOpenRouter())
    fake.content = good_answer(settings)
    run_read(settings, llm, SYMBOL, BAR, now=NOW)
    again = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert again.reason == "already_read" and len(fake.requests) == 1


# ------------------------------------------------------------------ skips (no LLM call)
def skipped(settings, llm, fake, reason, symbol=SYMBOL, bar=BAR, now=NOW):
    r = run_read(settings, llm, symbol, bar, now=now)
    assert r.status == "skipped" and r.reason.startswith(reason), r
    assert fake.requests == []
    assert q(settings, "SELECT * FROM reads") == []


def test_skip_context_only_symbol(settings):
    llm, fake = make(settings, FakeOpenRouter())
    skipped(settings, llm, fake, "not_traded", symbol="UK100.cash")


def test_skip_outside_session(settings):
    llm, fake = make(settings, FakeOpenRouter())
    bar = BAR - 3600 * 2  # 05:55 UTC, before the EU open
    skipped(settings, llm, fake, "outside_session", bar=bar, now=bar + 320)


def test_skip_stale_data(settings):
    llm, fake = make(settings, FakeOpenRouter())
    skipped(settings, llm, fake, "stale_data", now=BAR + 300 + 601)
    assert q(settings, "SELECT kind FROM events WHERE kind='read_skipped'")  # logged


def test_just_fresh_enough_is_not_stale(settings):
    llm, fake = make(settings, FakeOpenRouter())
    fake.content = good_answer(settings)
    assert run_read(settings, llm, SYMBOL, BAR, now=BAR + 300 + 600).status == "stored"


def test_skip_htf_conflict(settings):
    llm, fake = make(settings, FakeOpenRouter(), d1_drift=-5.0)  # D1 bear vs H1 bull
    skipped(settings, llm, fake, "htf_conflict")


def test_skip_budget_exceeded(settings):
    llm, fake = make(settings, FakeOpenRouter())
    settings.config.llm.daily_budget_usd = 0.01
    conn = db.connect(settings.db_path)
    db.insert_llm_call(conn, {"ts_utc": int(NOW), "purpose": "x", "prompt_version": "v1",
                              "model": "m", "prompt": "p", "status": "ok", "cost_usd": 1.0})  # fmt: skip
    conn.close()
    skipped(settings, llm, fake, "budget_exceeded")


def test_skip_without_history_or_digits(settings):
    llm, fake = make(settings, FakeOpenRouter(), db_seeded=False)
    db.init_db(settings.db_path)
    skipped(settings, llm, fake, "unknown_digits")
    conn = db.connect(settings.db_path)
    db.upsert_symbol_meta(conn, SYMBOL, 1)
    conn.close()
    skipped(settings, llm, fake, "bar_not_stored")


# ------------------------------------------------------------------ fail closed
@pytest.mark.parametrize("content", ["I think you should buy", "[]", '{"schema": 1}', ""])
def test_garbage_answers_never_alert(settings, content):
    llm, fake = make(settings, FakeOpenRouter())
    fake.content = content or " "
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.action == "none"
    (read,) = q(settings, "SELECT * FROM reads")
    assert read["push"] == 0 and read["action"] == "none"
    assert read["validation"].startswith(("rejected", "llm error"))


def test_llm_http_error_is_stored_as_none(settings):
    llm, fake = make(settings, FakeOpenRouter(status=500))
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.action == "none"
    (read,) = q(settings, "SELECT * FROM reads")
    assert read["validation"] == "llm error: HTTP 500" and read["push"] == 0


def test_model_alert_with_bad_prices_is_rejected(settings):
    llm, fake = make(settings, FakeOpenRouter())
    fake.content = good_answer(settings, stop=99999.0)  # absurd stop above entry
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.action == "none"
    assert q(settings, "SELECT validation FROM llm_calls")[0][0].startswith("rejected")


def test_unexpected_bug_fails_closed(settings, monkeypatch):
    llm, fake = make(settings, FakeOpenRouter())
    monkeypatch.setattr("app.reader.features.compute_features", lambda *a, **k: 1 / 0)
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.status == "skipped" and r.reason == "crashed" and fake.requests == []
    assert q(settings, "SELECT * FROM events WHERE kind='read_crashed'")


# ------------------------------------------------------------------ engine mode (prompt v3)
from app.strategies import Candidate  # noqa: E402


class OneLong:
    name, version = "testrule", "1"

    def __init__(self, propose=True):
        self.propose = propose

    def candidates(self, ev):
        if not self.propose:
            return []
        entry = round(ev.last_close + 1, 1)
        return [Candidate("H2", "long", entry, round(entry - ev.atr, 1), round(entry + 2 * ev.atr, 1),
                          True, evidence={"h_count": 2})]  # fmt: skip


def engine(settings, monkeypatch, strategy, content):
    settings.config.engine.strategy = "testrule"
    monkeypatch.setattr("app.reader.get_strategy", lambda name: strategy)
    llm, fake = make(settings, FakeOpenRouter())
    fake.content = content
    return llm, fake


def recommendation(**over):
    body = {"schema": 2, "symbol": SYMBOL, "bar_time_utc": BAR_ISO,
            "context": {"htf_alignment": "aligned_bull", "day_type": "trend_from_open", "always_in": "long"},
            "decision": "take", "candidate_id": 1, "grade": "A", "reason": "H2 at the EMA in a bull trend"}  # fmt: skip
    body.update(over)
    return json.dumps(body)


def test_engine_take_stores_alert_with_python_prices(settings, monkeypatch):
    llm, fake = engine(settings, monkeypatch, OneLong(), recommendation())
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert r.action == "alert", r
    user = fake.requests[0]["messages"][1]["content"]
    assert "Candidates from the Python strategy testrule:1" in user and "1) H2 long" in user
    assert "60-minute EMA20" in user and "evidence: h_count=2" in user
    (read,) = q(settings, "SELECT * FROM reads")
    setup = json.loads(read["setup"])
    assert setup["strategy"] == "testrule:1" and setup["candidate_id"] == 1
    assert setup["evidence"] == {"h_count": 2} and read["prompt_version"] == "v3"
    assert read["model_action"] == "take" and read["push"] == 1


def test_engine_without_candidates_never_calls_the_llm(settings, monkeypatch):
    llm, fake = engine(settings, monkeypatch, OneLong(propose=False), recommendation())
    r = run_read(settings, llm, SYMBOL, BAR, now=NOW)
    assert (r.status, r.reason) == ("skipped", "no_candidate") and fake.requests == []


def test_engine_skip_and_bad_answers_never_alert(settings, monkeypatch):
    for content in (recommendation(decision="skip", candidate_id=None, grade=None),
                    recommendation(candidate_id=7), "nonsense"):  # fmt: skip
        settings2 = settings
        llm, fake = engine(settings2, monkeypatch, OneLong(), content)
        conn = db.connect(settings2.db_path)
        conn.execute("DELETE FROM reads")
        conn.commit()
        conn.close()
        assert run_read(settings2, llm, SYMBOL, BAR, now=NOW).action == "none"


def test_engine_mode_is_off_by_default(settings):
    assert settings.config.engine.strategy == ""
