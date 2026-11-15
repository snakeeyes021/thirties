"""The answer to "Done with work?": a short yes or a time, recorded by Python, with what's left of the day.

The background service asks once a day, at work's planned end, "Done with work? If so, when did you finish?"
(day_times.due_question). A short answer to it, from the app or the phone (a plain yes, "just now", "5:15", "at
5"), is recorded here through log_day_times, the same tool the assistant and the app's work button use. So the
reply always says what follows: the evening planned from work's real end, and after work the daylight and dark
thirties left with a few things that fit (day_times.replan_after_work, after_work.offer). Anything longer, a no, a
question, or an answer once the question has gone stale goes to the assistant as usual.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

ASKED = "done with work?"  # the question's own words (day_times.due_question)
OPEN_FOR = timedelta(hours=2)  # an answer later than this isn't taken as an answer to it (day_times.ASK_WINDOW)
MAX_LENGTH = 40  # longer than this, the user is saying more than when work ended

YES_RE = re.compile(r"^\s*(?:y|yes|yeah|yea|yep|yup|sure|i am|just now|just did|just finished|done|all done|finally|"
                    r"finished|that'?s it|👍[\U0001F3FB-\U0001F3FF]?)(?!\w)", re.IGNORECASE)
NO_RE = re.compile(r"\b(?:no|nope|not yet|not quite|nah|still (?:working|at it|going)|almost|soon|in a (?:bit|few))\b",
                   re.IGNORECASE)
TIME_RE = re.compile(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon)\b", re.IGNORECASE)
NOT_PAST_RE = re.compile(r"\?|\b(?:will|'ll|going to|gonna|tomorrow|by the time)\b", re.IGNORECASE)


def work_ended_from(text: str) -> Optional[str]:
    """log_day_times' work_ended for a short answer to the question: the time said, or "now" for a plain yes. None
    when it isn't one (a no, a question, more than an answer)."""
    words = (text or "").strip()
    if not words or len(words) > MAX_LENGTH or NOT_PAST_RE.search(words) or NO_RE.search(words):
        return None
    m = TIME_RE.search(words)
    if m:
        rest = (words[:m.start()] + words[m.end():]).strip(" .,!")
        if re.fullmatch(r"(?:(?:yes|yeah|yep|yup|done|finished|i finished|i was done)\W*)?(?:at|around|about|by)?",
                        rest, re.IGNORECASE):
            return m.group(0).strip()
        return None
    return "now" if YES_RE.match(words) else None


def _as_said(ended: str, now: datetime) -> str:
    """A bare clock time ("4:10", "4") as the latest one that has already come ("4:10 PM" at 4:40 PM): work's end
    is in the past. Anything else as it is."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", ended.strip())
    if not m or not 1 <= int(m.group(1)) <= 12:
        return ended
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    best = None
    for meridiem, add in (("AM", 0), ("PM", 12)):
        when = now.replace(hour=hour + add, minute=minute, second=0, microsecond=0)
        if when <= now + timedelta(minutes=5) and (best is None or when > best[0]):
            best = (when, meridiem)
    return f"{hour or 12}:{minute:02d} {best[1]}" if best else ended


def _asked_at(manager) -> Optional[datetime]:
    """When the question was the last thing said (and still unanswered): None otherwise."""
    for m in reversed(manager.brain.messages):
        role = m.get("role")
        if role == "user":
            return None  # something was said since
        if role != "assistant" or m.get("kind") == "notice":
            continue
        if ASKED not in str(m.get("content", "")).lower():
            return None
        try:
            return datetime.fromisoformat(m.get("at") or "")
        except ValueError:
            return None
    return None


def answer(manager, text: str, via: str) -> str:
    """The reply when ``text`` answers an open "Done with work?": work's end recorded, and what follows. "" when it
    doesn't (the assistant takes it)."""
    tools = manager.tools
    if tools._state_db is None or not tools.is_current(tools.day_plan):
        return ""
    ended = work_ended_from(text)
    if ended is None:
        return ""
    now = tools._now()
    asked = _asked_at(manager)
    if asked is None or not timedelta(0) <= now - asked.astimezone(now.tzinfo) <= OPEN_FOR:
        return ""
    from thirties_core.plan_tools import NOT_DONE
    from thirties_core.day_actions import work_reply
    from thirties_core.day_times import work_span
    from thirties_core.messages import join_sections
    with manager.turn_lock:
        manager.refresh()
        work = work_span(tools.day_plan)
        if work is None or work.actual_end is not None:
            return ""  # no work today, or its end is already said: the assistant answers
        result = tools.execute("log_day_times", {"work_ended": _as_said(ended, now)})
        if not result or result.startswith(NOT_DONE):
            return ""  # e.g. a time that can't be work's end: the assistant asks
        said, rest = work_reply(result)
        reply = join_sections(f"Noted: {said[:1].lower()}{said[1:]}." if said else "Noted.", rest)
        tools._state_db.set_sync_meta("last_user_message_at", now.isoformat())
        manager.record_exchange(text.strip(), reply, via=via)
    if via == "app":  # the phone sees what was typed in the app and the answer (one conversation)
        from thirties_core.turns import Incoming, mirror_turn
        mirror_turn([Incoming(text.strip(), via, now.isoformat(timespec="seconds"))], reply, via)
    return reply


__all__ = ["ASKED", "answer", "work_ended_from"]
