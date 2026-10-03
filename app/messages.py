"""Text of every Telegram message that is built from data (plain text, no markup, so nothing
needs escaping). Times are shown in Budapest time with the exchange time in brackets where it
matters (docs/telegram.md)."""

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import AppConfig

CHOICES = {"take": "I'd take it", "skip": "Skip", "unsure": "Unsure"}


def short_name(symbol: str) -> str:
    return symbol.split(".")[0]


def fmt_time(epoch: int, tz: str, with_date: bool = False) -> str:
    dt = datetime.fromtimestamp(epoch, tz=ZoneInfo(tz))
    return f"{dt:%a %H:%M}" if with_date else f"{dt:%H:%M}"


def money(x: float, signed: bool = False) -> str:
    return f"{x:+,.2f}".replace("-", "−") if signed else f"{x:,.2f}"


def context_line(context: dict, alignment: str) -> str:
    day = context["day_type"].replace("_", " ")
    htf = alignment.replace("aligned_", "H1/D1 aligned ").replace("conflict", "H1/D1 conflict")
    return f"Context: {day}, always-in {context['always_in']}, {htf}"


def alert_text(read: sqlite3.Row, cfg: AppConfig, digits: int = 1) -> str:
    """The alert/watch message (docs/protocol.md §5)."""
    setup = json.loads(read["setup"])
    context = json.loads(read["context"])
    d = digits
    long = setup["direction"] == "long"
    sym = read["symbol"]
    sess = cfg.sessions[cfg.symbols[sym].session]
    bar = read["bar_time_utc"]
    exch_city = sess.tz.split("/")[-1].replace("_", " ")
    when = (
        f"bar {fmt_time(bar, cfg.telegram.display_tz)} Budapest · "
        f"{fmt_time(bar, sess.tz)} {exch_city}"
    )
    is_watch = read["action"] == "watch"
    mark = "👀" if is_watch else ("🟢" if long else "🔴")
    word = f"WATCH {setup['direction'].upper()}" if is_watch else setup["direction"].upper()
    entry, stop, target = setup["entry"], setup["stop"], setup["target"]
    risk, reward = abs(entry - stop), abs(target - entry)
    atr = read["atr"]
    atr_txt = f", {risk / atr:.1f} ATR" if atr else ""
    lines = [
        f"{mark} {short_name(sym)} {word} · {setup['type']} · {setup['grade']}   ({when})",
        context_line(context, read["htf_alignment"]),
        f"Entry  {entry:.{d}f} ({'buy' if long else 'sell'} stop)",
        f"Stop   {stop:.{d}f}  ({risk:.{d}f} pts{atr_txt})",
        f"Target {target:.{d}f}  ({reward / risk:.1f}R)",
        f"Why: {read['reason']}",
        f"Model: {read['model']} · prompt {read['prompt_version']} · read #{read['id']}",
    ]
    if read["validation"] != "ok":
        lines.append(f"Note: {read['validation'].removeprefix('ok: ')}")
    return "\n".join(lines)


def feedback_buttons(read_id: int, chosen: str | None = None) -> dict:
    """Inline keyboard. After a choice the row shrinks to the chosen button with a tick."""
    if chosen:
        return {"inline_keyboard": [[{"text": f"✓ {CHOICES[chosen]}", "callback_data": "noop"}]]}
    return {
        "inline_keyboard": [
            [
                {"text": label, "callback_data": f"fb:{read_id}:{key}"}
                for key, label in CHOICES.items()
            ]
        ]
    }
