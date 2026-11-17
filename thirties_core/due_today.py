"""An item due today, or overdue and not done, never leaves today without a word.

Every way off today checks the item's due date (the one it already carries: its list item's due date, kept with
its urgency in lists.ListCatalog). An overdue item (due before today) is held the same way, unless it was done:

- Thirties' own planning never moves a due-today item off today: a redraw with no room keeps it on today's plan,
  an on-site day with no free time keeps it where it is, and a skip leaves it on today's list;
- a vague word from the user ("not today", "later this week", "sometime", a skip) keeps it on today, and the answer
  says it's due today;
- a clear request that names the item and a day ("move the tax form to tomorrow") moves it, and the answer names
  the deadline: "<item> was due today; it's on tomorrow now." For an overdue item the answers say "<item> is
  overdue (it was due Friday), so it stays on today" and "<item> was overdue; it's on tomorrow now."

Only the user's own words count as clear. The app's own controls (a drag, a button) are the user's doing too.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

# a day named outright (a later day the item can go to)
CLEAR_DAY_RE = re.compile(
    r"\b(?:tomorrow|tmrw|tmr|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next (?:week|month)|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w* \d{1,2}|\d{1,2}/\d{1,2}|"
    r"the \d{1,2}(?:st|nd|rd|th))\b", re.IGNORECASE)


def _as_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def due_on(a: Any, title: str, task_id: Optional[str] = None) -> Optional[date]:
    """The item's due date, from what it already carries: its list item (by id, by its Today copy's link, or by
    its exact wording), else the due date kept for an item of that wording."""
    if a is None:
        return None
    tasks = list(getattr(getattr(a, "s", None), "tasks", []) or [])
    found = [t for t in tasks if task_id and t.id == task_id]
    db = getattr(a, "_db", None)
    if db is not None and title:
        from thirties_core.today import TodayLinks
        link = TodayLinks(db).home_of(title)
        if link:
            found += [t for t in tasks if t.id == link["home_id"]]
    if title:
        try:
            exact = a._exact_task(title)
        except Exception:
            exact = None
        if exact is not None:
            found.append(exact)
    for t in found:
        d = _as_date(getattr(t, "due", ""))
        if d is not None:
            return d
    if db is not None and title:
        from thirties_core.lists import ListCatalog
        low = title.strip().lower()
        for r in ListCatalog(db).urgencies().values():
            if (r.get("title") or "").strip().lower() == low and _as_date(r.get("due")):
                return _as_date(r.get("due"))
    return None


def is_due_today(pt: "PlanTools", title: str, task_id: Optional[str] = None, due: Any = None) -> bool:
    """The item is due on the day being lived (``due``: a due date already in hand, e.g. a leftover's)."""
    d = _as_date(due) or due_on(getattr(pt, "assistant", None), title, task_id)
    return d is not None and d == pt.today()


def _done_since(pt: "PlanTools", title: str, task_id: Optional[str], since: date) -> bool:
    """The item was done on or after ``since``: ticked on today's plan, or its list item marked done."""
    low = (title or "").strip().lower()
    plan = getattr(pt, "day_plan", None)
    from thirties_core.models import SpanStatus
    for s in (getattr(plan, "spans", None) or []):
        if s.status == SpanStatus.DONE and ((task_id and s.task_id == task_id) or s.title.strip().lower() == low):
            return True
    db = getattr(getattr(pt, "scheduler", None), "state_db", None)
    if task_id and db is not None:
        try:
            rec = db.get_task_history(task_id)
        except Exception:
            rec = None
        done = _as_date(rec.completed_date) if rec is not None else None
        if done is not None and done >= since:
            return True
    return False


def held_due(pt: "PlanTools", title: str, task_id: Optional[str] = None, due: Any = None) -> Optional[date]:
    """The due date that holds the item on today: due today, or overdue (due before today) and not done since.
    None when nothing holds it."""
    d = _as_date(due) or due_on(getattr(pt, "assistant", None), title, task_id)
    today = pt.today()
    if d is None or d > today:
        return None
    if d < today and _done_since(pt, title, task_id, d):
        return None
    return d


def must_stay(pt: "PlanTools", title: str, task_id: Optional[str] = None, due: Any = None) -> bool:
    """The item is due today or overdue (held_due): it leaves today only on the user's clear word."""
    return held_due(pt, title, task_id, due) is not None


def _day_words(day: date, today: date) -> str:
    """A past due date as the user reads it: the weekday within the last week, else the date."""
    return day.strftime("%A") if 0 < (today - day).days < 7 else day.strftime("%b %-d")


def deadline_words(due: Optional[date], today: date) -> str:
    """"due today", or "overdue (it was due Friday)" for a due date gone by."""
    if due is None or due >= today:
        return "due today"
    return f"overdue (it was due {_day_words(due, today)})"


def list_stays_note(title: str, due: Optional[date] = None, today: Optional[date] = None) -> str:
    """Added to an answer when a skip or a vague word leaves a held item on today's list."""
    if due is None or today is None or due >= today:
        return f" ({title} is due today, so it stays on today's list)"
    return f" {title} is {deadline_words(due, today)}, so it stays on today's list."


def clear_move(pt: "PlanTools", title: str, others: Iterable[str] = ()) -> bool:
    """The user asked outright for this item to go to another day: their message names it and names a day. The
    app's own controls and a call with nobody talking (a script) count as asked."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return True
    from thirties_core.item_facts import mentions
    return bool(CLEAR_DAY_RE.search(message)) and mentions(message, title, list(others))


def clear_ask(pt: "PlanTools", title: str, others: Iterable[str] = ()) -> bool:
    """The user asked outright for this item to come off (drop it, remove it), naming it."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return True
    from thirties_core.item_facts import mentions
    from thirties_core.off_today import asks_to_clear
    return asks_to_clear(message, title) and mentions(message, title, list(others))


def stays_line(title: str, due: Optional[date] = None, today: Optional[date] = None) -> str:
    """Said when a vague word would have taken a due-today (or overdue: ``due`` before ``today``) item off today:
    it stays."""
    words = deadline_words(due, today) if today is not None else "due today"
    return (f"{title} is {words}, so it stays on today. If it should still go to another day, the user can name "
            "the day.")


def moved_line(title: str, day: Optional[date], today: date, due: Optional[date] = None) -> str:
    """Said when a due-today (or overdue: ``due`` before ``today``) item goes to another day on the user's clear
    word."""
    was = "overdue" if due is not None and due < today else "due today"
    if day is None:
        return f"{title} was {was}; it's off today now."
    gap = (day - today).days
    when = "tomorrow" if gap == 1 else day.strftime("%A") if 1 < gap < 7 else day.strftime("%b %-d")
    return f"{title} was {was}; it's on {when} now."


def move_check(pt: "PlanTools", title: str, task_id: Optional[str], day: Optional[date], due: Any = None) -> str:
    """Before a due-today or overdue item goes from today to ``day`` (None: off today with no day, like someday):
    refused with the reason when the user's words aren't a clear request for it; else the line naming the
    deadline, to add to the answer (with a leading space). "" when nothing holds it or it stays today."""
    today = pt.today()
    if day is not None and day <= today:
        return ""
    held = held_due(pt, title, task_id, due)
    if held is None:
        return ""
    if not clear_move(pt, title, others_on(pt, title)):
        from thirties_core.plan_tools import ToolError
        raise ToolError(stays_line(title, held, today))
    return " " + moved_line(title, day, today, held)


def window_check(pt: "PlanTools", title: str, task_id: Optional[str], window: dict, due: Any = None) -> str:
    """move_check for a leftover placed at a natural horizon ({"when", "start", "end"}, carry.resolve_when): a
    window that ends today keeps it today."""
    today = pt.today()
    if window.get("end") == today.isoformat():
        return ""
    start = _as_date(window.get("start"))
    if start is not None and start <= today:  # a window from today on ("this week"): it leaves today all the same
        start = date.fromordinal(today.toordinal() + 1)
    return move_check(pt, title, task_id, start, due)


def others_on(pt: "PlanTools", title: str) -> list:
    """The other items' names on today's plan, so a message naming one isn't read as naming another."""
    return [s.title for s in pt.day_plan.spans if s.title.strip().lower() != title.strip().lower()]


__all__ = ["CLEAR_DAY_RE", "due_on", "is_due_today", "held_due", "must_stay", "deadline_words", "list_stays_note",
           "clear_move", "clear_ask", "stays_line", "moved_line", "others_on"]
