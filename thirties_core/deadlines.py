"""Deadlines that come round again: a missed one moves to the next, with time to prepare planned before it.

Some deadlines repeat: a filing window every quarter, a renewal every year. The user says so (set_item_facts with
``repeats``: monthly, quarterly or yearly), and the item keeps it; Python never guesses it from the wording.

When such a deadline passes with the item still open, it doesn't end and isn't dropped. Python:

- moves its deadline to the same point in the next period (the next quarter's, say; further on if more than one
  has passed);
- plans the time to prepare backwards from it: an intense week, a session each day there's room (budgets.py),
  starting a month before the new deadline, or a third of the period if that's shorter (ten days for a monthly
  one). For a quarter, that's about two months out.

The morning says where it went, so the user can correct it (the model can move the week with pin_to_period).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
PREP_LEAD_DAYS = 30      # the prep week starts this long before the deadline, or a third of the period if shorter
PREP_SESSIONS = 7        # an intense week: a session each day it has room
PREP_MINUTES = 60        # each, unless the item has a usual length


def add_months(d: date, months: int) -> date:
    """The same day ``months`` later, kept within the month (Jan 31 + 1 month is Feb 28). A deadline on a month's
    last day stays on the last day (a quarter's end, Sep 30, is followed by Dec 31)."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    if d.day == calendar.monthrange(d.year, d.month)[1]:
        return date(y, m, last)
    return date(y, m, min(d.day, last))


def next_deadline(due: date, repeats: str, after: date) -> Optional[date]:
    """The first deadline of the series after ``after``."""
    step = STEP_MONTHS.get(repeats)
    if not step:
        return None
    n, nxt = 1, add_months(due, step)
    while nxt <= after:
        n += 1
        nxt = add_months(due, step * n)
    return nxt


def prep_start(due: date, repeats: str) -> date:
    """When preparing starts: a month before the deadline, or a third of the period if that's shorter."""
    period_days = (due - add_months(due, -STEP_MONTHS.get(repeats, 3))).days
    return due - timedelta(days=min(PREP_LEAD_DAYS, period_days // 3))


def prep_week(due: date, today: date, week_start: int = 0, repeats: str = "quarterly"):
    """The week preparing starts in (prep_start); this week if that day has already come."""
    from thirties_core.horizons import period_of
    start = prep_start(due, repeats)
    return period_of("week", max(start, today), week_start)


def roll_missed(assistant, day: date) -> List[Dict[str, Any]]:
    """Every open item whose repeating deadline passed before ``day``: its next deadline set and a prep week
    planned. Returns what moved, for the morning: {"title", "due", "prep"} (dates, ISO)."""
    from thirties_core.horizons import HorizonPlanner
    from thirties_core.item_facts import LastingFacts
    from thirties_core.lists import URGENCIES, ListCatalog
    db = assistant._db
    if db is None:
        return []
    facts = LastingFacts(db)
    planner = None
    out = []
    for t in list(assistant.s.tasks):
        if not t.due or t.someday:
            continue
        try:
            due = date.fromisoformat(t.due[:10])
        except ValueError:
            continue
        if due >= day:
            continue
        repeats = facts.for_item(t.title).repeats
        nxt = next_deadline(due, repeats, day - timedelta(days=1)) if repeats else None
        if nxt is None:
            continue
        try:
            note = t.parent_note_id if t.is_checkbox_item else t.id
            urgency = t.urgency if t.urgency in URGENCIES and t.urgency != "someday" else "soon"
            ListCatalog(db).set_urgency(t.id, urgency, due=nxt, note_id=note or "", title=t.title, source="user")
            t.due = nxt.isoformat()
            planner = planner or HorizonPlanner(assistant.pt)
            week = prep_week(nxt, day, planner.week_start, repeats)
            minutes = facts.for_item(t.title).minutes or PREP_MINUTES
            planner.pin(t, week, PREP_SESSIONS, minutes)
        except Exception as e:  # one item's trouble never stops the morning
            logger.warning("Couldn't move %s to its next deadline: %s", t.title, e)
            continue
        out.append({"title": t.title, "due": nxt.isoformat(), "prep": week.start.isoformat()})
    return out


def moved_text(moved: List[Dict[str, Any]], today: date, week_start: int = 0, short=lambda t: t) -> str:
    """The morning's word on them: "Quarterly filing: next deadline Dec 31; a prep week from Nov 23."."""
    lines = []
    for m in moved:
        due, prep = date.fromisoformat(m["due"]), date.fromisoformat(m["prep"])
        lines.append(f"{short(m['title'])}: next deadline {due.strftime('%b %-d')}; a prep week from "
                     f"{prep.strftime('%b %-d')}.")
    return "\n".join(lines)


__all__ = ["add_months", "next_deadline", "prep_start", "prep_week", "roll_missed", "moved_text", "PREP_SESSIONS", "STEP_MONTHS"]
