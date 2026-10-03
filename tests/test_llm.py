import json
import sqlite3

import httpx
import pytest

from app import db
from app.llm import LlmClient, parse_json_object, utc_day_start

NOW = 1_790_000_000.0  # fixed clock
OK_BODY = {
    "provider": "SomeProvider",
    "choices": [{"message": {"content": '{"action": "none"}'}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.0123},
}


class Recorder:
    """Mock transport: replays queued responses/exceptions and remembers the requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def make(settings, *outcomes, key="sk-or-secret-key"):
    settings.secrets.openrouter_api_key = key
    db.init_db(settings.db_path)
    rec = Recorder(*outcomes)
    return LlmClient(settings, httpx.MockTransport(rec), now=lambda: NOW), rec


def rows(settings, sql="SELECT * FROM llm_calls"):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def json_response(body, status=200):
    return httpx.Response(status, json=body)


def test_success_is_logged_with_everything(settings):
    client, rec = make(settings, json_response(OK_BODY))
    r = client.complete("SYS", "USER")
    assert r.ok and r.text == '{"action": "none"}' and r.cost_usd == 0.0123
    (row,) = rows(settings)
    assert row["status"] == "ok" and row["provider"] == "SomeProvider"
    assert row["model"] == settings.config.llm.model
    assert row["prompt_version"] == settings.config.llm.prompt_version
    assert "SYS" in row["prompt"] and "USER" in row["prompt"]
    assert json.loads(row["raw_response"])["usage"]["cost"] == 0.0123
    assert (row["tokens_in"], row["tokens_out"], row["cost_usd"]) == (1200, 80, 0.0123)
    assert row["latency_ms"] >= 0 and row["error"] is None


def test_request_follows_config(settings):
    settings.config.llm.provider_order = ["ProviderA"]
    client, rec = make(settings, json_response(OK_BODY))
    client.complete("SYS", "USER")
    req = rec.requests[0]
    body = json.loads(req.content)
    assert body["model"] == settings.config.llm.model
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"]["order"] == ["ProviderA"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert req.headers["authorization"] == "Bearer sk-or-secret-key"


def test_api_key_is_never_stored(settings):
    client, _ = make(settings, json_response(OK_BODY))
    client.complete("SYS", "USER")
    conn = sqlite3.connect(settings.db_path)
    dump = "\n".join(conn.iterdump())
    assert "sk-or-secret-key" not in dump


def test_http_error_is_logged_and_not_retried(settings):
    client, rec = make(settings, json_response({"error": "boom"}, 500))
    r = client.complete("S", "U")
    assert not r.ok and r.error == "HTTP 500" and len(rec.requests) == 1
    (row,) = rows(settings)
    assert row["status"] == "error" and "boom" in row["raw_response"]


def test_one_retry_on_network_error(settings):
    client, rec = make(settings, httpx.ConnectError("down"), json_response(OK_BODY))
    assert client.complete("S", "U").ok
    assert len(rec.requests) == 2 and len(rows(settings)) == 1


def test_two_network_errors_give_up(settings):
    client, rec = make(settings, httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    r = client.complete("S", "U")
    assert not r.ok and r.error == "network error: ReadTimeout" and len(rec.requests) == 2
    assert rows(settings)[0]["status"] == "error"


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "  "}}]},
    ],
)
def test_empty_or_malformed_answers_are_errors(settings, body):
    client, _ = make(settings, json_response(body))
    r = client.complete("S", "U")
    assert not r.ok and r.text is None
    assert rows(settings)[0]["status"] == "error"


def test_non_json_body_is_an_error(settings):
    client, _ = make(settings, httpx.Response(200, text="<html>gateway</html>"))
    r = client.complete("S", "U")
    assert not r.ok and "unreadable" in r.error


def test_budget_guard_stops_calls_and_alerts_once(settings):
    settings.config.llm.daily_budget_usd = 0.02
    expensive = {**OK_BODY, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.03}}
    client, rec = make(settings, json_response(expensive))
    assert client.complete("S", "U").ok  # spends 0.03 > 0.02
    for _ in range(3):
        r = client.complete("S", "U")
        assert not r.ok and "budget" in r.error and r.call_id is None
    assert len(rec.requests) == 1  # no further network calls
    events = rows(settings, "SELECT * FROM events WHERE kind = 'llm_budget_exceeded'")
    assert len(events) == 1  # announced once
    assert client.spend_today() == pytest.approx(0.03)


def test_budget_is_per_utc_day(settings):
    client, _ = make(settings, json_response(OK_BODY))
    conn = db.connect(settings.db_path)
    db.insert_llm_call(
        conn,
        {"ts_utc": utc_day_start(NOW) - 60, "purpose": "x", "prompt_version": "v1", "model": "m",
         "prompt": "p", "status": "ok", "cost_usd": 99.0},
    )  # fmt: skip
    conn.close()
    assert client.spend_today() == 0.0  # yesterday's spend does not count
    assert client.complete("S", "U").ok


def test_empty_api_key_makes_no_call(settings):
    client, rec = make(settings, key="")
    r = client.complete("S", "U")
    assert not r.ok and not rec.requests


def test_reader_can_attach_parsed_and_validation(settings):
    client, _ = make(settings, json_response(OK_BODY))
    r = client.complete("S", "U")
    conn = db.connect(settings.db_path)
    db.update_llm_call(conn, r.call_id, '{"action": "none"}', "ok")
    conn.close()
    row = rows(settings)[0]
    assert row["parsed"] == '{"action": "none"}' and row["validation"] == "ok"


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure! {"a": 1}', None),
        ("[1, 2]", None),
        ("not json", None),
        ("", None),
    ],
)
def test_parse_json_object(text, expected):
    assert parse_json_object(text) == expected
