"""The evening catch-up: what's still unmarked from today, asked about once, in the one batched status question.

An unmarked item's outcome is unknown: it may well have happened. So nothing is moved by itself and nothing is
called "not done". Once an evening, the items nobody has asked about yet (a check-in may have already) go into one
question, worded like any check-in: "How did it go? Did you get to A, B and C?" There's no separate offer and no
yes to wait for. The answer is the user's own report, acted on through the usual tools:

- what happened is recorded as done;
- what didn't happen goes back to its list, to be fitted where it makes sense, and the answer says so in a line
  (``refit``): tonight only when it can't wait and there's room (never for a skip, which means not today),
  otherwise a later day this week (never tomorrow unless the user names it).

Only one-off items are ever in it (``behind``). Some things never roll: meals, routines and their steps,
appointments, and anything tied to a time that has passed (a calendar event, a fixed time, a time the user
stated).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from thirties_core.models import EventSpan

KEY = "evening_catch_up"
EVENING_FROM_HOUR = 18  # asked from 6 PM on
BEFORE_BED = timedelta(minutes=90)  # and only while there's an evening left
logger = logging.getLogger(__name__)


def _load(db) -> dict:
    try:
        return json.loads(db.get_sync_meta(KEY) or "{}") if db is not None else {}
    except ValueError:
        return {}


def rolls(tools, span: EventSpan) -> bool:
    """Whether an unmarked item from earlier today may go on tonight: a one-off with no time of its own. Meals,
    routines and routine steps, daily things, appointments and calendar events, and anything set at a time (fixed,
    or a time the user gave) stay where they were: their moment is over."""
    from thirties_core import kinds
    from thirties_core.models import SpanCategory
    if span.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK) or span.source in ("calendar", "routine") \
            or span.tentative or span.shown_only or span.is_firm or span.given_start or span.given_end:
        return False
    if kinds.is_meal(span.title) or kinds.kind_by_wording(span.title) == kinds.DAILY:
        return False
    try:
        if tools._is_fixed(span) or tools._is_routine(span):
            return False
        assistant = getattr(tools, "assistant", None)
        if assistant is not None and not kinds.rolls_over(assistant.kind_of(span)):
            return False  # a routine step, a daily item or project time comes round again anyway
    except Exception as e:  # never put something on tonight that might not belong there
        logger.warning("Couldn't tell whether %r rolls: %s", span.title, e)
        return False
    return True


def behind(tools, missed: List[EventSpan]) -> List[EventSpan]:
    """What the evening catch-up may carry: the unmarked items that roll (``rolls``), nothing else."""
    return [s for s in missed if rolls(tools, s)]


def due(tools, now: datetime, items: List[EventSpan]) -> bool:
    """Evening, still awake for a while, not asked yet today, and something unmarked nobody has asked about."""
    from thirties_core.whats_next import bedtime
    plan = tools.day_plan
    db = tools._state_db
    if db is None or not items or now.hour < EVENING_FROM_HOUR or now >= bedtime(plan) - BEFORE_BED:
        return False
    return _load(db).get("day") != plan.target_date.isoformat()


def question(names: List[str]) -> str:
    """The one batched status question, worded like a check-in about several things."""
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"👋 How did it go? Did you get to {listed}?"


def mark(tools, now: datetime, items: List[EventSpan]) -> None:
    """Asked today: never again this evening."""
    if tools._state_db is not None:
        tools._state_db.set_sync_meta(KEY, json.dumps({"day": tools.day_plan.target_date.isoformat(),
                                                       "at": now.isoformat(), "ids": [s.id for s in items]}))


def can_wait(assistant, span: EventSpan) -> bool:
    """Not urgent: the user gave it no "now" urgency and its words don't say it can't wait."""
    from thirties_core.carry import ASAP_RE
    task = next((t for t in assistant.s.tasks if span.task_id and t.id == span.task_id), None)
    return not ((task is not None and task.urgency == "now") or ASAP_RE.search(span.title or ""))


def refit(pt, span: EventSpan, line: Optional[str], tonight: bool = True) -> Optional[str]:
    """An item from earlier today the user says didn't happen: back to its list, fitted where it makes sense.
    Tonight only when it can't wait, there's room and ``tonight`` allows it (a skip never does: skip means not
    today, not_today.py); otherwise it waits for a later day this week, never tomorrow unless the user names it.
    Its copy on Today goes. The note for the tool's answer; None when this isn't such an item (it's still ahead,
    or it never rolls)."""
    if span.end_dt > pt._now() or not rolls(pt, span):
        return None
    a = getattr(pt, "assistant", None)
    if a is None or getattr(a, "_db", None) is None:
        return None
    title = line or span.title
    if tonight and not can_wait(a, span):
        from thirties_core.today_times import retime_held
        tonight = retime_held(a, title, span.task_id, pt._now(), said="can't wait, so it's on tonight at")
        if tonight:
            return tonight
    from thirties_core.leave_today import remove_copy
    from thirties_core.not_today import home_task, minutes_for, set_aside, when_words, where_to
    try:  # a later day this week, never tomorrow unless the user names it (not_today.py)
        task = home_task(a, span.title, span.task_id)
        day, named = where_to(pt, span.title, minutes_for(a, span.title, task))
        set_aside(a, span.title, task, day, named)
        if line is not None:
            remove_copy(a, line)
    except Exception as e:
        logger.warning("Couldn't put %r back for this week: %s", span.title, e)
        return None
    return f" ({span.title} goes back to its list, for {when_words(pt, day, named)})"


__all__ = ["KEY", "rolls", "behind", "due", "question", "mark", "can_wait", "refit"]
