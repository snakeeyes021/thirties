"""What kind of item something is, and when it goes moot.

**Kinds** (ROADMAP B2, 0.2 item 7):

- **once**: done means finished. It's ticked in its list, and if it doesn't happen
  it rolls over and counts as put off.
- **ongoing**: done means "worked on it" (project time, "Torquio dev"). The session
  is recorded; the list item stays open until the user says it's finished for good.
  It never rolls over: there's another session anyway.
- **daily**: done means done today (routine steps, meals, feeding the cats). A skip
  isn't "put off", and it never rolls over: it's on tomorrow's plan anyway.

Python reads the kind (AGENTS.md §7, "obvious cases get defaults"): the item's
stored kind if the user said one (``item_facts``), else its list's role (routine →
daily, backlog → ongoing, the rest → once), else the obvious cases by wording
(meals and daily anchors → daily; "X dev", "X session", "work on X" → ongoing),
else once. The model only says "done", "worked on it" or "finished for good".

**Moot** (ROADMAP B4, 0.2 item 9): each item gets a point after which there's no
sense doing it, offering it or asking about it:

- a meal, at the next meal ("one does not eat a previously skipped day's
  breakfast", 10-29; "I skipped lunch. But I had dinner so it is moot", 10-23);
- a daily or ongoing item, at the end of its day;
- a fixed item, once its time has passed (with ``FIXED_MOOT_AFTER`` to ask once).

A moot item with no outcome is stored as "unknown" (``SpanStatus.UNKNOWN``), not
skipped: nobody knows whether it happened, and it isn't put off.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Iterable, List, Optional

from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem

if TYPE_CHECKING:
    from thirties_core.item_facts import LastingFacts

ONCE, ONGOING, DAILY = "once", "ongoing", "daily"
KINDS = (ONCE, ONGOING, DAILY)

# A list's role says what its items are. "today" isn't here: a Today line is whatever its home item is, and an
# unlinked one is read by its wording.
ROLE_KIND = {"routine": DAILY, "backlog": ONGOING, "list": ONCE, "project": ONCE, "menu": ONCE, "reference": ONCE}

# Fixed items: asking "did you get to it?" right after its time is fine (a check-in); past this, it's moot. Spec
# says "once its time has passed"; two hours is the window check-ins already use, so it's asked about at most once.
FIXED_MOOT_AFTER = timedelta(hours=2)

MEAL_RE = re.compile(r"\b(breakfast|brunch|lunch|dinner|supper)\b", re.IGNORECASE)
_MEAL_WITH_RE = re.compile(r"\b(breakfast|brunch|lunch|dinner|supper)\s+(with|at|out)\b|\b(date|reservation)\b",
                           re.IGNORECASE)
# Things done every day, obvious from the wording alone (the user's daily anchors, 10-20 to 10-30: kitties,
# caffeinate, water, breakfast, throne, shower, PT, brush teeth, lunch, dinner, cats' dinner).
DAILY_RE = re.compile(
    r"\b(breakfast|brunch|lunch|dinner|supper|snack|"
    r"feed(ing)?\s+(the\s+)?(cats?|kitt(y|ies)|dogs?|pets?|fish)|(cats?|kitt(y|ies)|dogs?|pets?)'?\s+(dinner|breakfast|food)|"
    r"kitties|meds|medications?|vitamins?|pills|brush(ing)?\s+(my\s+)?teeth|floss(ing)?|shower|caffeinate|"
    r"water bottle|bedtime|wind(ing)? down|walk the dog|make the bed|throne)\b", re.IGNORECASE)
# Time on something that goes on: "Torquio dev", "Thirties session", "work on the album", "practice piano".
ONGOING_RE = re.compile(
    r"^(?:(?P<project>.+?)\s+(dev|development|work|session|sessions|time|block|practice|study|studying|writing)"
    r"|(work(ing)?|time)\s+on\s+.+|practi[cs]e\s+.+)$", re.IGNORECASE)


def is_meal(title: str) -> bool:
    """A meal of the user's own (not "dinner with Sam", which is an appointment)."""
    return bool(MEAL_RE.search(title or "")) and not _MEAL_WITH_RE.search(title or "") and not \
        re.search(r"\b(cats?|kitt(y|ies)|dogs?|pets?)'?s?\b", title or "", re.IGNORECASE)


def kind_by_wording(title: str) -> Optional[str]:
    """The obvious cases, from the words alone; None when the wording doesn't say."""
    t = str(title or "").strip()
    if not t or t.lower() in ("work", "sleep"):
        return None
    if DAILY_RE.search(t) and not _MEAL_WITH_RE.search(t):
        return DAILY
    if ONGOING_RE.match(t):
        return ONGOING
    return None


def kind_of(title: str, task: Optional[TaskItem] = None, *, home: Optional[TaskItem] = None,
            home_role: str = "", routine_steps: Iterable[str] = (), stored: str = "") -> str:
    """An item's kind: the stored one (the user's word), its list's role (a Today line's home list's), a routine
    step, the obvious wording, else once."""
    if stored in KINDS:
        return stored
    role = (home.list_role if home is not None else "") or home_role or (task.list_role if task is not None else "")
    if role in ROLE_KIND:
        return ROLE_KIND[role]
    names = {str(s).strip().lower() for s in routine_steps}
    for t in (title, task.title if task is not None else None):
        if t and t.strip().lower() in names:
            return DAILY
    return kind_by_wording(title) or (kind_by_wording(task.title) if task is not None else None) or ONCE


def trackable(span: EventSpan) -> bool:
    """A planned thing with an outcome (not work, sleep, a calendar event or an unanswered invite)."""
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and span.source != "calendar" \
        and not span.tentative


def moot_after(span: EventSpan, kind: str, day_end: datetime, others: Iterable[EventSpan] = (),
               fixed: bool = False) -> Optional[datetime]:
    """When an item stops being worth doing, offering or asking about; None if never (a flexible one-off).
    ``others`` are the spans around it (for the next meal); ``day_end`` is the end of its diurnal day."""
    points = []
    if fixed:
        points.append(span.end_dt + FIXED_MOOT_AFTER)
    if is_meal(span.title):
        meals = [s.start_dt for s in others if s.id != span.id and is_meal(s.title) and s.start_dt >= span.end_dt
                 and s.status != SpanStatus.SKIPPED]
        points.append(min(meals + [day_end]))
    elif kind in (DAILY, ONGOING):
        points.append(day_end)
    return min(points) if points else None


def is_moot(span: EventSpan, now: datetime, kind: str, day_end: datetime, others: Iterable[EventSpan] = (),
            fixed: bool = False) -> bool:
    """Past its moot point with nothing reported: never offered, rolled over or asked about."""
    if not trackable(span) or span.status not in (SpanStatus.PLANNED, SpanStatus.UNKNOWN):
        return False
    if span.status == SpanStatus.UNKNOWN:
        return True
    point = moot_after(span, kind, day_end, others, fixed)
    return point is not None and point <= now


def settle_moot(spans: List[EventSpan], now: datetime, day_end: datetime, kind: Callable[[EventSpan], str],
                fixed: Callable[[EventSpan], bool], persist: Callable[[EventSpan], None]) -> List[EventSpan]:
    """Store every planned item of one day that has gone moot as "unknown". Returns the ones changed."""
    changed = []
    for span in spans:
        if trackable(span) and span.status == SpanStatus.PLANNED and \
                is_moot(span, now, kind(span), day_end, spans, fixed(span)):
            span.status = SpanStatus.UNKNOWN
            persist(span)
            changed.append(span)
    return changed


def rolls_over(kind: str) -> bool:
    """Only one-off things carry over to the next day: daily things and project time come round again anyway
    (10-18: "daily things don't 'carry over'… generic tasks like 'development work' they don't either")."""
    return kind == ONCE


def put_off_counts(kind: str) -> bool:
    """A skipped daily item isn't "put off": it comes back tomorrow regardless."""
    return kind != DAILY


__all__ = ["KINDS", "ONCE", "ONGOING", "DAILY", "ROLE_KIND", "FIXED_MOOT_AFTER", "kind_of", "kind_by_wording",
           "is_meal", "moot_after", "is_moot", "settle_moot", "rolls_over", "put_off_counts", "trackable"]
