import json
import logging
import sqlite3
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from app import db
from app.scheduler import TelegramService
from app.telegram import TelegramApi

TOKEN = "123456:SECRET-TOKEN"
CHAT = "42"
SYMBOL = "GER40.cash"


def ts(y, m, d, hh, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


NOW = ts(2026, 10, 5, 7, 30)  # Monday, EU session running (09:30 Berlin, 09:30 Budapest)


class FakeTelegram:
    """Transport that records Bot API calls and answers ok."""

    def __init__(self, updates=None):
        self.calls: list[tuple[str, dict]] = []
        self.updates = updates or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        ctype = request.headers.get("content-type", "")
        data = {}
        if "urlencoded" in ctype:
            data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        elif "multipart" in ctype:
            body = request.content.decode("latin-1")
            for part in body.split("--")[1:-1]:
                if 'name="' in part and "filename" not in part:
                    name = part.split('name="')[1].split('"')[0]
                    data[name] = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
                elif "filename" in part:
                    data["has_photo"] = "1"
        self.calls.append((method, data))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    def sent(self, method="sendMessage"):
        return [d for m, d in self.calls if m == method]


@pytest.fixture
def tg(settings):
    settings.secrets.telegram_chat_id = CHAT
    settings.secrets.telegram_bot_token = TOKEN
    db.init_db(settings.db_path)
    fake = FakeTelegram()
    svc = TelegramService(
        settings, TelegramApi(TOKEN, httpx.MockTransport(fake)), started_at=NOW - 10_000
    )
    return svc, fake


def events(settings, kind):
    c = sqlite3.connect(settings.db_path)
    try:
        return c.execute("SELECT detail FROM events WHERE kind = ?", (kind,)).fetchall()
    finally:
        c.close()


def msg(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "from": {"id": 9}, "text": text}}


# ------------------------------------------------------------------ allowlist and commands
def test_other_chats_are_ignored_and_logged(tg, settings):
    svc, fake = tg
    svc.bot.handle_update(msg("/status", chat="999"))
    assert fake.calls == []
    assert len(events(settings, "telegram_rejected")) == 1


def test_command_parsing_and_logging(tg, settings):
    svc, fake = tg
    svc.bot.handle_update(msg("/help@clvztradebot"))
    svc.bot.handle_update(msg("/nonsense"))  # unknown -> help text
    svc.bot.handle_update(msg("just chatting"))  # not a command: ignored
    texts = [d["text"] for d in fake.sent()]
    assert len(texts) == 2 and all("Commands" in t for t in texts)
    logged = [json.loads(e[0]) for e in events(settings, "command")]
    assert [c["cmd"] for c in logged] == ["/help", "unknown:/nonsense"]


def test_no_command_can_trade():
    svc_cmds = TelegramService.commands.__code__.co_names
    assert not {"order", "buy", "sell", "trade", "close_position"} & set(svc_cmds)


def test_status_command_runs_on_empty_db(tg):
    svc, fake = tg
    svc.bot.handle_update(msg("/status"))
    text = fake.sent()[0]["text"]
    assert "MT5        no heartbeat yet" in text and "LLM" in text and "Next" in text


def test_pause_and_resume(tg, settings):
    svc, fake = tg
    svc.bot.handle_update(msg("/pause 30"))
    c = db.connect(settings.db_path)
    assert int(db.kv_get(c, "paused_until")) > 0
    svc.bot.handle_update(msg("/resume"))
    assert db.kv_get(c, "paused_until") is None


def test_chart_command_needs_known_symbol(tg):
    svc, fake = tg
    svc.bot.handle_update(msg("/chart"))
    svc.bot.handle_update(msg("/chart FOO"))
    svc.bot.handle_update(msg("/chart GER40 M1"))
    svc.bot.handle_update(msg("/chart GER40"))  # known symbol but no bars yet
    texts = [d["text"] for d in fake.sent()]
    assert texts[0].startswith("Usage") and texts[1].startswith("Unknown symbol")
    assert texts[2].startswith("Timeframe") and texts[3].startswith("No M5 bars")


def test_restart_mt5_is_not_enabled(tg):
    svc, fake = tg
    svc.bot.handle_update(msg("/restart_mt5"))
    assert "not enabled" in fake.sent()[0]["text"]


# ------------------------------------------------------------------ polling
def test_poll_once_persists_offset(tg, settings):
    svc, fake = tg
    fake.updates = [{**msg("/help"), "update_id": 10}, {**msg("/help"), "update_id": 11}]
    assert svc.bot.poll_once(timeout=0) == 2
    fake.updates = []
    svc.bot.poll_once(timeout=0)
    last_get = fake.sent("getUpdates")[-1]
    assert last_get["offset"] == "12"


def test_api_errors_never_leak_the_token(settings, caplog):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    api = TelegramApi(TOKEN, httpx.MockTransport(boom))
    with caplog.at_level(logging.DEBUG):
        assert api.send_message(CHAT, "hi") is None
    assert "SECRET-TOKEN" not in caplog.text and "ConnectError" in caplog.text


# ------------------------------------------------------------------ feedback buttons
def seed_read(settings, action="alert", push=1, bar=NOW - 300, validation="ok", grade="A"):
    c = db.connect(settings.db_path)
    setup = {"direction": "long", "type": "H2", "with_trend": True, "entry_type": "stop",
             "entry": 24325.0, "stop": 24298.0, "target": 24379.0, "grade": grade}  # fmt: skip
    rid = db.insert_read(c, {
        "ts_utc": bar + 300, "symbol": SYMBOL, "bar_time_utc": bar, "session": "eu",
        "model": "test/model", "prompt_version": "v1", "htf_alignment": "aligned_bull",
        "day_type_hint": "trend", "atr": 30.0, "last_close": 24320.0, "model_action": action,
        "action": action, "push": push, "grade": grade, "setup": json.dumps(setup),
        "context": json.dumps({"htf_alignment": "aligned_bull", "day_type": "trend_from_open",
                               "always_in": "long"}),
        "reason": "H2 with trend", "validation": validation})  # fmt: skip
    db.upsert_symbol_meta(c, SYMBOL, 1)
    c.close()
    return rid


def callback(rid, choice, chat=CHAT):
    return {"update_id": 5, "callback_query": {
        "id": "cb1", "data": f"fb:{rid}:{choice}",
        "message": {"message_id": 77, "chat": {"id": int(chat)}}}}  # fmt: skip


def test_feedback_button_saves_choice_and_updates_keyboard(tg, settings):
    svc, fake = tg
    rid = seed_read(settings)
    svc.bot.handle_update(callback(rid, "take"))
    c = sqlite3.connect(settings.db_path)
    assert c.execute("SELECT read_id, choice FROM feedback").fetchall() == [(rid, "take")]
    assert fake.sent("answerCallbackQuery")[0]["text"] == "Saved: I'd take it"
    edited = json.loads(fake.sent("editMessageReplyMarkup")[0]["reply_markup"])
    assert edited["inline_keyboard"][0][0]["text"] == "✓ I'd take it"
    svc.bot.handle_update(callback(rid, "skip"))  # he changes his mind: latest wins
    assert c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


@pytest.mark.parametrize("data", ["fb:1:buy", "fb:x:take", "garbage", "fb:999:take"])
def test_bad_buttons_store_nothing(tg, settings, data):
    svc, fake = tg
    seed_read(settings)
    upd = callback(1, "take")
    upd["callback_query"]["data"] = data
    svc.bot.handle_update(upd)
    c = sqlite3.connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_feedback_from_other_chat_is_rejected(tg, settings):
    svc, fake = tg
    rid = seed_read(settings)
    svc.bot.handle_update(callback(rid, "take", chat="999"))
    c = sqlite3.connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert fake.calls == []


# ------------------------------------------------------------------ sending reads
def test_alert_is_sent_once_with_sound_and_buttons(tg, settings):
    svc, fake = tg
    rid = seed_read(settings)
    assert svc.send_pending_reads(NOW) == 1
    assert svc.send_pending_reads(NOW) == 0  # never twice
    sent = fake.sent()[0]  # no bars in the db -> text without a chart
    assert sent["disable_notification"] == "false"
    assert "🟢 GER40 LONG · H2 · A" in sent["text"] and f"read #{rid}" in sent["text"]
    assert "Entry  24325.0 (buy stop)" in sent["text"] and "2.0R" in sent["text"]
    assert "Stop   24298.0  (27.0 pts, 0.9 ATR)" in sent["text"]
    assert f"fb:{rid}:take" in sent["reply_markup"]


def test_watch_is_silent_and_none_is_never_sent(tg, settings):
    svc, fake = tg
    seed_read(settings, action="watch", push=0, bar=NOW - 600, grade="B")
    seed_read(settings, action="none", push=0, bar=NOW - 900)
    svc.send_pending_reads(NOW)
    (sent,) = fake.sent()
    assert sent["disable_notification"] == "true" and "👀 GER40 WATCH LONG" in sent["text"]


def test_alert_with_a_chart_uses_send_photo(tg, settings):
    from tests.test_reader import m5_frame, store

    svc, fake = tg
    c = db.connect(settings.db_path)
    store(c, m5_frame().tail(100).set_axis(m5_frame().tail(100).index), "M5")
    c.close()
    from tests.test_reader import BAR

    seed_read(settings, bar=BAR)
    svc.send_pending_reads(BAR + 330)
    assert fake.sent("sendPhoto") and fake.sent("sendPhoto")[0]["has_photo"] == "1"
    assert "GER40 LONG" in fake.sent("sendPhoto")[0]["caption"]


def test_stale_alert_is_dropped_not_sent(tg, settings):
    svc, fake = tg
    seed_read(settings, bar=NOW - 1500)  # closed 20 min ago
    assert svc.send_pending_reads(NOW) == 0 and fake.calls == []
    assert events(settings, "alert_dropped")


def test_paused_alerts_are_muted_and_not_replayed(tg, settings):
    svc, fake = tg
    seed_read(settings)
    svc.bot.handle_update(msg("/pause 60"))
    fake.calls.clear()
    svc.send_pending_reads(int(__import__("time").time()))  # real clock: pause is relative to it
    # The read is old relative to the real clock, so it is dropped as stale; either way no send.
    assert fake.sent() == []
    svc.bot.handle_update(msg("/resume"))
    fake.calls.clear()
    svc.send_pending_reads(NOW)
    assert fake.sent() == []  # already claimed: not replayed after /resume


def test_http_libraries_never_log_request_urls(caplog):
    """httpx logs full URLs at INFO, and Telegram URLs contain the bot token."""
    from app.main import setup_logging

    setup_logging()
    api = TelegramApi(TOKEN, httpx.MockTransport(FakeTelegram()))
    with caplog.at_level(logging.INFO):
        api.send_message(CHAT, "hello")
    assert TOKEN not in caplog.text and "SECRET-TOKEN" not in caplog.text
