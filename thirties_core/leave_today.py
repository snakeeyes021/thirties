"""An item leaving today takes its copy off the Today note: skipped for today, moved to another day, or taken off.

Skipping an item or moving it to another day used to change only the plan. Its line stayed on the Today note, so
the next schedule showed it again under "on today's list, no time yet", and the If there's time tool refused it
because it wasn't under that heading. Now, once the plan says an item isn't happening today, its open line on the
Today note goes too, and nothing is ever ticked:

- an item with a home list loses only its copy on Today; the line on its home list is untouched;
- an item that lives only on the Today note follows the rule for its kind of leaving:
  - moved to a later day's plan: that day's morning puts it back on Today (day_lists.onto_today), so the line
    just goes;
  - put on a later day's list with no time: it waits for that day's list (day_lists.AheadLists);
  - skipped for today: a to-do waits for a later day this week (never tomorrow unless the user names it), and a
    meal, routine step or appointment simply ends (not_today.py);
  - dropped (taken off on the user's clear ask): filed in the month's Dropped note (off_today.take_off_list).

A routine's group on Today stays (its steps come and go with the routine), and so does a line while another planned
copy of the item is still ahead today.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# the user saying an item isn't for today: skip it, not today, push it to another day
SKIP_ASK_RE = re.compile(
    r"\b(?:skip\w*|not today|not (?:doing|going to do|gonna do|getting to)|won'?t (?:do|get to|be doing|have time)|"
    r"can'?t (?:do|get to)|forget (?:about )?(?:it|that|them|the)|leave (?:it|that|them|the)\b[^.!?\n]{0,30}?\bfor "
    r"(?:tomorrow|another day|later this week|next week)|off (?:today|the day|today'?s list))\b", re.IGNORECASE)
# a word that takes it off one day for another (a later day's name alone could mean "on that day too")
MOVE_OFF_RE = re.compile(r"\b(?:move\w*|push\w*|bump\w*|shift\w*|postpone\w*|put (?:it |that |them )?off|"
                         r"defer\w*|reschedul\w*|instead|rather)\b", re.IGNORECASE)


def asks_to_skip(message: Optional[str]) -> bool:
    """The message says something isn't happening today (skip it, not today, forget it)."""
    return bool(message and SKIP_ASK_RE.search(message))


def _assistant(pt: "PlanTools") -> Any:
    return getattr(pt, "assistant", None)


def _routine_names(a: Any) -> set:
    try:
        return {n["title"].strip().lower() for n in a._routines()}
    except Exception:
        return set()


def top_line(pt: "PlanTools", title: str, task_id: Optional[str] = None) -> Optional[str]:
    """The item's open top-level line on today's Today note (not a routine's group, nor a step inside one), as
    written there; None when it has none."""
    a = _assistant(pt)
    if a is None or getattr(a, "_db", None) is None:
        return None
    try:
        note = a._today_note()
    except Exception:
        return None
    if note is None:
        return None
    from thirties_core.joplin_engine import CHECKBOX_REGEX
    from thirties_core.today import TodayLinks
    tops = []
    for line in (note.get("body") or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        if m and not m.group(1):
            tops.append(m.group(2).strip())
    wanted = [title.strip().lower()]
    if task_id:
        linked = TodayLinks(a._db).today_title_of(task_id)
        if linked:
            wanted.insert(0, linked.strip().lower())
        home = next((t for t in getattr(a.s, "tasks", []) if t.id == task_id), None)
        if home is not None:
            wanted.append(home.title.strip().lower())
    routines = _routine_names(a)
    for w in wanted:
        line = next((t for t in tops if t.strip().lower() == w), None)
        if line is not None and line.strip().lower() not in routines:
            return line
    return None


def has_home(a: Any, line: str) -> bool:
    """The line on Today is a copy of an item on one of the user's lists."""
    from thirties_core.done_log import FILED_ROLES
    from thirties_core.today import TodayLinks
    return TodayLinks(a._db).home_of(line) is not None or any(
        x.title.strip().lower() == line.strip().lower() and x.list_role in FILED_ROLES for x in a.s.tasks)


def remove_copy(a: Any, line: str) -> bool:
    """Take the line off the Today note, and only there: its home list (if any) is never written to. The link
    between the two goes with it. True when the removal was sent."""
    return not remove_copy_why(a, line)


def remove_copy_why(a: Any, line: str) -> str:
    """remove_copy, saying why when the line couldn't be taken off (the real reason, so the model can act on it);
    "" when the removal was sent."""
    from thirties_core.today import TodayLinks
    note, writer = a._today_note(), a._writer()
    if note is None:
        return "there's no Today note to take it off"
    if writer is None or not getattr(writer, "allowed", True):
        return "Thirties isn't allowed to change Joplin (Preferences → Tasks)"
    status, why = writer.submit({"op": "remove", "note_id": note["id"], "title": line, "state": "open"})
    if status not in ("done", "queued"):
        return str(why or status)
    a.s.tasks = [x for x in a.s.tasks if not (x.parent_note_id == note["id"] and x.title == line)]
    TodayLinks(a._db).unlink(line)
    return ""


def _still_planned_today(pt: "PlanTools", span: "EventSpan") -> bool:
    from thirties_core.models import SpanStatus
    return any(s.id != span.id and s.status == SpanStatus.PLANNED and s.end_dt > pt._now()
               and (s.title.strip().lower() == span.title.strip().lower() or (span.task_id and s.task_id == span.task_id))
               for s in pt.day_plan.spans)


def _talking_or_hand(pt: "PlanTools") -> bool:
    """A change the user made: their words this turn, or the app's own controls. The background's own bookkeeping
    (nobody talking) never touches the Today note this way."""
    return getattr(pt, "user_message", None) is not None or bool(getattr(pt, "by_hand", False))


def skip_line(a: Any, line: str) -> str:
    """Take a line off today's list because it isn't happening today (not_today.skipped_off): it ends, is
    dropped on the user's drop words, or waits for a later day (with a home list, the line there is untouched).
    What happened, in a few words ("" when nothing changed)."""
    try:
        from thirties_core.not_today import skipped_off
        return skipped_off(a, line, line)
    except Exception:
        return ""


def after_skip(pt: "PlanTools", span: "EventSpan") -> str:
    """An item skipped for today leaves today's list. A note for the tool's answer ("" when nothing changed)."""
    if not _talking_or_hand(pt) or span.is_envelope or span.source == "calendar":
        return ""
    if not pt.is_current(pt.day_plan) or _still_planned_today(pt, span):
        return ""
    a = _assistant(pt)
    line = top_line(pt, span.title, span.task_id)
    if a is None:
        return ""
    from thirties_core.due_today import deadline_words, held_due  # due today or overdue: a fact, never a hold
    held = held_due(pt, line or span.title, span.task_id)
    due = f" {line or span.title} is {deadline_words(held, pt.today())}." if held is not None else ""
    return _skipped(pt, a, span, line) + due


def _skipped(pt: "PlanTools", a: Any, span: "EventSpan", line: Optional[str]) -> str:
    """after_skip once the item's facts are known: where it goes now that it isn't happening today."""
    # --- not today: it ends, it's dropped on the user's drop words, or it waits for a later day (not_today.py) ---
    from thirties_core.not_today import ends_when_skipped, skipped_off
    from thirties_core.write_guard import asks_to_drop
    if not (asks_to_drop(getattr(pt, "user_message", None)) or ends_when_skipped(a, span.title, span)):
        # --- it didn't happen (its time is past): back to its list, fitted where it makes sense (catch_up.refit) ---
        from thirties_core.catch_up import refit
        # a skip (the word, or the app's skip button) never puts it back on today; only "didn't happen" may
        message = getattr(pt, "user_message", None)
        back = refit(pt, span, line, tonight=message is not None and not asks_to_skip(message))
        if back is not None:
            return back
        # --- end didn't happen ---
    try:
        done = skipped_off(a, span.title, line, span)
    except Exception:
        done = ""
    # --- end not today ---
    return f" (also {done})" if done else ""


def after_move(pt: "PlanTools", span: "EventSpan", source_plan: Any, target_plan: Any) -> str:
    """An item moved from today's plan to a later day leaves today's list; that day's morning puts it back on Today.
    A note for the tool's answer ("" when nothing changed)."""
    if not _talking_or_hand(pt) or span.is_envelope or span.source == "calendar":
        return ""
    if not pt.is_current(source_plan) or target_plan.target_date <= source_plan.target_date:
        return ""
    saved = pt.day_plan
    pt.day_plan = source_plan
    try:
        if _still_planned_today(pt, span):
            return ""
        a = _assistant(pt)
        line = top_line(pt, span.title, span.task_id)
        if a is None or line is None:
            return ""
        homed = has_home(a, line)
        today_ids = {x.id for x in a.s.tasks if x.list_role == "today"}
        if not remove_copy(a, line):
            return ""
        if not homed and span.task_id in today_ids:
            # its id was its line on today's Today note, which is gone: that day's line is linked by its wording
            span.task_id = None
            pt._persist(span)
    except Exception:
        return ""
    finally:
        pt.day_plan = saved
    return f" (off today's list; it goes on the Today list {_day_word(pt, target_plan.target_date)} morning)"


def after_ahead(a: Any, title: str, day: date) -> str:
    """An item put on a later day's list (no time) while it's on today's: off today's list. A note ("" when it
    wasn't on today's, or the user's words keep it on both)."""
    pt = a.pt
    message = getattr(pt, "user_message", None)
    if message is not None and not (MOVE_OFF_RE.search(message) or asks_to_skip(message)):
        return ""
    if day <= pt.today():
        return ""
    task = a._exact_task(title)
    line = top_line(pt, title, task.id if task is not None else None)
    if line is None:
        return ""
    try:
        if not remove_copy(a, line):
            return ""
    except Exception:
        return ""
    from thirties_core.models import SpanStatus
    saved = pt.day_plan
    pt.day_plan = pt.plan_for(pt.today())  # the call itself is working on the later day's plan
    try:
        gone = [s for s in pt.day_plan.spans if s.status == SpanStatus.PLANNED and s.end_dt > pt._now()
                and (s.title.strip().lower() == line.strip().lower() or (task is not None and s.task_id == task.id))]
        for s in gone:
            pt._drop(s)  # it isn't on today's plan either
        if gone:
            pt._save_plan_after_change()
    finally:
        pt.day_plan = saved
    return f"{line} is off today's list."


def _day_word(pt: "PlanTools", day: date) -> str:
    gap = (day - pt.today()).days
    return "tomorrow" if gap == 1 else f"on {day.strftime('%A')}" if gap < 7 else f"on {day.strftime('%b %-d')}"


__all__ = ["asks_to_skip", "skip_line", "top_line", "has_home", "remove_copy", "remove_copy_why", "after_skip", "after_move", "after_ahead"]
