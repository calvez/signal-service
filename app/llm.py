"""OpenRouter chat-completions client with a daily budget guard.

Model id, provider order and fallback policy come from config.yaml (`llm:`), never from code.
Every call that reaches the network is logged in `llm_calls` (prompt version, full prompt, raw
response, model, provider, latency, tokens, cost). The reader (T7) adds the parsed result and
the validation outcome to the same row. The API key is only ever put in the Authorization header
and is never logged or stored.

Failure policy: an error is returned, never raised, so the caller can fail closed (no alert).
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app import db
from app.config import Settings

log = logging.getLogger("signal.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_STORED_BODY = 20_000  # characters of a response body kept in the log


@dataclass(frozen=True)
class LlmResult:
    ok: bool
    call_id: int | None  # row in llm_calls; None when no call was made (budget)
    text: str | None  # the model's message content
    error: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0


def utc_day_start(now: float) -> int:
    d = datetime.fromtimestamp(now, tz=UTC)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class LlmClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ):
        self._s = settings
        self._cfg = settings.config.llm
        self._http = httpx.Client(transport=transport, timeout=self._cfg.timeout_sec)
        self._now = now

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ budget
    def spend_today(self) -> float:
        conn = db.connect(self._s.db_path)
        try:
            return db.llm_spend_since(conn, utc_day_start(self._now()))
        finally:
            conn.close()

    def _budget_exceeded(self) -> bool:
        """True once today's spend has reached the cap. Announces it once per day as an event
        (`llm_budget_exceeded`) which the Telegram monitor turns into one alert."""
        day = utc_day_start(self._now())
        conn = db.connect(self._s.db_path)
        try:
            spent = db.llm_spend_since(conn, day)
            if spent < self._cfg.daily_budget_usd:
                return False
            if not db.event_exists_since(conn, "llm_budget_exceeded", day):
                db.log_event(
                    conn,
                    "llm_budget_exceeded",
                    {"spent_usd": round(spent, 4), "cap_usd": self._cfg.daily_budget_usd},
                )
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------ request
    def _body(self, system: str, user: str) -> dict:
        provider: dict = {
            "allow_fallbacks": self._cfg.allow_fallbacks,
            "require_parameters": True,  # only providers that honour response_format
        }
        if self._cfg.provider_order:
            provider["order"] = self._cfg.provider_order
        return {
            "model": self._cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._cfg.temperature,
            "response_format": {"type": "json_object"},
            "provider": provider,
            "usage": {"include": True},  # ask OpenRouter to report the cost
        }

    def _post(self, body: dict) -> httpx.Response:
        """POST with one retry, on network errors only (never on an HTTP error status)."""
        headers = {"Authorization": f"Bearer {self._s.secrets.openrouter_api_key}"}
        try:
            return self._http.post(OPENROUTER_URL, json=body, headers=headers)
        except httpx.TransportError as exc:
            log.warning("OpenRouter network error (%s), retrying once", type(exc).__name__)
            return self._http.post(OPENROUTER_URL, json=body, headers=headers)

    def complete(self, system: str, user: str, purpose: str = "market_read") -> LlmResult:
        """One chat completion. Returns LlmResult; never raises for API or network problems."""
        if not self._s.secrets.openrouter_api_key:
            return LlmResult(False, None, None, "OPENROUTER_API_KEY is empty")
        if self._budget_exceeded():
            return LlmResult(False, None, None, "daily LLM budget exceeded")

        row = {
            "ts_utc": int(self._now()),
            "purpose": purpose,
            "prompt_version": self._cfg.prompt_version,
            "model": self._cfg.model,
            "prompt": f"### SYSTEM\n{system}\n\n### USER\n{user}",
            "status": "error",
        }
        started = time.monotonic()
        text: str | None = None
        error: str | None = None
        try:
            resp = self._post(self._body(system, user))
            row["raw_response"] = resp.text[:MAX_STORED_BODY]
            if resp.status_code != 200:
                error = f"HTTP {resp.status_code}"
            else:
                data = resp.json()
                row["provider"] = data.get("provider")
                usage = data.get("usage") or {}
                row["tokens_in"] = usage.get("prompt_tokens")
                row["tokens_out"] = usage.get("completion_tokens")
                row["cost_usd"] = usage.get("cost")
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if not isinstance(text, str) or not text.strip():
                    text, error = None, "empty model response"
        except httpx.HTTPError as exc:
            error = f"network error: {type(exc).__name__}"
        except (ValueError, AttributeError, IndexError, TypeError) as exc:
            text, error = None, f"unreadable response: {type(exc).__name__}"

        row["latency_ms"] = int((time.monotonic() - started) * 1000)
        if error is None:
            row["status"] = "ok"
        else:
            row["error"] = error
            log.warning("LLM call failed: %s", error)

        conn = db.connect(self._s.db_path)
        try:
            call_id = db.insert_llm_call(conn, row)
        finally:
            conn.close()
        return LlmResult(
            ok=error is None,
            call_id=call_id,
            text=text,
            error=error,
            cost_usd=float(row.get("cost_usd") or 0.0),
            latency_ms=row["latency_ms"],
        )


def parse_json_object(text: str) -> dict | None:
    """Parse the model's answer as ONE JSON object. Tolerates a markdown code fence around it
    (some models add one) but nothing else; anything else is None (fail closed)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
