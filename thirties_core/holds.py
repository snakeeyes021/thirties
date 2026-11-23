"""Hold every message until the user says something is over (a meeting, an interview).

A check-in mute (memory.CheckinMutes) quiets only check-ins, and only until a clock time. A hold quiets everything
Thirties would say by itself (start notices, check-ins, reminders, heads-ups, offers) on the desktop and the phone,
and it ends on the user's word:
- the item it waits for gets an outcome ("interview done": the interview is marked done),
- or the user says the words it waits for, in the app or on the phone ("interview over", "it's over"),
- or they say to end it (end_hold), or the time it was given passes.
It never outlasts the day: it ends at the start of the next one at the latest. Reminders asked for during it are
kept and go out once it ends; notices about a moment that passed are not sent late.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

KEY = "message_hold"
PASSES = ("reply", "hold")  # what still goes out during a hold: answers to the user's own messages, and its end

HOLD_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "hold_messages",
        "description": "\"Hold my messages until …\", \"no notifications till I'm back\": hold everything Thirties "
                       "sends by itself (start notices, check-ins, reminders) until the user says it's over. Give "
                       "what it waits for: an item on the plan (it ends when that's marked), the words the user "
                       "will say (\"interview over\"), and/or a clock time. Unlike mute_checkins, which pauses check-ins "
                       "only.",
        "parameters": {"until_done": "The item it waits for, id or title (optional).",
                       "until_said": "The words that end it, e.g. 'the interview's over' (optional).",
                       "until": "A clock time it ends at (optional; default: when they say so, at the latest "
                                "the end of the day)."},
        "required": [],
    },
    {
        "name": "end_hold",
        "description": "End a hold on messages (hold_messages) when the user says it's over or to send messages "
                       "again.",
        "parameters": {},
        "required": [],
    },
]

_STOP = {"i", "i've", "ive", "im", "i'm", "have", "has", "had", "the", "a", "an", "it", "its", "it's", "that", "this",
         "is", "was", "be", "been", "done", "back", "say", "said", "mention", "tell", "you", "my", "me", "and", "or",
         "until", "till", "when", "once", "after", "with", "of", "to", "for", "on", "at", "just", "now", "got"}


def _stem(word: str) -> str:
    word = word.lower().strip("'’")
    for suffix in ("ing", "ed", "es", "s", "e"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _words(text: str) -> List[str]:
    return [_stem(w) for w in re.findall(r"[\w'’]+", text or "") if w.lower() not in _STOP and len(w) > 2]


def _load(db) -> dict:
    try:
        data = json.loads(db.get_sync_meta(KEY) or "{}") if db is not None else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def active(db, now: datetime) -> Optional[dict]:
    """The hold running now, if any."""
    data = _load(db)
    if data.get("state") != "on":
        return None
    try:
        if now >= datetime.fromisoformat(data["ends"]):
            return None
    except (KeyError, ValueError):
        return None
    return data


def lets_through(data: Optional[dict], kind: str) -> bool:
    """Whether a message of this kind goes out during this hold: answers and its end always; a reminder the user
    set, when the hold came from their own words and they didn't say nothing at all (quiet_asks.py)."""
    if data is None or kind in PASSES:
        return True
    return kind == "reminder" and bool(data.get("from_words")) and not data.get("strict", True)


def start(db, now: datetime, ends: datetime, span_id: str = "", title: str = "", said: str = "") -> dict:
    words = sorted(set(_words(said) + (_words(title) if title and not said else [])))
    data = {"state": "on", "since": now.isoformat(), "ends": ends.isoformat(), "span_id": span_id,
            "title": title, "words": words, "clock": False}
    db.set_sync_meta(KEY, json.dumps(data))
    return data


def end(db, now: datetime, why: str = "") -> bool:
    data = _load(db)
    if data.get("state") != "on":
        return False
    db.set_sync_meta(KEY, json.dumps({**data, "state": "off", "ended": now.isoformat(), "why": why}))
    return True


def said_over(data: dict, messages: List[dict]) -> bool:
    """A message from the user since the hold began says the words it waits for."""
    from thirties_core.engineer_notes import without_note
    want = set(data.get("words") or [])
    if not want:
        return False
    since = data.get("since", "")
    for m in messages:
        # the message that asked for the hold never ends it, even when it uses the words ("before my next message")
        if m.get("role") != "user" or m.get("hidden") or str(m.get("at") or "") <= since:
            continue
        if want & set(_words(without_note(str(m.get("content") or "")))):  # a note to the engineers ends nothing
            return True
    return False


def check(db, plan, messages: List[dict], now: datetime) -> str:
    """Ends the hold once what it waits for has happened; returns why ("" while it holds, or with none)."""
    data = _load(db)
    if data.get("state") != "on":
        return ""
    try:
        if now >= datetime.fromisoformat(data["ends"]):
            end(db, now, "time")
            return "time"
    except (KeyError, ValueError):
        end(db, now, "time")
        return "time"
    from thirties_core.models import SpanStatus
    span = next((s for s in plan.spans if s.id == data.get("span_id")), None) if data.get("span_id") else None
    if span is not None and span.status != SpanStatus.PLANNED:
        end(db, now, "done")
        return "done"
    if said_over(data, messages):
        end(db, now, "said")
        return "said"
    if data.get("until_user") and wrote_since(data, messages):  # "until I write" (quiet_asks.py): they wrote
        end(db, now, "wrote")
        return "wrote"
    return ""


def wrote_since(data: dict, messages: List[dict]) -> bool:
    """A message from the user after the one that asked for the hold."""
    since = data.get("since", "")
    return any(m.get("role") == "user" and not m.get("hidden") and str(m.get("at") or "") > since
               and not str(m.get("content") or "").startswith("[System check") for m in messages)


class HoldTools:
    """The two tools, on AssistantTools."""

    def __init__(self, assistant) -> None:
        self.a = assistant

    def hold_messages(self, until_done: Any = None, until_said: Any = None, until: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        from thirties_core.clock import format_clock
        pt = self.a.pt
        db = self.a._db
        if db is None:
            raise ToolError("Messages can't be held here.")
        now = pt._now()
        # --- the user's own ask, already recorded from their words this turn, stands (quiet_asks.py) ---
        from thirties_core import quiet_asks
        mine = quiet_asks.recorded_for(db, getattr(pt, "user_message", None), now)
        if mine is not None:
            return quiet_asks.describe(mine)
        # --- end the user's own ask ---
        span = pt._find_span(until_done) if until_done not in (None, "") else None
        ends = pt.day_plan.day_end
        if until not in (None, ""):
            ends = min(pt._end(until, now), pt.day_plan.day_end)  # never past the day: the morning always comes
        said = str(until_said or "").strip()
        data = start(db, now, ends, span.id if span is not None else "", span.title if span is not None else "", said)
        if until not in (None, ""):
            data["clock"] = True
            db.set_sync_meta(KEY, json.dumps(data))
        if span is not None:
            return f"Holding all messages until {span.title} is done (or you say it's over)."
        if said:
            return f"Holding all messages until you say {said}."
        return f"Holding all messages until {format_clock(ends)}" + (
            " tomorrow" if ends.date() > now.date() else "") + "."

    def end_hold(self, **_: Any) -> str:
        if self.a._db is None or not end(self.a._db, self.a.pt._now(), "asked"):
            return "Messages weren't on hold."
        return "Messages are back on."

    def handlers(self) -> Dict[str, Any]:
        return {"hold_messages": self.hold_messages, "end_hold": self.end_hold}


__all__ = ["KEY", "PASSES", "lets_through", "HOLD_TOOL_SPECS", "HoldTools", "active", "start", "end", "check", "said_over"]
