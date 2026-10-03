def test_health_is_open_and_empty(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "last_bar_utc": {}, "last_heartbeat_utc": None}
