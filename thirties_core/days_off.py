"""Days off: "I'm off this week", "off until Monday", "back at work on the 20th".

Work is planned from the user's week (week.py: work days and hours) every day it's a work day. A stretch of days off
the user names is kept here, as ranges of dates, synced with the other shared settings: on each of those days the
default Work doesn't go on the plan (and comes off if it was already there), leaving the whole day free (11-11 to
11-13: Work was put on three days running while the user was off, and had to be taken off each morning). Work the
user puts on by hand on such a day stays: they said they're working after all. Ending the stretch early ("I'm back
at work tomorrow") puts the default Work back on the days that are work days again.

Routines that run only on work days (or only on days off) read ``works_on`` for a date, so on a day off the work-day
ones stay off and the day-off ones come on.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

KEY = "days_off"  # in shared_settings: synced, last write wins

DAYS_OFF_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "set_days_off",
    "description": "Days the user is off work (\"I'm off this week\", \"off until Monday\", \"no work through the "
                   "20th\"): no Work goes on those days, leaving the whole day free. Give through (the last day off) or "
                   "back (the first day back at work); 'this week' and 'next week' are understood. back_to_work=true "
                   "ends days off early from start (\"I'm back tomorrow\"). Leave everything out to list them.",
    "parameters": {"start": "The first day off (default: today).",
                   "through": "The last day off, e.g. 'Friday', 'Nov 20', 'this week'.",
                   "back": "The first day back at work, e.g. 'Monday'.",
                   "back_to_work": "true to end the days off from start on.",
                   "why": "A word on why, if said ('vacation'), optional."},
    "required": [],
}]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ranges(db) -> List[Dict[str, str]]:
    """The stretches of days off, as {"start", "end", "why"} with ISO dates (end included), in order."""
    if db is None:
        return []
    try:
        data = json.loads(db.get_shared_setting(KEY) or "[]")
    except ValueError:
        return []
    out = [r for r in data if isinstance(r, dict) and r.get("start") and r.get("end")]
    return sorted(out, key=lambda r: r["start"])


def _save(db, items: List[Dict[str, str]]) -> None:
    db.set_shared_setting(KEY, json.dumps(sorted(items, key=lambda r: r["start"])), _now_iso())


def is_off(db, day: date) -> Optional[Dict[str, str]]:
    """The stretch of days off this date is in, or None."""
    iso = day.isoformat()
    return next((r for r in ranges(db) if r["start"] <= iso <= r["end"]), None)


def works_on(gen, db, day: date) -> bool:
    """A work day: one of the user's work days (week.py), and not a day off."""
    from thirties_core.week import weekday_key, work_days
    return weekday_key(day) in work_days(gen) and is_off(db, day) is None


def add(db, start: date, end: date, why: str = "") -> Dict[str, str]:
    """Days off from ``start`` through ``end``; overlapping or touching stretches become one."""
    if end < start:
        start, end = end, start
    new = {"start": start.isoformat(), "end": end.isoformat(), "why": why.strip()}
    keep = []
    for r in ranges(db):
        a, b = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        if a <= end + timedelta(days=1) and start <= b + timedelta(days=1):
            new["start"] = min(new["start"], r["start"])
            new["end"] = max(new["end"], r["end"])
            new["why"] = new["why"] or r.get("why", "")
        else:
            keep.append(r)
    _save(db, keep + [new])
    return new


def end_from(db, start: date) -> List[date]:
    """No more days off from ``start`` on (a stretch that began earlier now ends the day before). Returns the dates
    that are no longer off."""
    freed: List[date] = []
    keep = []
    for r in ranges(db):
        a, b = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        if b < start:
            keep.append(r)
            continue
        d = max(a, start)
        while d <= b:
            freed.append(d)
            d += timedelta(days=1)
        if a < start:
            keep.append({**r, "end": (start - timedelta(days=1)).isoformat()})
    _save(db, keep)
    return freed


# --- on the plan ---

def apply(scheduler, plan) -> bool:
    """On a day off, the default Work comes off the plan (and the stored day), so the day is open. Work the user put
    on by hand stays. Returns True if Work came off. Called by the day builder for every day it builds."""
    from thirties_core.models import SpanCategory
    db = scheduler.state_db
    if is_off(db, plan.target_date) is None:
        return False
    work = [s for s in plan.spans if s.category == SpanCategory.WORK and s.source == "routine"
            and s.start_dt.date() == plan.target_date]
    if not work:
        return False
    from thirties_core.plan_log import caused_by
    with caused_by("days off"):
        for s in work:
            db.delete_span(s.id)
    plan.spans = [s for s in plan.spans if s not in work]
    return True


def restore_work(scheduler, days: List[date]) -> List[date]:
    """Put the default Work back on these days, where they're work days and Work isn't on already. Returns them."""
    from thirties_core.models import SpanCategory
    from thirties_core.week import weekday_key, work_days
    db = scheduler.state_db
    back = []
    for d in days:
        if weekday_key(d) not in work_days(scheduler.config.general) or is_off(db, d) is not None:
            continue
        for span in scheduler.routine_spans(d):
            if span.category != SpanCategory.WORK:
                continue
            day_start = datetime.combine(d, datetime.min.time(), tzinfo=span.start_dt.tzinfo)
            there = [s for s in db.get_spans_between(day_start, day_start + timedelta(days=1))
                     if s.category == SpanCategory.WORK]
            if not there:
                db.upsert_span(span)
                back.append(d)
    return back


# --- what the user says ---

def _week_end(day: date) -> date:
    return day + timedelta(days=6 - day.weekday())  # through Sunday


def resolve(text: Any, today: date) -> Optional[date]:
    """A day the user named ('Friday', 'Nov 20', 'the 20th'), never in the past: a date that's gone by is next
    year's."""
    from thirties_core.clock import parse_day
    raw = re.sub(r"^(the|on|until|till|til|through|thru)\s+", "", str(text or "").strip().lower())
    if not raw:
        return None
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", raw)
    if m:  # the 20th: this month's, or next month's once it's gone by
        n = int(m.group(1))
        y, mo = today.year, today.month
        for _ in range(2):
            try:
                d = date(y, mo, n)
            except ValueError:
                d = None
            if d is not None and d >= today:
                return d
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return None
    try:
        d = parse_day(raw, today)
    except Exception:
        return None
    if d is not None and d < today:
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d


def set_days_off(tools, start: Any = None, through: Any = None, back: Any = None, back_to_work: Any = None,
                 why: Any = None, **_: Any) -> str:
    from thirties_core.assistant_tools import ToolError
    db = tools._state_db
    if db is None or tools.scheduler is None:
        raise ToolError("Days off can't be kept here.")
    today = tools.today()
    first = resolve(start, today) if start not in (None, "") else today
    if first is None:
        raise ToolError(f"'{start}' isn't a day I can read.")
    ending = str(back_to_work).strip().lower() in ("true", "yes", "1") if isinstance(back_to_work, str) \
        else bool(back_to_work)
    if ending:
        freed = end_from(db, first)
        restored = restore_work(tools.scheduler, [d for d in freed if d >= today])
        _refresh(tools, restored)
        return ("Back at work from " + _day_word(first, today) + ": Work is on the plan again"
                + (f" ({', '.join(_day_word(d, today) for d in restored)})" if restored else "") + "." if freed else
                "There were no days off from then on; nothing changed.")
    if through in (None, "") and back in (None, ""):
        if start in (None, ""):
            listed = [r for r in ranges(db) if r["end"] >= today.isoformat()]
            if not listed:
                return "No days off coming up."
            return "Days off: " + "; ".join(_describe(r, today) for r in listed) + "."
        last = first  # one day
    elif through not in (None, ""):
        word = str(through).strip().lower()
        if word in ("this week", "the week", "the rest of the week", "all week", "this whole week"):
            last = _week_end(today)
        elif word in ("next week", "all next week"):
            first = _week_end(today) + timedelta(days=1) if start in (None, "") else first
            last = _week_end(first)
        elif word in ("the weekend", "this weekend"):
            last = _week_end(today)
        else:
            last = resolve(through, today)
    else:
        back_day = resolve(back, today)
        if back_day is not None and back_day <= first:
            raise ToolError(f"Back at work on {_day_word(back_day, today)} is before the first day off.")
        last = back_day - timedelta(days=1) if back_day is not None else None
    if last is None:
        raise ToolError(f"'{through or back}' isn't a day I can read.")
    if last < first:
        raise ToolError("The last day off comes before the first.")
    rec = add(db, first, last, str(why or ""))
    off = _refresh(tools, [])
    return (f"Off {_describe(rec, today)}: no Work on those days, so they're open."
            + (" Work is off today's plan." if off else ""))


def _refresh(tools, restored: List[date]) -> bool:
    """The plan in use follows at once: Work off it on a day off, back on it when it's restored."""
    plan = tools.day_plan
    off = apply(tools.scheduler, plan)
    if plan.target_date in restored:
        fresh, _ = tools.scheduler.build_day_plan(plan.target_date)
        from thirties_core.models import SpanCategory
        for s in fresh.spans:
            if s.category == SpanCategory.WORK and plan.get_span(s.id) is None:
                plan.spans.append(s)
        plan.spans.sort(key=lambda s: s.start_dt)
    getattr(tools, "_other_days", {}).clear()  # other days are built afresh, with the change
    return off


def _day_word(d: date, today: date) -> str:
    gap = (d - today).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    if 0 < gap < 7:
        return d.strftime("%A")
    return d.strftime("%a %b %-d")


def _describe(r: Dict[str, str], today: date) -> str:
    a, b = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
    when = (_day_word(a, today) if a == b else
            f"{_day_word(max(a, today), today)} through {_day_word(b, today)}")
    return when + (f" ({r['why']})" if r.get("why") else "")


__all__ = ["DAYS_OFF_TOOL_SPECS", "ranges", "is_off", "works_on", "add", "end_from", "apply", "restore_work",
           "set_days_off", "resolve"]
