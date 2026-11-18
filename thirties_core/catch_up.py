"""The evening catch-up: when things from today are still not marked in the evening, one offer to move them to
tonight ("Move these 5 to tonight?"), and a plain yes (or a 👍 on Telegram) that does it.

Only one-off items that could still happen tonight are ever in it (``behind``). Some things never roll: meals,
routines and their steps, appointments, and anything tied to a time that has passed (a calendar event, a fixed
time, a time the user stated). And an unmarked item's outcome is unknown: it may well have happened, so the offer
never calls it "not done"; it only puts it on tonight in case it hasn't.

Listing what's behind isn't help on its own (11-14: five items listed under "Nothing else is planned today", and
the user had to ask what about them). The background service makes the offer once an evening (``due``, ``offer``),
when there's room tonight; the yes is answered here, by Python, from either the app or the phone (``answer``),
so it moves exactly what was offered, through the same tools as any other change. Anything else said next lets
the offer go: the conversation goes on as usual, and the assistant sees the offer there.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from thirties_core.clock import format_clock, thirty_ceil, thirty_of
from thirties_core.models import EventSpan, SpanStatus

KEY = "evening_catch_up"
EVENING_FROM_HOUR = 18  # the offer is made from 6 PM on
BEFORE_BED = timedelta(minutes=90)  # and only while there's an evening left
OPEN_FOR = timedelta(hours=2)  # a yes later than this isn't taken as an answer to it
MIN_ITEMS = 2  # one item behind is a check-in's job
HALF = timedelta(minutes=30)
logger = logging.getLogger(__name__)

YES_RE = re.compile(r"^\s*(?:y|yes|yeah|yea|yep|yup|sure|ok|okay|k|please|yes please|do it|go ahead|sounds good|"
                    r"let'?s do it|perfect|great|👍[\U0001F3FB-\U0001F3FF]?)\s*[.!]*\s*$", re.IGNORECASE)


def _load(db) -> dict:
    try:
        return json.loads(db.get_sync_meta(KEY) or "{}") if db is not None else {}
    except ValueError:
        return {}


def _save(db, data: dict) -> None:
    if db is not None:
        db.set_sync_meta(KEY, json.dumps(data))


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


def placements(tools, items: List[EventSpan], now: datetime) -> Tuple[List[Tuple[EventSpan, datetime]], List[EventSpan]]:
    """Where each item would go tonight, in order: from the next thirty to bedtime, in the open thirties, short
    items sharing a thirty (their minutes stack, as plan_tools does), longer ones in whole thirties. Returns
    (item, start) pairs and what doesn't fit."""
    from thirties_core.whats_next import bedtime
    plan = tools.day_plan
    bed = bedtime(plan)
    start = thirty_ceil(now + timedelta(minutes=1))
    free: List[datetime] = []
    for a, b in tools.open_windows(after=start, before=bed):
        t = max(a, start)
        while t + HALF <= b:
            free.append(t)
            t += HALF
    placed, left = [], []
    current: Optional[datetime] = None
    used = timedelta(0)
    for item in items:
        length = max(item.end_dt - item.start_dt, timedelta(minutes=5))
        if length < HALF and current is not None and used + length <= HALF:
            placed.append((item, current))
            used += length
            continue
        need = -(-length // HALF)  # whole thirties
        run = next((i for i in range(len(free)) if all(
            i + k < len(free) and free[i + k] == free[i] + k * HALF for k in range(need))), None)
        if run is None:
            left.append(item)
            continue
        at = free[run]
        del free[run:run + need]
        placed.append((item, at))
        current, used = (at, length) if length < HALF else (None, timedelta(0))
    return placed, left


def due(tools, now: datetime, missed: List[EventSpan]) -> bool:
    """Evening, still awake for a while, no offer yet today, two or more things behind, and room for at least
    one of them tonight."""
    plan = tools.day_plan
    from thirties_core.whats_next import bedtime
    db = tools._state_db
    if db is None or now.hour < EVENING_FROM_HOUR or now >= bedtime(plan) - BEFORE_BED:
        return False
    if _load(db).get("day") == plan.target_date.isoformat():
        return False
    if len(missed) < MIN_ITEMS:
        return False
    placed, _ = placements(tools, missed, now)
    return bool(placed)


def offer(tools, now: datetime, missed: List[EventSpan]) -> str:
    """The offer's text, recorded as open: what's behind, then the one question."""
    from thirties_core.whats_next import unmarked_lines
    _save(tools._state_db, {"day": tools.day_plan.target_date.isoformat(), "at": now.isoformat(), "state": "offered",
                            "ids": [s.id for s in missed]})
    n = len(missed)
    lines = unmarked_lines(missed)
    return "\n".join(lines) + "\n\n" + (f"Move these {n} to tonight?" if n > 1 else "Move it to tonight?")


def pending(tools, now: datetime) -> Optional[dict]:
    data = _load(tools._state_db)
    if data.get("state") != "offered" or data.get("day") != tools.day_plan.target_date.isoformat():
        return None
    try:
        if now - datetime.fromisoformat(data["at"]) > OPEN_FOR:
            return None
    except (KeyError, ValueError):
        return None
    return data


def _listed(titles: List[str]) -> str:
    return titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + f" and {titles[-1]}"


def apply(tools, now: datetime, ids: List[str]) -> Tuple[str, List[EventSpan]]:
    """Move what was offered (and is still planned and behind) to tonight. Returns the line saying what moved and
    what didn't fit, and the moved items."""
    from thirties_core.plan_tools import NOT_DONE
    by_id = {s.id: s for s in tools.day_plan.spans}
    items = [by_id[i] for i in ids if i in by_id and by_id[i].status == SpanStatus.PLANNED and by_id[i].end_dt <= now]
    if not items:
        return "Those are all taken care of already.", []
    placed, left = placements(tools, items, now)
    moved, failed = [], list(left)
    for item, at in placed:
        result = tools.execute("update_event", {"event": item.id, "start": format_clock(at)})
        (failed if result.startswith(NOT_DONE) else moved).append(item)
    by_id = {s.id: s for s in tools.day_plan.spans}
    moved = [by_id.get(s.id, s) for s in moved]
    parts = []
    if moved:
        parts.append(f"Moved to tonight: {_listed([s.title for s in moved])}.")
    if failed:
        parts.append(f"No room tonight for {_listed([s.title for s in failed])}.")
    return " ".join(parts), moved


def let_go(manager) -> None:
    """Something other than a yes was said: an open offer isn't answered by a later yes."""
    try:
        tools = manager.tools
        data = pending(tools, tools._now()) if tools._state_db is not None else None
        if data is not None:
            _save(tools._state_db, {**data, "state": "passed"})
    except Exception as e:
        logger.warning("Letting the evening offer go failed: %s", e)


def answer(manager, text: str, via: str) -> str:
    """A plain yes to an open offer: the items are moved and the reply says so, then what's next. The exchange
    is kept in the conversation like any other. "" when this isn't that (and anything else lets the offer go)."""
    tools = manager.tools
    db = tools._state_db
    if db is None or not tools.is_current(tools.day_plan):
        return ""
    now = tools._now()
    data = pending(tools, now)
    if data is None or not YES_RE.match(text or ""):
        return ""  # anything else: let_go
    from thirties_core import whats_next
    from thirties_core.messages import join_sections
    with manager.turn_lock:
        manager.refresh()
        _save(db, {**data, "state": "accepted"})
        db.set_sync_meta("last_user_message_at", now.isoformat())
        said, moved = apply(tools, now, data.get("ids") or [])
        part = whats_next.summary(tools, now, moved=moved)  # as the person wants it (reply_extras.py)
        if part:
            whats_next.SentSummaries(db).mark(tools.day_plan, now)
        reply = join_sections(said, part)
        manager.record_exchange(text.strip(), reply, via=via)
    if via == "app":  # the phone sees what was typed in the app and the answer (one conversation)
        from thirties_core.turns import Incoming, mirror_turn
        mirror_turn([Incoming(text.strip(), via, now.isoformat(timespec="seconds"))], reply, via)
    return reply


__all__ = ["KEY", "YES_RE", "rolls", "behind", "due", "offer", "pending", "apply", "answer", "placements"]
