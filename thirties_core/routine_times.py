"""A time the user gave for an item wins when Thirties' own planning places it: a routine, a leftover, the morning.

given_times.py keeps a time the user states for an item they place themselves. Thirties' own placing went by its
own times instead: asked for a routine step at 12:15 ("vent the kiln at 12:15 today"), put_routine put the step at
the routine's usual 12:00, and nothing later moved it; of two steps given times on another day, one got its time
and the other didn't (only a time the model typed in front of a step was read). The same held for a leftover or a
day proposal placing an item the user had given a time.

Now, whenever Thirties places an item itself (put_routine, a leftover, the morning set-up, a proposal, any tool
that places through another one), a time the user gave for that item goes first:

- **which time**: a clause of the user's words that names the item outright and gives a time ("log the firing at
  2:05"); this message first, then their earlier messages today, newest first, so a later time replaces an earlier
  one. A clause naming the routine itself ("the kiln check at 12") gives no step its time;
- **which day**: words about another day ("tomorrow") never time today's items; for a later day's plan, an earlier
  message counts only when it names that day;
- **still standing**: a time from an earlier message that has passed is left alone; one this message gives is
  always applied;
- **theirs**: the item keeps it as ``given_start``, so it's shown to the minute and a reply may say it (shown_times,
  clock.real_times).
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Iterator, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_OTHER_DAY_RE = re.compile(r"\b(?:tomorrow|tmrw|tonight's|yesterday|next week|(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|"
                           r"fri|sat(?:ur)?|sun)(?:day)?s?)\b", re.IGNORECASE)
_PM_RE = re.compile(r"p\.?m\.?", re.IGNORECASE)
_AM_RE = re.compile(r"a\.?m\.?", re.IGNORECASE)

# Thirties' own placing, said outright by a caller that places outside a tool call (the morning set-up)
_own: contextvars.ContextVar[bool] = contextvars.ContextVar("own_placing", default=False)


@contextlib.contextmanager
def own_placing() -> Iterator[None]:
    """What's placed in here is Thirties' own placing, even when add_event is the first tool called."""
    token = _own.set(True)
    try:
        yield
    finally:
        _own.reset(token)


def _thirties_placing() -> bool:
    from thirties_core.given_times import _PLACING
    from thirties_core.plan_log import outer_tool
    return _own.get() or outer_tool() not in _PLACING


def earlier_today(messages: Sequence[dict]) -> List[Tuple[str, str]]:
    """(when, what) for each message the user wrote before this turn, oldest first: the words a time they gave
    earlier is read from. Hidden instructions and system checks aren't theirs."""
    out = []
    for m in messages:
        text = str(m.get("content") or "")
        if m.get("role") == "user" and text.strip() and not m.get("hidden") and not text.startswith("[System check"):
            out.append((str(m.get("at") or ""), text))
    return out


def _day_of(at: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(at).date()
    except (TypeError, ValueError):
        return None


def _names_day(text: str, day: date, today: date) -> bool:
    low = text.lower()
    if day == today + timedelta(days=1) and re.search(r"\b(?:tomorrow|tmrw)\b", low):
        return True
    return _DAYS[day.weekday()] in low or _DAYS[day.weekday()][:3] + " " in low


def _messages(pt: "PlanTools", day: date) -> List[Tuple[str, bool]]:
    """(the user's words, said this turn) newest first, as far as they can time an item on ``day``."""
    try:
        today = pt.today()
    except Exception:
        return []
    out: List[Tuple[str, bool]] = []
    now = getattr(pt, "user_message", None)
    if now and not getattr(pt, "by_hand", False):
        if day == today and not _OTHER_DAY_RE.search(now):
            out.append((now, True))
        elif day != today:
            out.append((now, True))  # the day the item goes on is the one this turn talks about
    for at, text in reversed(getattr(pt, "said_earlier", None) or []):
        if _day_of(at) != today or text == now:
            continue
        if (day == today and not _OTHER_DAY_RE.search(text)) or (day != today and _names_day(text, day, today)):
            out.append((text, False))
    return out


def _times(clause: str) -> List[Tuple[int, int, str]]:
    """(hour, minute, "am"/"pm"/"") for each time the clause gives."""
    from thirties_core.facts import _SAID_TIME_RE
    out = []
    for m in _SAID_TIME_RE.finditer(clause or ""):
        h = m.group(1) or m.group(3) or m.group(5)
        mm = m.group(2) or m.group(4) or m.group(6) or "0"
        if not h or int(h) > 23 or int(mm) > 59:
            continue
        after = clause[m.end(0):].lstrip()
        half = "pm" if _PM_RE.search(m.group(0)) or _PM_RE.match(after) else \
            "am" if _AM_RE.search(m.group(0)) or _AM_RE.match(after) else ""
        out.append((int(h), int(mm), half))
    return out


def _on_day(plan: Any, hour: int, minute: int, half: str, near: datetime) -> Optional[datetime]:
    """The time on the plan's canvas the user meant: by am/pm when said, else the one nearest ``near``."""
    base = plan.day_start.replace(hour=0, minute=0, second=0, microsecond=0)
    if hour > 12:
        hours = [hour]
    elif half == "pm":
        hours = [hour % 12 + 12]
    elif half == "am":
        hours = [hour % 12]
    else:
        hours = [hour % 12, hour % 12 + 12]
    found = []
    for h in hours:
        for days in (0, 1):  # a canvas runs past midnight
            dt = base + timedelta(days=days, hours=h, minutes=minute)
            if plan.day_start <= dt < plan.day_end:
                found.append(dt)
    return min(found, key=lambda dt: abs(dt - near)) if found else None


def said_time(pt: "PlanTools", plan: Any, title: str, near: datetime, also: Iterable[str] = ()) -> Optional[datetime]:
    """The start the user gave for this item, on ``plan``'s day, or None. ``also``: names that aren't on the plan
    yet but are said alongside (the routine and its other steps), so a word they share names neither."""
    if getattr(pt, "by_hand", False) or not title:
        return None
    from thirties_core.given_times import _clauses
    from thirties_core.item_facts import mentions
    titles = list(dict.fromkeys([s.title for s in plan.spans if s.title] + [t for t in also if t] + [title]))
    others = [t for t in titles if t != title]
    try:
        now = pt._now()
    except Exception:
        now = None
    for message, this_turn in _messages(pt, plan.target_date):
        for clause in _clauses(message):
            times = _times(clause)
            if not times or not mentions(clause, title, others):
                continue
            hour, minute, half = times[0]
            dt = _on_day(plan, hour, minute, half, near)
            if dt is None:
                continue
            if not this_turn and now is not None and dt < now:
                return None  # passed: it no longer stands
            return dt
    return None


def placing_at(pt: "PlanTools", title: str, start: datetime, end: datetime) -> Tuple[datetime, datetime, bool]:
    """add_event, placing for Thirties' own planning: (start, end) moved to the time the user gave for the item, and
    whether it was. Anything the user or the model placed themselves is left to given_times.py."""
    if not _thirties_placing():
        return start, end, False
    from thirties_core.models import SpanStatus
    plan = pt.day_plan
    names = [s.title for s in plan.spans if s.status == SpanStatus.PLANNED]
    at = said_time(pt, plan, title, start, names + list(_ALSO.get()))
    if at is None or at == start:
        return start, end, at is not None
    return at, at + (end - start), True


# the names said alongside an item being placed (a routine and its steps), while it's placed
_ALSO: contextvars.ContextVar[Tuple[str, ...]] = contextvars.ContextVar("placing_alongside", default=())


@contextlib.contextmanager
def alongside(names: Iterable[str]) -> Iterator[None]:
    token = _ALSO.set(tuple(names))
    try:
        yield
    finally:
        _ALSO.reset(token)


__all__ = ["alongside", "earlier_today", "own_placing", "placing_at", "said_time"]
