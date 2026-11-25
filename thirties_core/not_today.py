"""Not today: where something goes when it isn't happening today.

The design, drawn from how people use the word: "skip" means "not today". It never means "tomorrow".

- A skipped item comes off today's plan and the Today list at once (leave_today.py).
- A meal, a routine or one of its steps, a daily thing, time on a project, or an appointment simply ends when
  it's skipped: it comes round again by itself, so it never waits anywhere (``ends_when_skipped``).
- A skipped to-do goes back to waiting on its own list and returns on a later day this week, a day Python picks
  by fit (``later_day``): never the very next day by itself. Only when the user names a day ("Saturday", "put
  it tomorrow") does it go there instead (``named_window``).
- Only the user's own drop words for that item stop it coming back, never a drop said for today
  (off_today.drop_meant).

The same holds for any move to "tomorrow" that the user didn't ask for: the model moving an item to tomorrow
(update_event with a day, If there's time's tomorrow, a leftover kept for tomorrow), or Python finding no room
for something today. Without a day in the user's words such a move goes to a later day this week, by fit; with
one, to that day. Something due today or overdue keeps its own protections (due_today.py) and isn't handled
here.

Every such path asks ``where_to`` for the day, and ``set_aside`` records where it waits: on that day's list (it
goes on the Today note that morning, day_lists.AheadLists) and in the open pool with a window starting that day,
so no earlier day offers it.

Example (invented): an evening at the pottery studio runs late, and the user skips "Trim the test mugs" planned
for 2:20 PM on a Thursday. It leaves Thursday's plan and list, isn't on Friday's, and returns on Saturday, the
first later day with room. Had they said "do the mugs Sunday", it would wait for Sunday.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional, Tuple

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# a day (or a span of days) in the user's own words
NAMED_DAY_RE = re.compile(
    r"\b(?:(?P<after>day[\s-]+after[\s-]+tomorrow)|"
    r"(?P<tomorrow>tomorrow|tmrw|tmr|(?:the )?next day|a day later|one day later|by a day|in a day|back a day)|"
    r"(?P<weekend>(?:this |the |over the )?weekend)|(?P<nextweek>next week)|"
    r"(?P<weekday>(?:next |this |on )?(?:monday|tuesday|tues|wednesday|thursday|thurs|friday|saturday|sunday))|"
    r"(?P<md>" + _MONTHS + r" \d{1,2}(?:st|nd|rd|th)?))\b", re.IGNORECASE)


def _week_start(pt: Any) -> str:
    try:
        return getattr(pt.scheduler.config.general, "week_start", "mon") or "mon"
    except AttributeError:
        return "mon"


def week_end(pt: Any, today: date) -> date:
    """The last day of the user's week that ``today`` is in."""
    from thirties_core.carry import _week_bounds
    return _week_bounds(today, _week_start(pt))[1]


def named_window(pt: Any, message: Optional[str] = None, today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """The later day (or days) the user's words name: "tomorrow", "Saturday", "the weekend", "next week",
    "Oct 20". None when they name no day after today (or nobody is talking)."""
    message = getattr(pt, "user_message", None) if message is None else message
    if not message:
        return None
    today = today or pt.today()
    from thirties_core.carry import _week_bounds
    from thirties_core.clock import ClockError, parse_day
    for m in NAMED_DAY_RE.finditer(message):
        if m.group("after"):
            d = today + timedelta(days=2)
            return d, d
        if m.group("tomorrow"):
            d = today + timedelta(days=1)
            return d, d
        if m.group("weekend"):
            if today.weekday() == 5:  # said on a Saturday: what's left of it is Sunday
                return today + timedelta(days=1), today + timedelta(days=1)
            sat = today + timedelta(days=(5 - today.weekday()) % 7)
            return sat, sat + timedelta(days=1)
        if m.group("nextweek"):
            start, end = _week_bounds(today, _week_start(pt))
            return start + timedelta(days=7), end + timedelta(days=7)
        text = m.group("weekday") or m.group("md")
        try:
            d = parse_day(text, today)
        except (ClockError, ValueError):
            continue
        if d is not None and d > today:
            return d, d
    return None


def later_day(pt: Any, title: str, minutes: int = 30, today: Optional[date] = None,
              first: Optional[date] = None, last: Optional[date] = None) -> date:
    """A later day this week for something not happening today, picked by fit: the first day from the day after
    tomorrow to the end of the week with a free stretch for it (in daylight, for something outdoors). When the
    week ends before that, or no day in it has room, the first such day in the week after; failing that, the day
    after tomorrow. Never tomorrow: that's only ever the user's word."""
    from thirties_core.replan import first_day_with_room, outdoors
    today = today or pt.today()
    first = max(first or today, today + timedelta(days=2))
    end = last or week_end(pt, today)
    light = outdoors(title, pt)
    tries = [(first, end)] if end >= first else []
    tries.append((max(first, end + timedelta(days=1)), max(first, end) + timedelta(days=7)))
    for a, b in tries:
        try:
            d = first_day_with_room(pt, max(30, int(minutes or 30)), a, b, daylight=light)
        except Exception:
            d = None
        if d is not None:
            return d
    from thirties_core.holidays import first_workable  # never a holiday by itself (holidays.py)
    return first_workable(pt, first)


def where_to(pt: Any, title: str, minutes: int = 30, today: Optional[date] = None) -> Tuple[date, bool]:
    """(the day it goes to, whether the user named it): the day in the user's words when they name one (the
    first with room, for a span of days like "the weekend"), else a later day this week by fit."""
    today = today or pt.today()
    window = named_window(pt, today=today)
    if window is not None:
        a, b = window
        if a == b:
            return a, True
        return later_day(pt, title, minutes, today, first=a, last=b) if b >= today + timedelta(days=2) else a, True
    return later_day(pt, title, minutes, today), False


def tomorrow_unasked(pt: Any, day: Optional[date]) -> bool:
    """A move to tomorrow the user's words didn't ask for: the model (or Python) picked tomorrow while the user,
    talking this turn, named no day. The app's own controls and calls with nobody talking are taken as given."""
    if day is None or getattr(pt, "by_hand", False) or getattr(pt, "user_message", None) is None:
        return False
    return day == pt.today() + timedelta(days=1) and named_window(pt) is None


def moved_day(pt: Any, span: Any, to_day: Any) -> Any:
    """The day for update_event's to_day: a move from the day being lived to tomorrow that the user didn't ask
    for (they named no day) goes to a later day this week instead, by fit. Anything else as given."""
    from thirties_core.clock import ClockError, parse_day
    from thirties_core.models import SpanStatus
    try:
        day = parse_day(to_day, pt.today())
    except (ClockError, ValueError):
        return to_day
    if span.status != SpanStatus.PLANNED or not pt.is_current(pt.day_plan) or not tomorrow_unasked(pt, day):
        return to_day
    minutes = int((span.end_dt - span.start_dt).total_seconds() // 60)
    return later_day(pt, span.title, minutes).isoformat()


def window_unasked(pt: Any, window: dict, title: str, minutes: int = 30) -> dict:
    """A leftover's window ({"when", "start", "end"}) of tomorrow alone that the user's words didn't name (the
    morning model's own pick, or a move with no day said): a later day this week instead, by fit. The app's own
    controls, and any other window, as given."""
    tomorrow = (pt.today() + timedelta(days=1)).isoformat()
    if window.get("start") != tomorrow or window.get("end") != tomorrow or getattr(pt, "by_hand", False) \
            or named_window(pt) is not None:
        return window
    d = later_day(pt, title, minutes)
    return {**window, "when": d.strftime("%A"), "start": d.isoformat(), "end": d.isoformat()}


def ends_when_skipped(a: Any, title: str, span: Any = None) -> bool:
    """Skipped, it simply ends: a meal, a routine or one of its steps, a daily thing, time on a project, an
    appointment or calendar event, or anything else that comes round again by itself."""
    from thirties_core import kinds
    from thirties_core.models import SpanCategory
    if span is not None and (span.source in ("calendar", "routine") or span.is_firm or span.is_envelope
                             or span.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK)):
        return True
    if kinds.is_meal(title) or kinds.kind_by_wording(title) == kinds.DAILY:
        return True
    if a is None:
        return False
    try:
        low = title.strip().lower()
        if low in a.routines.daily_titles() or a._routine_of(title) is not None \
                or low in {n["title"].strip().lower() for n in a._routines()}:
            return True
        return not a.carries(span if span is not None else title)
    except Exception:
        return False


def minutes_for(a: Any, title: str, task: Any = None) -> int:
    try:
        from thirties_core.day_proposal import usual_minutes
        return usual_minutes(a, task, title) or 30
    except Exception:
        return 30


def set_aside(a: Any, title: str, task: Any, day: date, named: bool) -> None:
    """It waits for ``day``: on that day's list (on the Today note that morning) and in the open pool with a
    window from that day on, so nothing offers it sooner. A day the user named stands as theirs."""
    from thirties_core.carry import WEEK, OpenPool
    from thirties_core.day_lists import AheadLists
    db = a._db
    today = a.pt.today()
    ahead = AheadLists(db)
    for rec in ahead.all_records():  # one day only: an earlier setting-aside of it gives way
        if rec.get("state") == "waiting" and rec["title"].strip().lower() == title.strip().lower() \
                and rec["day"] > today.isoformat() and rec["day"] != day.isoformat():
            ahead.set_state(date.fromisoformat(rec["day"]), rec["title"], "moved")
    ahead.add(day, title, task)
    pool = OpenPool(db)
    pool.add([{"title": title, "task_id": getattr(task, "id", "") or "", "horizon": WEEK}], today)
    end = day if named else max(day, week_end(a.pt, today))
    pool.place(title, WEEK, {"when": day.strftime("%A"), "start": day.isoformat(), "end": end.isoformat(),
                             "named_day": day.isoformat() if named else "", "set_aside": today.isoformat()})


def day_words(day: date, today: date) -> str:
    gap = (day - today).days
    return "tomorrow" if gap == 1 else day.strftime("%A") if gap < 7 else day.strftime("%a %b %-d")


def when_words(pt: Any, day: date, named: bool) -> str:
    """The day something waits for, as the answer says it: "Saturday, later this week" ("tomorrow" or the day
    alone when the user named it)."""
    words = day_words(day, pt.today())
    if named:
        return words
    return words + (", later this week" if day <= week_end(pt, pt.today()) else ", the first day with room")


def named_skip(db: Any, title: str, day: date) -> bool:
    """Skipped on ``day`` and set aside for a day the user named: a move of theirs, not a miss (backoff.py)."""
    from thirties_core.carry import OpenPool
    rec = OpenPool(db).all().get(title.strip().lower()) or {}
    return bool(rec.get("named_day")) and rec.get("set_aside") == day.isoformat()



def home_task(a: Any, title: str, task_id: Optional[str] = None) -> Any:
    """The list item a skipped line stands for (its home), when it has one."""
    tasks = list(getattr(a.s, "tasks", []) or [])
    try:
        from thirties_core.today import TodayLinks
        link = TodayLinks(a._db).home_of(title) if a._db is not None else None
    except Exception:
        link = None
    wanted = {i for i in (task_id, (link or {}).get("home_id")) if i}
    found = next((t for t in tasks if t.id in wanted and t.list_role != "today"), None)
    if found is None and a is not None:
        try:
            exact = a._exact_task(title)
        except Exception:
            exact = None
        found = exact if exact is not None and exact.list_role != "today" else None
    return found


def skipped_off(a: Any, title: str, line: Optional[str], span: Any = None) -> str:
    """Skipped for today, as the design above says: off today's list (``line``, its copy on the Today note, when
    it has one), then it ends, it's dropped (only on the user's drop words) or it waits for a later day. What
    happened, in a few words ("" when nothing changed)."""
    from thirties_core.leave_today import has_home, remove_copy
    pt = a.pt
    homed = line is not None and has_home(a, line)
    message = getattr(pt, "user_message", None)
    from thirties_core.off_today import drop_meant  # their drop words for this item, not for today (off_today.py)
    if message is not None and drop_meant(pt, title):  # the user's drop words: it stops coming back
        from thirties_core.carry import DROPPED, OpenPool
        if line is not None:
            from thirties_core.off_today import take_off_list
            gone = take_off_list(pt, line)
        else:
            gone = ""
        task = home_task(a, title, getattr(span, "task_id", None))
        pool = OpenPool(a._db)
        pool.add([{"title": title, "task_id": getattr(task, "id", "") or "", "horizon": DROPPED}], pt.today())
        pool.place(title, DROPPED)
        return (gone + "; " if gone else "") + "dropped: it won't come back by itself"
    if ends_when_skipped(a, title, span):
        if line is not None and remove_copy(a, line):
            return "off today's list; today's simply ends"
        return ""
    if line is not None and not remove_copy(a, line):
        return ""
    task = home_task(a, title, getattr(span, "task_id", None))
    day, named = where_to(pt, title, minutes_for(a, title, task))
    try:
        set_aside(a, title, task, day, named)
    except Exception:
        return "off today's list" if line is not None else ""
    when = when_words(pt, day, named)
    head = "off today's list; " if line is not None else ""
    return head + (f"it stays on its own list and comes back {when}" if homed or task is not None
                   else f"it waits for {when}")


__all__ = ["named_window", "later_day", "where_to", "tomorrow_unasked", "ends_when_skipped", "set_aside",
           "minutes_for", "day_words", "named_skip", "week_end", "home_task",
           "skipped_off", "when_words", "moved_day",
           "window_unasked"]
