import json
import os
import sqlite3

import pytest

from app import db
from app.spool import SpoolWatcher
from tests.conftest import bars_payload, heartbeat_payload


@pytest.fixture
def spool(settings, tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    db.init_db(settings.db_path)
    calls = []
    return SpoolWatcher(settings, d, lambda sym, t: calls.append((sym, t))), d, calls


def put(d, name, body):
    p = d / name
    p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return p


def count(settings, table):
    c = sqlite3.connect(settings.db_path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_bars_and_heartbeat_files_are_stored_and_removed(spool, settings):
    w, d, calls = spool
    put(d, "bars_1791000000_000001.json", bars_payload())
    hb = put(d, "hb_1791000000_000002.json", heartbeat_payload())
    os.utime(hb, (1791000123, 1791000123))
    assert w.process_once() == 2
    assert list(d.glob("*.json")) == []
    assert count(settings, "bars") == 1 and count(settings, "heartbeats") == 1
    c = sqlite3.connect(settings.db_path)
    assert c.execute("SELECT received_at FROM heartbeats").fetchone()[0] == 1791000123  # file time
    assert c.execute("SELECT digits FROM symbol_meta").fetchone()[0] == 2


def test_same_file_twice_is_idempotent(spool, settings):
    w, d, _ = spool
    put(d, "bars_1_000001.json", bars_payload())
    w.process_once()
    put(d, "bars_2_000001.json", bars_payload())
    w.process_once()
    assert count(settings, "bars") == 1


def test_incomplete_tmp_files_are_left_alone(spool, settings):
    w, d, _ = spool
    put(d, "bars_1_000001.tmp", '{"half": ')
    assert w.process_once() == 0
    assert (d / "bars_1_000001.tmp").exists()


@pytest.mark.parametrize(
    "name,body",
    [
        ("bars_1_x.json", "not json"),
        ("bars_1_x.json", {"schema": 1}),
        ("bars_1_x.json", bars_payload(symbol="EURUSD")),
        (
            "bars_1_x.json",
            bars_payload(bars=[{"t": 1, "o": 2, "h": 1, "l": 1, "c": 1, "tv": 0, "sp": 0}]),
        ),
        ("hb_1_x.json", {**heartbeat_payload(), "extra": 1}),
        ("orders_1_x.json", {"buy": "GER40"}),
    ],
)
def test_bad_files_are_rejected_logged_and_store_nothing(spool, settings, name, body):
    w, d, calls = spool
    put(d, name, body)
    assert w.process_once() == 0
    assert not (d / name).exists() and (d / "rejected" / name).exists()
    assert count(settings, "bars") == 0 and count(settings, "heartbeats") == 0
    c = sqlite3.connect(settings.db_path)
    (detail,) = c.execute("SELECT detail FROM events WHERE kind = 'spool_rejected'").fetchone()
    assert json.loads(detail)["file"] == name
    assert calls == []


def test_one_bad_file_does_not_block_the_others(spool, settings):
    w, d, _ = spool
    put(d, "bars_1_000001.json", "garbage")
    put(d, "hb_1_000002.json", heartbeat_payload())
    assert w.process_once() == 1
    assert count(settings, "heartbeats") == 1


def test_market_read_is_triggered_only_for_traded_m5(spool, settings):
    w, d, calls = spool
    put(d, "bars_1_000001.json", bars_payload(timeframe="H1"))
    put(d, "bars_1_000002.json", bars_payload(symbol="UK100.cash"))
    assert w.process_once() == 2 and calls == []
    bars = [
        {"t": 1759480200 + 300 * i, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "tv": 1, "sp": 1}
        for i in range(3)
    ]
    put(d, "bars_1_000003.json", bars_payload(bars=bars))
    w.process_once()
    assert calls == [("GER40.cash", 1759480200 + 600 - 3 * 3600)]  # newest bar, in UTC


def test_files_are_processed_in_name_order(spool, settings):
    w, d, _ = spool
    for i, eq in enumerate((80000.0, 79000.0, 78000.0)):
        put(d, f"hb_17910000{i:02d}_00000{i}.json", heartbeat_payload(equity=eq))
    w.process_once()
    c = sqlite3.connect(settings.db_path)
    assert [r[0] for r in c.execute("SELECT equity FROM heartbeats ORDER BY id")] == [
        80000.0, 79000.0, 78000.0,
    ]  # fmt: skip


def test_missing_spool_dir_is_harmless(settings, tmp_path):
    w = SpoolWatcher(settings, tmp_path / "does-not-exist")
    assert w.process_once() == 0
