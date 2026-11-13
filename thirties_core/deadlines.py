"""Deadlines that come round again: a missed one moves to the next, and its work is planned back from there.

Some deadlines repeat: a window every quarter, a renewal every year. The user says so (set_item_facts with
``repeats``: monthly, quarterly or yearly), or the item's own text marks it ("(quarterly)"), and the item keeps
it; Python never guesses it from the rest of the wording. Unknown, a missed deadline is like any other: the item
goes to Before today.

When such a deadline passes with the item still open, it doesn't end and isn't dropped. Python moves its deadline
to the same point in the next period (further on if more than one has passed) and records the miss. Then the
item's remaining work becomes a budget, planned back from the deadline through the trickle-down (budgets.py):

- how much work: what the user said ("about 12 hours"), else the model's estimate, which is judgement, stored on
  the item as an estimate, shown, and correctable; until there is one, one session holds the place and the
  morning asks for an estimate. Work already done on it since the last deadline counts off;
- each session's length: what the user said, else the item's history (its typical session), else an hour;
- when it starts: the lead the user said, else now (work is pulled forward, never left to the end);
- a buffer before the deadline: a few days, more for each deadline of this item missed before;
- how dense: the sessions spread over the time left; when there are more than days, they get longer.

The morning says where it went, so the user can correct it.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
BUFFER_DAYS = 3          # done this long before the deadline, plus BUFFER_PER_MISS for each one of its missed before
BUFFER_PER_MISS = 2
BUFFER_MAX = 14
SESSION_MINUTES = 60     # a session, when neither the user nor the item's history says how long
LONGEST_SESSION = 240    # when the work can't fit a session a day, sessions grow to at most this
MISSED_KEY = "deadlines_missed"   # sync meta: [{"item", "title", "due", "noticed"}], for the statistics
MARK_RE = re.compile(r"\((monthly|quarterly|yearly|annually|every (month|quarter|year))\)", re.IGNORECASE)
_MARKS = {"monthly": "monthly", "quarterly": "quarterly", "yearly": "yearly", "annually": "yearly",
          "every month": "monthly", "every quarter": "quarterly", "every year": "yearly"}


def marked_repeat(title: str) -> str:
    """A repeat the item's own text states, "(quarterly)"; "" otherwise."""
    m = MARK_RE.search(title or "")
    return _MARKS.get(m.group(1).lower(), "") if m else ""


END = 31  # a series' day: "the month's last day"


def series_day(d: date) -> int:
    """The day a series of deadlines keeps, from its own first date: that day of the month, or END when that date
    is the month's last day (a quarter's end, Sep 30, is followed by Dec 31)."""
    return END if d.day == calendar.monthrange(d.year, d.month)[1] else d.day


def add_months(d: date, months: int, day: Optional[int] = None) -> date:
    """The series' day (``day``; else ``d``'s own, series_day) ``months`` later, kept within the month: Jan 30
    + 1 month is Feb 28, and with day 30 the month after is Mar 30, never drifting to the month's end."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    want = series_day(d) if day is None else day
    return date(y, m, last if want == END else min(want, last))


def next_deadline(due: date, repeats: str, after: date, day: Optional[int] = None) -> Optional[date]:
    """The first deadline of the series after ``after``; ``day``: the series' own day (series_day of its first
    date), so a deadline clamped into a short month doesn't move the ones after it."""
    step = STEP_MONTHS.get(repeats)
    if not step:
        return None
    n, nxt = 1, add_months(due, step, day)
    while nxt <= after:
        n += 1
        nxt = add_months(due, step * n, day)
    return nxt


SERIES_DAY_KEY = "deadline_day:{}"  # sync meta, by item id: the day its series keeps (from its first deadline)


def day_of_series(db, task_id: str, due: date) -> int:
    """The series' day: kept from the first deadline that rolled (the user's own date), never from a clamped one."""
    raw = db.get_sync_meta(SERIES_DAY_KEY.format(task_id)) if db is not None else None
    if raw and raw.isdigit():
        return int(raw)
    day = series_day(due)
    if db is not None:
        db.set_sync_meta(SERIES_DAY_KEY.format(task_id), str(day))
    return day


def restart_series(db, task_id: str, due: Optional[date]) -> None:
    """The user set a new deadline: its series keeps the new date's day from now on, not the old one's."""
    if db is not None and due is not None:
        db.set_sync_meta(SERIES_DAY_KEY.format(task_id), str(series_day(due)))


def repeats_of(db, title: str) -> str:
    """How an item's deadline comes round: what the user said, else its own text's mark; "" if it doesn't."""
    from thirties_core.item_facts import LastingFacts
    return LastingFacts(db).for_item(title).repeats or marked_repeat(title)


def misses_of(db, task_id: str) -> int:
    """How many of this item's deadlines were missed before (recorded by roll_missed)."""
    try:
        return sum(1 for m in json.loads(db.get_sync_meta(MISSED_KEY) or "[]") if m.get("item") == task_id)
    except (ValueError, TypeError):
        return 0


def buffer_days(db, task_id: str) -> int:
    """Days before the deadline the work should be done: a few, more for an item that has slipped before."""
    return min(BUFFER_MAX, BUFFER_DAYS + BUFFER_PER_MISS * misses_of(db, task_id))


def work_by(db, task) -> Optional[date]:
    """The day the work on a repeating deadline should be done by: its deadline less the buffer. None without a
    deadline or a repeat (then the deadline itself is the limit, budgets.py)."""
    try:
        due = date.fromisoformat(task.due[:10]) if getattr(task, "due", "") else None
    except ValueError:
        return None
    if due is None or db is None or not repeats_of(db, task.title):
        return None
    return due - timedelta(days=buffer_days(db, task.id))


def parse_lead_days(value) -> int:
    """'a month', '2 weeks', '10 days' as days."""
    raw = str(value).strip().lower()
    m = re.fullmatch(r"(?:about\s+)?(a|an|one|\d+)\s*(day|week|month|year)s?(?:\s+before)?", raw)
    if not m:
        raise ValueError(f"Could not read '{value}' as a lead; use something like '2 weeks' or 'a month'.")
    n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
    return n * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]


def _done_minutes(pt, title: str, start: date, end: date) -> int:
    """Minutes of ``title`` done on the days start..end."""
    from thirties_core.budgets import _same
    from thirties_core.models import SpanStatus
    try:
        a, b = pt.plan_for(start).day_start, pt.plan_for(end).day_end
        spans = pt.scheduler.state_db.get_spans_between(a, b)
    except Exception:
        return 0
    return sum(s.duration_minutes for s in spans if _same(s.title, title) and s.status == SpanStatus.DONE)


def plan_back(assistant, task, due: date, today: date, repeats: str) -> Dict[str, Any]:
    """Plan an item's remaining work back from its deadline, as a budget the trickle-down spreads (budgets.py).
    Returns what was planned: {"minutes" (left), "sessions", "length", "by", "from", "source"} (source: 'user',
    'estimate' or '' when nobody has said yet: then one session holds the place)."""
    import math
    from thirties_core.horizons import HorizonPlanner, period_of
    from thirties_core.item_facts import LastingFacts
    db = assistant._db
    facts = LastingFacts(db).for_item(task.title)
    typical = assistant.typical_duration(task.title) if assistant.pt.scheduler else None
    length = facts.minutes or (max(30, int(typical)) if typical else 0) or SESSION_MINUTES
    by = max(today, due - timedelta(days=buffer_days(db, task.id)))
    begin = max(today, by - timedelta(days=facts.lead_days)) if facts.lead_days else today
    if facts.effort:
        cycle = add_months(due, -STEP_MONTHS.get(repeats, 3))  # this deadline's period: work since the last one counts
        left = max(0, facts.effort - _done_minutes(assistant.pt, task.title, min(cycle, today), today))
    else:
        left = length  # nothing said or estimated yet: one session holds the place
    days = (by - begin).days + 1
    sessions = max(1, math.ceil(left / length)) if left else 0
    if sessions > days:  # more work than days: denser sessions, then as many days as it needs
        length = min(LONGEST_SESSION, max(length, int(math.ceil(left / days / 30.0) * 30)))
        sessions = math.ceil(left / length)
    planner = HorizonPlanner(assistant.pt)
    level = next((lv for lv in ("week", "month", "quarter", "year")
                  if period_of(lv, begin, planner.week_start).end >= by), "year")
    if sessions:
        planner.pin(task, period_of(level, begin, planner.week_start), sessions, length)
    return {"minutes": left, "sessions": sessions, "length": length, "by": by.isoformat(),
            "from": begin.isoformat(), "source": facts.effort_source if facts.effort else ""}


def replan_item(assistant, title: str) -> str:
    """After its effort or lead is said: an open item with a repeating deadline ahead is planned afresh. Returns a
    short note for the tool result ("" if there's nothing to plan)."""
    from thirties_core.item_facts import item_name
    db = assistant._db
    if db is None:
        return ""
    today = assistant._today()
    for t in assistant.s.tasks:
        if item_name(t.title) != item_name(title) or not t.due or t.someday:
            continue
        repeats = repeats_of(db, t.title)
        try:
            due = date.fromisoformat(t.due[:10])
        except ValueError:
            continue
        if not repeats or due < today:
            continue
        got = plan_back(assistant, t, due, today, repeats)
        return work_line(t.title, due, got)
    return ""


def work_line(title: str, due: date, got: Dict[str, Any], short=lambda t: t) -> str:
    """What was planned, plainly: "Permit renewal: next deadline Dec 31; 6 sessions of 2 hours before Dec 28."."""
    from thirties_core.clock import format_duration
    by = date.fromisoformat(got["by"])
    head = f"{short(title)}: next deadline {due.strftime('%b %-d')}"
    if not got["sessions"]:
        return head + "; its work is already done."
    n = got["sessions"]
    work = f"{n} session{'s' if n != 1 else ''} of {format_duration(got['length'])} before {by.strftime('%b %-d')}"
    if not got["source"]:
        return head + f"; {work} for now, until it's known how much work it needs."
    return head + f"; {work}" + (" (from an estimate)." if got["source"] == "estimate" else ".")


def roll_missed(assistant, day: date) -> List[Dict[str, Any]]:
    """Every open item whose repeating deadline passed before ``day``: its next deadline set and its remaining work
    planned back from it. Returns what moved, for the morning: {"title", "due", "plan"} (plan_back's result)."""
    from thirties_core.lists import URGENCIES, ListCatalog
    db = assistant._db
    if db is None:
        return []
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
        repeats = repeats_of(db, t.title)
        nxt = next_deadline(due, repeats, day - timedelta(days=1), day_of_series(db, t.id, due)) if repeats else None
        if nxt is None:
            continue
        try:
            note = t.parent_note_id if t.is_checkbox_item else t.id
            urgency = t.urgency if t.urgency in URGENCIES and t.urgency != "someday" else "soon"
            ListCatalog(db).set_urgency(t.id, urgency, due=nxt, note_id=note or "", title=t.title, source="user")
            t.due = nxt.isoformat()
            _record_miss(db, t, due, day)
            got = plan_back(assistant, t, nxt, day, repeats)
        except Exception as e:  # one item's trouble never stops the morning
            logger.warning("Couldn't move %s to its next deadline: %s", t.title, e)
            continue
        out.append({"title": t.title, "due": nxt.isoformat(), "plan": got})
    return out


def _record_miss(db, task, due: date, noticed: date) -> None:
    """The miss, for the statistics: put off once more, and the deadline that passed."""
    try:
        db.record_task_deferred(task.id)
        missed = json.loads(db.get_sync_meta(MISSED_KEY) or "[]")
        missed.append({"item": task.id, "title": task.title, "due": due.isoformat(), "noticed": noticed.isoformat()})
        db.set_sync_meta(MISSED_KEY, json.dumps(missed[-200:]))
    except Exception as e:
        logger.warning("Couldn't record the missed deadline of %s: %s", task.title, e)


def moved_text(moved: List[Dict[str, Any]], today: date, week_start: int = 0, short=lambda t: t) -> str:
    """The morning's word on them, one line each (work_line). Asking the model for an estimate is separate
    (estimates_wanted): this text can reach the user."""
    return "\n".join(work_line(m["title"], date.fromisoformat(m["due"]), m["plan"], short) for m in moved)


def estimates_wanted(assistant) -> List[str]:
    """Open items with a repeating deadline ahead whose work nobody has sized yet: the model is asked to estimate."""
    from thirties_core.item_facts import LastingFacts
    db = assistant._db
    if db is None:
        return []
    facts, today, out = LastingFacts(db), assistant._today(), []
    for t in assistant.s.tasks:
        if not t.due or t.someday or not repeats_of(db, t.title) or facts.for_item(t.title).effort:
            continue
        try:
            if date.fromisoformat(t.due[:10]) >= today:
                out.append(t.title)
        except ValueError:
            continue
    return out


__all__ = ["add_months", "next_deadline", "roll_missed", "moved_text", "plan_back", "replan_item", "work_by",
           "work_line", "estimates_wanted", "buffer_days", "parse_lead_days", "repeats_of", "STEP_MONTHS"]
