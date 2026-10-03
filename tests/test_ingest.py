import sqlite3

from tests.conftest import AUTH, bars_payload, heartbeat_payload


def count(settings, table):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_auth_required(client, settings):
    for path, body in (("/v1/bars", bars_payload()), ("/v1/heartbeat", heartbeat_payload())):
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(path, json=body, headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
        assert (
            client.post(path, json=body, headers={"Authorization": "test-token"}).status_code == 401
        )
    assert count(settings, "bars") == 0
    assert count(settings, "heartbeats") == 0


def test_empty_server_token_rejects_everything(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.secrets.ingest_token = ""
    with TestClient(create_app(settings)) as c:
        assert (
            c.post(
                "/v1/bars", json=bars_payload(), headers={"Authorization": "Bearer "}
            ).status_code
            == 401
        )


def test_bars_stored_with_utc_time(client, settings):
    r = client.post("/v1/bars", json=bars_payload(), headers=AUTH)
    assert r.status_code == 200 and r.json() == {"accepted": 1}
    conn = sqlite3.connect(settings.db_path)
    t_server, t_utc = conn.execute("SELECT t_server, t_utc FROM bars").fetchone()
    assert t_server == 1759480200
    # ny_plus_7 in October (EDT, UTC-4): server clock = UTC + 3 h
    assert t_utc == 1759480200 - 3 * 3600


def test_duplicate_bars_upsert_cleanly(client, settings):
    client.post("/v1/bars", json=bars_payload(), headers=AUTH)
    changed = bars_payload()
    changed["bars"][0]["c"] = 24320.0
    r = client.post("/v1/bars", json=changed, headers=AUTH)
    assert r.status_code == 200
    assert count(settings, "bars") == 1
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT c FROM bars").fetchone()[0] == 24320.0


def test_backfill_batch_in_one_request(client, settings):
    bars = [
        {"t": 1759480200 + 300 * i, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "tv": 1, "sp": 1}
        for i in range(500)
    ]
    r = client.post("/v1/bars", json=bars_payload(bars=bars), headers=AUTH)
    assert r.json() == {"accepted": 500}
    assert count(settings, "bars") == 500


def test_malformed_payloads_422_and_nothing_stored(client, settings):
    bad_bar_ohlc = bars_payload()
    bad_bar_ohlc["bars"][0]["h"] = 1.0  # high below the close
    cases = [
        bars_payload(schema=2),
        bars_payload(timeframe="M1"),
        bars_payload(bars=[]),
        bars_payload(bars=[{"t": 1}]),
        bars_payload(symbol="EURUSD"),  # not in config
        bars_payload(extra_field=1),
        bad_bar_ohlc,
    ]
    for body in cases:
        assert client.post("/v1/bars", json=body, headers=AUTH).status_code == 422, body
    assert client.post("/v1/bars", content=b"not json", headers=AUTH).status_code == 422
    assert count(settings, "bars") == 0


def test_oversized_batch_rejected(client, settings):
    bars = [{"t": 1 + i, "o": 1, "h": 1, "l": 1, "c": 1, "tv": 0, "sp": 0} for i in range(501)]
    assert client.post("/v1/bars", json=bars_payload(bars=bars), headers=AUTH).status_code == 422
    assert count(settings, "bars") == 0


def test_heartbeat_stored_and_reflected_in_health(client, settings):
    assert client.post("/v1/heartbeat", json=heartbeat_payload(), headers=AUTH).status_code == 200
    assert count(settings, "heartbeats") == 1
    client.post("/v1/bars", json=bars_payload(), headers=AUTH)
    h = client.get("/health").json()
    assert h["last_heartbeat_utc"] is not None
    assert h["last_bar_utc"]["GER40.cash"]["M5"] is not None


def test_malformed_heartbeat_422(client, settings):
    body = heartbeat_payload()
    del body["balance"]
    assert client.post("/v1/heartbeat", json=body, headers=AUTH).status_code == 422
    assert count(settings, "heartbeats") == 0
