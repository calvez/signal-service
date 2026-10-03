"""Telegram transport and bot: long polling, chat-id allowlist, command router, feedback buttons.

Rules (docs/telegram.md, CLAUDE.md): outbound long polling only (no webhook); only
TELEGRAM_CHAT_ID may talk to the bot, everybody else is ignored and logged; no command can
trade. The bot token is part of every API URL, so exceptions are logged by TYPE only, never
with their text.
"""

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable

import httpx

from app import db
from app.config import Settings
from app.messages import CHOICES, feedback_buttons

log = logging.getLogger("signal.telegram")

API = "https://api.telegram.org"
CAPTION_MAX = 1024
TEXT_MAX = 4096


class TelegramApi:
    """Thin httpx wrapper. Every method returns the API's `result`, or None on any failure."""

    def __init__(
        self, token: str, transport: httpx.BaseTransport | None = None, timeout: float = 35.0
    ):
        self._base = f"{API}/bot{token}"
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def call(self, method: str, data: dict | None = None, files: dict | None = None):
        try:
            resp = self._http.post(f"{self._base}/{method}", data=data, files=files)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram %s failed: %s", method, type(exc).__name__)  # never str(exc)
            return None
        if not body.get("ok"):
            log.warning("telegram %s refused: %s", method, body.get("description"))
            return None
        return body["result"]

    def send_message(self, chat_id, text, silent=False, markup=None) -> int | None:
        data = {"chat_id": chat_id, "text": text[:TEXT_MAX], "disable_notification": silent}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        res = self.call("sendMessage", data)
        return None if res is None else res["message_id"]

    def send_photo(self, chat_id, png: bytes, caption="", silent=False, markup=None) -> int | None:
        data = {
            "chat_id": chat_id,
            "caption": caption[:CAPTION_MAX],
            "disable_notification": silent,
        }
        if markup:
            data["reply_markup"] = json.dumps(markup)
        res = self.call("sendPhoto", data, files={"photo": ("chart.png", png, "image/png")})
        return None if res is None else res["message_id"]

    def send_document(self, chat_id, filename: str, content: bytes, caption="") -> int | None:
        data = {"chat_id": chat_id, "caption": caption[:CAPTION_MAX], "disable_notification": True}
        res = self.call("sendDocument", data, files={"document": (filename, content, "text/csv")})
        return None if res is None else res["message_id"]

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict] | None:
        data = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_markup(self, chat_id, message_id: int, markup: dict) -> None:
        self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": json.dumps(markup)},
        )


class Bot:
    """Routes updates to command handlers. `commands` maps '/name' -> handler(args) -> Reply."""

    def __init__(
        self,
        settings: Settings,
        api: TelegramApi,
        commands: dict[str, Callable],
        actions: dict[str, Callable] | None = None,
    ):
        self.s = settings
        self.api = api
        self.chat_id = settings.secrets.telegram_chat_id
        self.commands = commands
        self.actions = actions or {}  # inline-button prefix -> handler(parts) -> answer text
        self._stop = threading.Event()

    # ------------------------------------------------------------------ allowlist
    def _allowed(self, chat_id) -> bool:
        return bool(self.chat_id) and str(chat_id) == str(self.chat_id)

    def _log(self, kind: str, detail: dict) -> None:
        conn = db.connect(self.s.db_path)
        try:
            db.log_event(conn, kind, detail)
        finally:
            conn.close()

    # ------------------------------------------------------------------ updates
    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception:  # a bad update must not kill the poller
            log.exception("update handling crashed")
            self._log("telegram_error", {"update_id": update.get("update_id")})

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg.get("chat", {}).get("id")
        if not self._allowed(chat_id):
            # Ignored silently, but logged. Only the type of chat, never the text.
            self._log(
                "telegram_rejected", {"chat_id": chat_id, "from": msg.get("from", {}).get("id")}
            )
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        head, _, rest = text.partition(" ")
        cmd = head.split("@")[0].lower()
        args = rest.split()
        handler = self.commands.get(cmd, self.commands["/help"])
        known = cmd in self.commands
        try:
            reply = handler(args)
            ok = True
        except Exception:
            log.exception("command %s crashed", cmd)
            reply, ok = None, False
            self.api.send_message(chat_id, f"⚠️ {cmd} failed, see the service log.")
        self._log("command", {"cmd": cmd if known else f"unknown:{cmd}", "args": args, "ok": ok})
        if reply is not None:
            reply.send(self.api, chat_id)

    def _handle_callback(self, cb: dict) -> None:
        msg = cb.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if not self._allowed(chat_id):
            self._log("telegram_rejected", {"chat_id": chat_id, "callback": True})
            return
        data = cb.get("data", "")
        if data == "noop":
            self.api.answer_callback(cb["id"])
            return
        parts = data.split(":")
        if parts[0] in self.actions:
            answer = self.actions[parts[0]](parts[1:])
            self.api.answer_callback(cb["id"], answer)
            if msg.get("message_id"):  # the button is single-use
                self.api.edit_markup(chat_id, msg["message_id"], {"inline_keyboard": []})
            return
        if len(parts) != 3 or parts[0] != "fb" or parts[2] not in CHOICES or not parts[1].isdigit():
            self.api.answer_callback(cb["id"], "Unknown button")
            return
        read_id, choice = int(parts[1]), parts[2]
        conn = db.connect(self.s.db_path)
        try:
            if conn.execute("SELECT 1 FROM reads WHERE id = ?", (read_id,)).fetchone() is None:
                self.api.answer_callback(cb["id"], "Read not found")
                return
            db.add_feedback(conn, read_id, choice, int(time.time()))
        finally:
            conn.close()
        self.api.answer_callback(cb["id"], f"Saved: {CHOICES[choice]}")
        if msg.get("message_id"):
            self.api.edit_markup(chat_id, msg["message_id"], feedback_buttons(read_id, choice))

    # ------------------------------------------------------------------ polling loop
    def poll_once(self, timeout: int = 25) -> int:
        """One getUpdates round. Returns the number of updates handled."""
        conn = db.connect(self.s.db_path)
        try:
            off = db.kv_get(conn, "telegram_offset")
        finally:
            conn.close()
        updates = self.api.get_updates(int(off) if off else None, timeout)
        if updates is None:
            return -1
        for u in updates:
            self.handle_update(u)
            conn = db.connect(self.s.db_path)
            try:
                db.kv_set(conn, "telegram_offset", str(u["update_id"] + 1))
            finally:
                conn.close()
        return len(updates)

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self.poll_once() < 0:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            else:
                backoff = 1.0

    def stop(self) -> None:
        self._stop.set()


class Reply:
    """What a command handler wants sent: text, or a photo with a caption."""

    def __init__(self, text: str = "", png: bytes | None = None, markup: dict | None = None):
        self.text, self.png, self.markup = text, png, markup

    def send(self, api: TelegramApi, chat_id) -> None:
        if self.png:
            api.send_photo(chat_id, self.png, self.text, silent=True)
        else:
            api.send_message(chat_id, self.text, silent=True, markup=self.markup)


# The service runs as the unprivileged user "signal"; deploy/sudoers/signal-mt5 lets it run
# exactly these two commands and nothing else.
SCREENSHOT_CMD = [
    "sudo",
    "-n",
    "-u",
    "mt5",
    "/usr/bin/import",
    "-display",
    ":99",
    "-window",
    "root",
    "png:-",
]
RESTART_CMD = ["sudo", "-n", "/usr/bin/systemctl", "restart", "mt5-terminal"]


def take_screenshot() -> bytes | None:
    """PNG of the headless MT5 display (read-only). None if it is not available."""
    try:
        res = subprocess.run(SCREENSHOT_CMD, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 and res.stdout[:4] == b"\x89PNG" else None


def restart_mt5() -> tuple[bool, str]:
    """Restart the mt5-terminal service. Returns (ok, short message)."""
    try:
        res = subprocess.run(RESTART_CMD, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    if res.returncode != 0:
        return False, (res.stderr.strip().splitlines() or ["failed"])[-1][:120]
    return True, "restarted"
