"""Every conversation, by day, for the app's All conversations view, without a display.

Newest day first, and within a day newest first. A conversation with nothing said in it isn't listed. A search
keeps the conversations whose title, summary or messages hold every word typed (the user's and the assistant's own
messages; never Thirties' hidden ones).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

from thirties_core.memory import Conversation, ConversationStore, is_internal


@dataclass
class ConversationEntry:
    id: str
    day: date
    title: str
    started: str        # "9:41 AM", in the user's time zone
    messages: int       # what the user said in it
    summary: str = ""


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _text(conv: Conversation) -> str:
    said = [str(m.get("content") or "") for m in conv.messages
            if m.get("role") in ("user", "assistant") and not is_internal(m)]
    return " ".join([conv.title or "", conv.summary or ""] + said).lower()


def _started(iso: str, tz) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return (dt.astimezone(tz) if tz is not None else dt).strftime("%I:%M %p").lstrip("0")


def all_conversations(store: ConversationStore, query: str = "", tz=None) -> List[Tuple[date, List[ConversationEntry]]]:
    """(day, its conversations) pairs, newest first; only those matching every word of ``query``."""
    words = _words(query)
    convs = store.between(date.min, date.max)
    days: dict = {}
    for conv in convs:
        if not any(m.get("role") == "user" and not is_internal(m) for m in conv.messages):
            continue
        if words:
            text = _text(conv)
            if not all(w in text for w in words):
                continue
        days.setdefault(conv.plan_date, []).append(ConversationEntry(
            conv.id, conv.plan_date, conv.title or "", _started(conv.created_at, tz),
            sum(1 for m in conv.messages if m.get("role") == "user" and not is_internal(m)), conv.summary or ""))
    out = []
    for day in sorted(days, reverse=True):
        entries = days[day]
        entries.reverse()  # store order is oldest first
        out.append((day, entries))
    return out


def day_heading(day: date, today: Optional[date] = None) -> str:
    """'Today', 'Yesterday', 'Thursday, October 15', or with its year when it isn't this year's."""
    if today is not None:
        if day == today:
            return "Today"
        if (today - day).days == 1:
            return "Yesterday"
        if day.year != today.year:
            return day.strftime("%A, %B %-d, %Y")
    return day.strftime("%A, %B %-d")


__all__ = ["ConversationEntry", "all_conversations", "day_heading"]
