"""What the day pages offer to do with a tap, decided without a display (src/views/agenda.py and timeline.py draw it).

- **Work started, work finished**: one button, for today's Work. Before work has been said to start (and from
  an hour before its planned start), "Started work"; once started, or once its time is over with no start said,
  "Finished work"; once finished, nothing. A tap
  records the time through log_day_times, the same tool the assistant uses for "started work at 9:40".
- **Pencilled in**: what's on a later day's list with no time yet can be done early, given a time on that
  day, moved to the day after, or dropped from that day's list.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.clock import format_clock
from thirties_core.models import DayPlan, EventSpan, SpanStatus

START_EARLY = timedelta(hours=1)    # "Started work" shows from this long before Work's planned start
END_LATE = timedelta(hours=3)       # "Finished work" stays this long after its planned end, if not said yet
RELIABLE = ("reported", "checkin", "tracker")  # day_times.RELIABLE: times the user gave, not guessed

WORK_START, WORK_END = "start", "end"
WORK_LABELS = {WORK_START: "Started work", WORK_END: "Finished work"}


def _work(plan: DayPlan) -> Optional[EventSpan]:
    """The Work log_day_times records on (day_times.work_span), unless it was skipped (no work that day)."""
    from thirties_core.day_times import work_span
    work = work_span(plan)
    return work if work is not None and work.status != SpanStatus.SKIPPED else None


def _said(span: EventSpan, end: bool) -> bool:
    value = span.actual_end if end else span.actual_start
    return value is not None and span.time_source in RELIABLE


def work_step(plan: DayPlan, now: Optional[datetime]) -> Optional[Tuple[str, EventSpan]]:
    """Which work button today shows now, with the Work it's about: (WORK_START or WORK_END, span), or None (not
    today, no Work, too early, or both already said)."""
    work = _work(plan)
    if now is None or work is None:
        return None
    planned_start = work.planned_start or work.start_dt
    planned_end = work.planned_end or work.end_dt
    end = max(planned_end, work.end_dt)
    if _said(work, end=True):
        return None
    if not _said(work, end=False) and planned_start - START_EARLY <= now < end:
        return WORK_START, work
    if (_said(work, end=False) or now >= end) and now < end + END_LATE:
        return WORK_END, work  # once started, or past its end with no start said: finishing is still worth saying
    return None


def work_call(step: str, now: datetime) -> Tuple[str, Dict[str, Any]]:
    """The tool call a work button makes: the time it was tapped, as work's real start or end."""
    key = "work_started" if step == WORK_START else "work_ended"
    return "log_day_times", {key: format_clock(now)}


def work_reply(result: str) -> Tuple[str, str]:
    """log_day_times' result after a work button, split for the window: (what was recorded, for a short notice;
    the rest: how the day follows work, and after work the thirties left and a few things that fit, for the chat).
    "Recorded: finished work 4:35 PM. After work: ..." -> ("Finished work 4:35 PM", "After work: ...")."""
    text = (result or "").strip()
    head, sep, rest = text.partition(". ")
    if not sep:
        head, rest = text.rstrip("."), ""
    head = head.removeprefix("Recorded: ").strip()
    return (head[:1].upper() + head[1:]) if head else "", rest.strip()


def work_line(work: EventSpan) -> str:
    """The Work row's quiet line: its times, and what's been said."""
    start = work.actual_start if _said(work, end=False) else None
    if start is not None:
        return f"Started {format_clock(start)}"
    return f"{format_clock(work.start_dt)} – {format_clock(work.end_dt)}"


# --- effort: the user's own estimate per item, in the unit they pick (efforts.py keeps it) ----------------------

ESTIMATE_UNITS = ("thirties", "hours", "minutes", "days", "points")  # efforts.UNITS, blocks first as the app says


def estimate_text(amount: float, unit: str) -> str:
    """What set_estimate takes: '3 thirties', '1.5 hours', '5 points'."""
    n = max(0.5, float(amount))
    return f"{n:g} {unit if unit in ESTIMATE_UNITS else 'hours'}"


def estimate_calls(task: str, amount: float, unit: str, usual_unit: str) -> List[Tuple[str, Dict[str, Any]]]:
    """The tool calls a saved estimate makes: the unit picked becomes the usual one if it isn't (set_effort_unit),
    then the estimate itself, kept for the item by its id (set_estimate)."""
    calls: List[Tuple[str, Dict[str, Any]]] = []
    if unit != usual_unit:
        calls.append(("set_effort_unit", {"unit": unit}))
    calls.append(("set_estimate", {"task": task, "estimate": estimate_text(amount, unit)}))
    return calls


def shown_estimate(rec: Optional[Dict[str, Any]], usual_unit: str) -> Tuple[float, str]:
    """An estimate kept (efforts.estimate_for: {"amount", "unit", ...}) as (amount, unit) for the editor; none
    kept: (0, the usual unit)."""
    if rec and rec.get("unit") in ESTIMATE_UNITS and float(rec.get("amount") or 0) > 0:
        return float(rec["amount"]), rec["unit"]
    return 0.0, usual_unit if usual_unit in ESTIMATE_UNITS else "hours"


# --- pencilled in: a later day's list, no time yet ---------------------------------------------------------------

PENCIL_DONE, PENCIL_SCHEDULE, PENCIL_NEXT_DAY, PENCIL_DROP = "done", "schedule", "next_day", "drop"


def first_free_start(layout) -> Optional[datetime]:
    """Where "Schedule it" puts an item on a later day: the start of its first free thirty (Thirties picks block
    times, never odd minutes). ``layout`` is that day's timeline_layout.DayLayout."""
    free = [f for f in getattr(layout, "free", []) if f.kind == "free"]
    return free[0].start if free else None


def pencil_act(assistant, day: date, title: str, action: str, layout=None) -> str:
    """One action on an item pencilled in for ``day`` (a later day's list, day_lists.AheadLists): done early (its
    home item ticked through the same guarded Joplin path as a tick on Today), schedule (add_event at the day's
    first free thirty, on the tools of that day's plan), the day after, or drop from that day's list. Returns
    what's said to the user."""
    from thirties_core.day_lists import AheadLists
    db = assistant._db
    if db is None:
        return ""
    store = AheadLists(db)
    row = next((r for r in store.for_day(day) if r["title"].strip().lower() == title.strip().lower()), None)
    if row is None:
        return f"{title} isn't on {day.strftime('%A')}'s list."
    title = row["title"]
    if action == PENCIL_SCHEDULE:
        start = first_free_start(layout) if layout is not None else None
        if start is None:
            return f"There's no free time on {day.strftime('%A')} for {title}; it stays on the list."
        args: Dict[str, Any] = {"title": title, "start": format_clock(start), "duration": "30 minutes"}
        if row.get("home_id"):
            args.update(task_id=row["home_id"], category="task")
        out = assistant.pt.execute("add_event", args)
        if out.startswith("NOT_DONE"):
            return out
        store.set_state(day, title, "scheduled")
        return f"{title} is on {day.strftime('%A')}'s plan at {format_clock(start)}."
    if action == PENCIL_NEXT_DAY:
        later = day + timedelta(days=1)
        home = next((t for t in assistant.s.tasks if row.get("home_id") and t.id == row["home_id"]), None)
        store.add(later, title, home)
        store.set_state(day, title, "moved")
        return f"{title} is on {later.strftime('%A')}'s list instead."
    if action == PENCIL_DROP:
        store.set_state(day, title, "dropped")
        return f"{title} is off {day.strftime('%A')}'s list."
    if action == PENCIL_DONE:
        _tick_home(assistant, row)
        store.set_state(day, title, "done")
        return f"Done: {title}."
    return ""


def _tick_home(assistant, row: Dict[str, str]) -> None:
    """Done early: its home item (by its exact id, never a fuzzy match) is ticked in Joplin through the writer, as
    a tick on Today ticks it. Never a routine (a template), a wishlist item, or something ongoing (a session done
    isn't the thing finished)."""
    from thirties_core.joplin_writeback import complete_op
    from thirties_core.kinds import ONGOING
    home_id = row.get("home_id") or ""
    home = next((t for t in assistant.s.tasks if home_id and t.id == home_id), None)
    if home is None:
        return
    assistant._db.record_task_completed(home.id, assistant._today())
    if home.id.startswith("media:") or home.list_role == "routine" or assistant.kind_of(home) == ONGOING:
        return
    writer = assistant._writer()
    if writer is not None:
        writer.submit(complete_op(home))


__all__ = ["work_reply", "ESTIMATE_UNITS", "estimate_text", "estimate_calls", "shown_estimate", "work_step", "work_call", "work_line", "WORK_LABELS",
           "WORK_START", "WORK_END", "pencil_act", "first_free_start", "PENCIL_DONE", "PENCIL_SCHEDULE",
           "PENCIL_NEXT_DAY", "PENCIL_DROP"]
