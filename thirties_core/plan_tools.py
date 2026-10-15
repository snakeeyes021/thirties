"""Shared, model-agnostic schedule tool API.

Every brain (see ``thirties_core.brains``) changes the day plan only through
``PlanTools``. Tools take natural clock times ("4:30 PM") and durations
("2 hours"); all time arithmetic, overlap checks and persistence happen here,
so no brain ever does schedule math itself (AGENTS.md §3).

Tool results are short plain-English sentences in clock time. A result that
starts with ``NOT_DONE`` means nothing was changed, and says why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.assistant_tools import (
    ASSISTANT_READ_ONLY,
    ASSISTANT_TOOL_SPECS,
    AssistantTools,
    Services,
    ToolError,
)
from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.calendar_engine import CalendarEvent
from thirties_core.clock import (
    ClockError,
    format_clock,
    format_duration,
    parse_day,
    parse_duration_minutes,
    parse_shift_minutes,
    resolve_clock,
)
from thirties_core.models import DayPlan, EventSpan, SpanCategory, new_span_id
from thirties_core.scheduler import DeterministicScheduler

logger = logging.getLogger(__name__)

NOT_DONE = "NOT_DONE"


# Tool contract shared by every brain. Times are clock strings like "4:30 PM";
# durations and shifts are phrases like "90 minutes" or "+1 hour".
TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "add_event",
        "description": "Put something on the schedule.",
        "parameters": {
            "title": "What it is, e.g. 'Composing' or 'Travel to dentist'.",
            "start": "Clock time it starts, e.g. '5:00 PM'.",
            "end": "Clock time it ends. Give either end or duration.",
            "duration": "How long, e.g. '2 hours' or '30 minutes'. Give either end or duration.",
            "category": "'activity' (default), 'task' (with task_id), 'work' or 'sleep'.",
            "task_id": "Backlog task ID when scheduling a backlog task.",
            "replace_overlaps": "true to trim or remove planned activities in the way (never calendar events).",
            "alongside": "true if it happens at the same time as something else (e.g. an audiobook during dinner).",
            "push_later": "true to make room by sliding the planned activities that follow it later, in order.",
            "day": "Which day, e.g. 'tomorrow', 'Monday', 'Oct 20' (default: the day being planned).",
        },
        "required": ["title", "start"],
    },
    {
        "name": "update_event",
        "description": "Change an existing item: new start and/or end, new duration, a shift, or a new title.",
        "parameters": {
            "event": "The item's id (or its exact title).",
            "start": "New start clock time. If no end or duration is given, the end stays where it is.",
            "end": "New end clock time. If no start is given, the start stays where it is.",
            "duration": "New length, keeping the start.",
            "shift": "Move the whole item, keeping its length: '+1 hour' is later, '-30 minutes' is earlier.",
            "title": "New title.",
            "replace_overlaps": "true to trim or remove planned activities in the way (never calendar events).",
            "push_later": "true to slide the planned activities that follow it later instead of overlapping them.",
            "to_day": "Move it to another day, e.g. 'tomorrow' (start/end are then on that day; default keeps the clock time).",
            "day": "The day the item is on (default: the day being planned).",
        },
        "required": ["event"],
    },
    {
        "name": "remove_event",
        "description": "Take an item off the schedule (including work or sleep, e.g. for a day off).",
        "parameters": {"event": "The item's id (or its exact title).", "day": "The day the item is on (default: the day being planned)."},
        "required": ["event"],
    },
    {
        "name": "swap_events",
        "description": "Swap two planned items. Back-to-back items trade places in order; others trade start times.",
        "parameters": {"first": "One item's id or title.", "second": "The other item's id or title.",
                       "day": "The day they're on (default: the day being planned)."},
        "required": ["first", "second"],
    },
    {
        "name": "clear_time",
        "description": "Free up a stretch of time by trimming or removing planned activities in it. Calendar events are kept.",
        "parameters": {
            "start": "Clock time the stretch starts.",
            "end": "Clock time the stretch ends.",
            "include_work": "true to also clear work in that stretch.",
            "include_sleep": "true to also clear sleep in that stretch.",
            "day": "Which day (default: the day being planned).",
        },
        "required": ["start", "end"],
    },
    {
        "name": "find_open_time",
        "description": "Look up free stretches of time. Read-only; you will see the result before replying.",
        "parameters": {
            "duration": "Minimum length needed, e.g. '1 hour'.",
            "after": "Earliest clock time to consider.",
            "before": "Latest clock time to consider.",
            "daylight": "true for daylight only, false for dark only.",
            "day": "Which day (default: the day being planned).",
        },
        "required": [],
    },
    {
        "name": "list_schedule",
        "description": "Show a day's schedule (any day). Read-only; you will see the result before replying.",
        "parameters": {"day": "Which day, e.g. 'tomorrow' or 'Oct 20' (default: the day being planned)."},
        "required": [],
    },
    {
        "name": "resolve_calendar_event",
        "description": "Record the user's decision on an unconfirmed calendar invite.",
        "parameters": {
            "event": "The invite's id (or its exact title).",
            "attend": "true if the user is going, false if not.",
        },
        "required": ["event", "attend"],
    },
]

SCHEDULE_TOOL_SPECS = TOOL_SPECS
TOOL_SPECS = SCHEDULE_TOOL_SPECS + ASSISTANT_TOOL_SPECS
READ_ONLY_TOOLS = {"list_schedule", "find_open_time"} | ASSISTANT_READ_ONLY


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "attend", "accept", "going")
    return bool(value)


def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class SpanState:
    title: str
    start_dt: datetime
    end_dt: datetime
    category: str
    status: str
    tentative: bool
    is_firm: bool


@dataclass
class PlanSnapshot:
    spans: Dict[str, SpanState]
    ambiguous_event_ids: set[str]
    is_finalized: bool


@dataclass
class StateDelta:
    added: List[str]
    removed: List[str]
    changed: List[str]
    resolved_events: List[str]
    finalized_changed: bool

    @property
    def total_changes(self) -> int:
        return (len(self.added) + len(self.removed) + len(self.changed)
                + len(self.resolved_events) + (1 if self.finalized_changed else 0))


class PlanTools:
    """Executes schedule tool calls against a DayPlan and persists the result."""

    def __init__(
        self,
        day_plan: DayPlan,
        ambiguous_events: List[CalendarEvent],
        scheduler: Optional[DeterministicScheduler],
        services: Optional[Services] = None,
    ) -> None:
        self.day_plan = day_plan
        self.ambiguous_events = list(ambiguous_events)
        self.scheduler = scheduler
        if services is None and scheduler is not None:
            services = Services.for_db(scheduler.state_db)
        self.services = services
        self.assistant = AssistantTools(self, services) if services else None
        self._other_days: Dict[Any, DayPlan] = {}
        self.successful_changes = 0  # every tool call that changed something, on any day or store

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _state_db(self):
        return self.scheduler.state_db if self.scheduler else None

    def _now(self) -> datetime:
        return get_current_time(self.day_plan.day_start.tzinfo)

    def today(self):
        """The diurnal date it is right now (before sunrise still counts as yesterday)."""
        gen = self.scheduler.config.general if self.scheduler else None
        if gen is None:
            return self.day_plan.target_date
        return get_diurnal_date(self._now(), lat=gen.latitude, lon=gen.longitude, tz_name=gen.timezone)

    def plan_for(self, day: Any) -> DayPlan:
        """The plan for a day given as text ('tomorrow', 'Oct 20') relative to today."""
        if day in (None, ""):
            return self.day_plan
        target = parse_day(day, self.today())
        if target == self.day_plan.target_date:
            return self.day_plan
        if self.scheduler is None:
            raise ToolError("Other days are not available here.")
        if target not in self._other_days:
            self._other_days[target] = self.scheduler.build_day_plan(target)[0]
        return self._other_days[target]

    def open_windows_for(self, plan: DayPlan):
        saved = self.day_plan
        self.day_plan = plan
        try:
            return self.open_windows()
        finally:
            self.day_plan = saved

    def _start(self, text: Any) -> datetime:
        return resolve_clock(str(text), self.day_plan.day_start)

    def _end(self, text: Any, start: datetime) -> datetime:
        return resolve_clock(str(text), self.day_plan.day_start, after=start)

    def describe(self, span: EventSpan) -> str:
        return f"{span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)} ({format_duration(span.duration_minutes)})"

    def _find_span(self, ref: Any) -> EventSpan:
        ref_str = str(ref or "").strip()
        if not ref_str:
            raise ToolError("Say which item (its id or title).")
        in_view = self.day_plan.spans_in_view()
        for span in in_view:
            if span.id == ref_str or span.id == f"cal:{ref_str}" or span.source_event_id == ref_str:
                return span
        lowered = ref_str.lower()
        exact = [s for s in in_view if s.title.lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        partial = [s for s in in_view if lowered in s.title.lower()]
        if len(partial) == 1:
            return partial[0]
        if exact or partial:
            options = "; ".join(f"{self.describe(s)} [id {s.id}]" for s in (exact or partial))
            raise ToolError(f"'{ref_str}' matches several items: {options}. Use the id.")
        raise ToolError(f"There is no item '{ref_str}' on this day's schedule.")

    def _conflicts(self, span: EventSpan) -> List[EventSpan]:
        """Active spans that this span may not overlap.

        Activities, tasks and calendar items may sit on top of work and sleep, but not on
        each other. Work and sleep only conflict with another span of the same kind.
        """
        others = [s for s in self.day_plan.spans_overlapping(span.start_dt, span.end_dt) if s.id != span.id]
        if span.is_envelope:
            return [s for s in others if s.category == span.category]
        return [s for s in others if not s.is_envelope]

    def _persist(self, span: EventSpan) -> None:
        if self._state_db and span.source != "calendar":
            self._state_db.upsert_span(span)

    def _drop(self, span: EventSpan) -> None:
        self.day_plan.spans = [s for s in self.day_plan.spans if s.id != span.id]
        if self._state_db and span.source != "calendar":
            self._state_db.delete_span(span.id)

    def _carve(self, span: EventSpan, start: datetime, end: datetime) -> str:
        """Remove [start, end) from a span: delete, trim, or split it. Returns a description."""
        before = self.describe(span)
        if start <= span.start_dt and span.end_dt <= end:
            self._drop(span)
            return f"removed {before}"
        if span.start_dt < start and end < span.end_dt:
            tail = EventSpan(
                new_span_id(), span.title, end, span.end_dt, span.category, source=span.source,
                task_id=span.task_id, status=span.status,
            )
            span.end_dt = start
            self.day_plan.spans.append(tail)
            self._persist(span)
            self._persist(tail)
            return f"split {before} around the cleared time"
        if span.start_dt < start:
            span.end_dt = start
        else:
            span.start_dt = end
        self._persist(span)
        return f"trimmed {before} to {format_clock(span.start_dt)} – {format_clock(span.end_dt)}"

    def _place(self, span: EventSpan, replace_overlaps: bool) -> List[str]:
        """Check conflicts for a new or moved span; carve overlapping activities if allowed."""
        conflicts = self._conflicts(span)
        if not conflicts:
            return []
        firm = [s for s in conflicts if s.is_firm or s.source == "calendar"]
        if firm:
            raise ToolError(
                f"{span.title} at {format_clock(span.start_dt)} – {format_clock(span.end_dt)} would overlap "
                + "; ".join(self.describe(s) for s in firm)
                + ", which is a fixed calendar commitment."
            )
        if not replace_overlaps:
            raise ToolError(
                f"{span.title} at {format_clock(span.start_dt)} – {format_clock(span.end_dt)} would overlap "
                + "; ".join(f"{self.describe(s)} [id {s.id}]" for s in conflicts)
                + ". Ask the user, then retry with replace_overlaps=true, move it, or pick another time."
            )
        return [self._carve(s, span.start_dt, span.end_dt) for s in conflicts]

    def _movable(self, span: EventSpan) -> bool:
        return not span.is_envelope and span.source != "calendar" and not span.tentative

    def _push_following(self, anchor: EventSpan) -> List[str]:
        """Slide planned activities that collide with ``anchor`` (and each other) later, keeping order."""
        cursor = anchor.end_dt
        moved: List[Tuple[EventSpan, timedelta]] = []
        followers = sorted((s for s in self.day_plan.active_spans()
                            if s.id != anchor.id and self._movable(s) and s.end_dt > anchor.start_dt
                            and s.start_dt >= anchor.start_dt - timedelta(minutes=1)), key=lambda s: s.start_dt)
        for span in followers:
            if span.start_dt >= cursor:
                break
            delta = cursor - span.start_dt
            moved.append((span, delta))
            cursor = span.end_dt + delta
        for span, delta in moved:
            new_start, new_end = span.start_dt + delta, span.end_dt + delta
            firm = [c for c in self.day_plan.spans_overlapping(new_start, new_end)
                    if c.id != span.id and (c.is_firm or c.source == "calendar")]
            if firm:
                raise ToolError(f"Sliding {span.title} later would run into {firm[0].title} "
                                f"({format_clock(firm[0].start_dt)}), a fixed commitment.")
        notes = []
        for span, delta in moved:
            span.start_dt, span.end_dt = span.start_dt + delta, span.end_dt + delta
            self._persist(span)
            notes.append(f"{span.title} now {format_clock(span.start_dt)} – {format_clock(span.end_dt)}")
        return notes

    def _save_plan_after_change(self) -> None:
        if self.day_plan.is_finalized:
            # A finalized plan that changes is no longer final.
            self.day_plan.is_finalized = False
            if self._state_db:
                meta = self._state_db.get_day_meta(self.day_plan.target_date)
                meta.is_finalized = False
                self._state_db.save_day_meta(meta)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def add_event(
        self,
        title: str,
        start: Any,
        end: Any = None,
        duration: Any = None,
        category: str = "activity",
        task_id: Optional[str] = None,
        replace_overlaps: Any = False,
        alongside: Any = False,
        push_later: Any = False,
        **_: Any,
    ) -> str:
        if not str(title or "").strip():
            raise ToolError("The item needs a title.")
        try:
            cat = SpanCategory((category or "activity").lower())
        except ValueError:
            raise ToolError(f"Unknown category '{category}'. Use activity, task, work or sleep.")
        if cat == SpanCategory.CALENDAR:
            raise ToolError("Calendar events come from the user's calendar and can't be added here.")
        if task_id and cat == SpanCategory.ACTIVITY:
            cat = SpanCategory.TASK
        start_dt = self._start(start)
        if end not in (None, ""):
            end_dt = self._end(end, start_dt)
        elif duration not in (None, ""):
            end_dt = start_dt + timedelta(minutes=parse_duration_minutes(duration))
        else:
            raise ToolError("Give an end time or a duration.")

        span = EventSpan(new_span_id(), str(title).strip(), start_dt, end_dt, cat, task_id=task_id)
        if _as_bool(alongside):
            notes = []
        elif _as_bool(push_later) and not span.is_envelope:
            firm = [c for c in self.day_plan.spans_overlapping(start_dt, end_dt) if c.is_firm or c.source == "calendar"]
            if firm:
                raise ToolError(f"{span.title} would overlap {firm[0].title}, a fixed calendar commitment.")
            notes = self._push_following(span)
        else:
            notes = self._place(span, _as_bool(replace_overlaps))
        self.day_plan.spans.append(span)
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._persist(span)
        self._save_plan_after_change()
        extra = f" ({'; '.join(notes)})" if notes else ""
        return f"Added {self.describe(span)} [id {span.id}].{extra}"

    def update_event(
        self,
        event: Any,
        start: Any = None,
        end: Any = None,
        duration: Any = None,
        shift: Any = None,
        title: Optional[str] = None,
        replace_overlaps: Any = False,
        push_later: Any = False,
        to_day: Any = None,
        **_: Any,
    ) -> str:
        span = self._find_span(event)
        if span.source == "calendar":
            raise ToolError(f"{span.title} comes from the user's calendar; change it there.")
        if to_day not in (None, ""):
            return self._move_to_day(span, to_day, start, end, duration, title)
        before = self.describe(span)
        new_start, new_end = span.start_dt, span.end_dt

        if shift not in (None, ""):
            delta = timedelta(minutes=parse_shift_minutes(shift))
            new_start, new_end = new_start + delta, new_end + delta
        else:
            if start not in (None, ""):
                new_start = self._start(start)
            if end not in (None, ""):
                new_end = self._end(end, new_start)
            elif duration not in (None, ""):
                new_end = new_start + timedelta(minutes=parse_duration_minutes(duration))
        if new_end <= new_start:
            raise ToolError(f"That would make {span.title} end ({format_clock(new_end)}) before it starts ({format_clock(new_start)}).")
        if new_start == span.start_dt and new_end == span.end_dt and not title:
            raise ToolError("Nothing to change: give a new start, end, duration, shift or title.")

        moved = EventSpan(span.id, (title or span.title).strip(), new_start, new_end, span.category,
                          source=span.source, task_id=span.task_id, status=span.status, notes=span.notes)
        if _as_bool(push_later) and not span.is_envelope:
            notes = []
            span_backup = (span.start_dt, span.end_dt)
            span.title, span.start_dt, span.end_dt = moved.title, moved.start_dt, moved.end_dt
            try:
                notes = self._push_following(span)
            except ToolError:
                span.start_dt, span.end_dt = span_backup
                raise
        else:
            notes = self._place(moved, _as_bool(replace_overlaps))
        span.title, span.start_dt, span.end_dt = moved.title, moved.start_dt, moved.end_dt
        if span.source == "routine":
            span.source = "user"
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._persist(span)
        self._save_plan_after_change()
        extra = f" ({'; '.join(notes)})" if notes else ""
        return f"Changed {before} → {self.describe(span)}.{extra}"

    def _move_to_day(self, span: EventSpan, to_day: Any, start: Any, end: Any, duration: Any, title: Any) -> str:
        source_plan = self.day_plan
        target_plan = self.plan_for(to_day)
        length = span.end_dt - span.start_dt
        if start not in (None, ""):
            new_start = resolve_clock(str(start), target_plan.day_start)
        else:
            clock = span.start_dt.timetz()
            new_start = resolve_clock(clock.strftime("%H:%M"), target_plan.day_start)
        if end not in (None, ""):
            new_end = resolve_clock(str(end), target_plan.day_start, after=new_start)
        elif duration not in (None, ""):
            new_end = new_start + timedelta(minutes=parse_duration_minutes(duration))
        else:
            new_end = new_start + length
        moved = EventSpan(span.id, (title or span.title).strip(), new_start, new_end, span.category,
                          source="user", task_id=span.task_id, notes=span.notes)
        self.day_plan = target_plan
        try:
            self._place(moved, False)
        finally:
            self.day_plan = source_plan
        before = self.describe(span)
        source_plan.spans = [s for s in source_plan.spans if s.id != span.id]
        span.title, span.start_dt, span.end_dt, span.source = moved.title, new_start, new_end, "user"
        target_plan.spans.append(span)
        target_plan.spans.sort(key=lambda s: s.start_dt)
        self._persist(span)
        return f"Moved {before} to {target_plan.target_date.strftime('%A, %B %d')}, {format_clock(new_start)} – {format_clock(new_end)}."

    def swap_events(self, first: Any, second: Any, **_: Any) -> str:
        a, b = self._find_span(first), self._find_span(second)
        if a.id == b.id:
            raise ToolError("Those are the same item.")
        for span in (a, b):
            if not self._movable(span):
                raise ToolError(f"{span.title} can't be moved here.")
        a, b = sorted((a, b), key=lambda s: s.start_dt)
        between = [s for s in self.day_plan.active_spans()
                   if self._movable(s) and s.id not in (a.id, b.id) and a.end_dt <= s.start_dt < b.start_dt]
        if not between and b.start_dt - a.end_dt <= timedelta(minutes=1):
            # Back to back: b goes first, a right after it, filling the same stretch.
            start = a.start_dt
            b_len, a_len = b.end_dt - b.start_dt, a.end_dt - a.start_dt
            b.start_dt, b.end_dt = start, start + b_len
            a.start_dt, a.end_dt = b.end_dt, b.end_dt + a_len
        else:
            a_len, b_len = a.end_dt - a.start_dt, b.end_dt - b.start_dt
            a_start, b_start = a.start_dt, b.start_dt
            a.start_dt, a.end_dt = b_start, b_start + a_len
            b.start_dt, b.end_dt = a_start, a_start + b_len
            for span in (a, b):
                clash = [s for s in self._conflicts(span) if s.id not in (a.id, b.id)]
                if clash:
                    a.start_dt, a.end_dt, b.start_dt, b.end_dt = a_start, a_start + a_len, b_start, b_start + b_len
                    raise ToolError(f"Swapping would make {span.title} overlap {clash[0].title}.")
        for span in (a, b):
            self._persist(span)
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._save_plan_after_change()
        return f"Swapped: {self.describe(b)}; {self.describe(a)}." if b.start_dt < a.start_dt else \
            f"Swapped: {self.describe(a)}; {self.describe(b)}."

    def remove_event(self, event: Any, **_: Any) -> str:
        span = self._find_span(event)
        if span.source == "calendar":
            if span.tentative:
                raise ToolError(f"{span.title} is an unconfirmed invite; use resolve_calendar_event with attend=false.")
            raise ToolError(f"{span.title} comes from the user's calendar; remove it there.")
        self._drop(span)
        self._save_plan_after_change()
        return f"Removed {self.describe(span)}."

    def clear_time(self, start: Any, end: Any, include_work: Any = False, include_sleep: Any = False, **_: Any) -> str:
        start_dt = self._start(start)
        end_dt = self._end(end, start_dt)
        allowed = {SpanCategory.ACTIVITY, SpanCategory.TASK}
        if _as_bool(include_work):
            allowed.add(SpanCategory.WORK)
        if _as_bool(include_sleep):
            allowed.add(SpanCategory.SLEEP)
        targets = [s for s in self.day_plan.spans_overlapping(start_dt, end_dt)
                   if s.category in allowed and s.source != "calendar"]
        if not targets:
            return f"Nothing to clear between {format_clock(start_dt)} and {format_clock(end_dt)}."
        changes = [self._carve(s, start_dt, end_dt) for s in targets]
        self._save_plan_after_change()
        return f"Cleared {format_clock(start_dt)} – {format_clock(end_dt)}: " + "; ".join(changes) + "."

    def open_windows(self, after: Optional[datetime] = None, before: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
        """Free stretches within the canvas (minute precision), optionally bounded."""
        lo = max(self.day_plan.day_start, after) if after else self.day_plan.day_start
        hi = min(self.day_plan.day_end, before) if before else self.day_plan.day_end
        if lo >= hi:
            return []
        busy = _merge_intervals([(max(s.start_dt, lo), min(s.end_dt, hi))
                                 for s in self.day_plan.spans_overlapping(lo, hi)])
        windows, cursor = [], lo
        for b_start, b_end in busy:
            if b_start > cursor:
                windows.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if cursor < hi:
            windows.append((cursor, hi))
        return windows

    def _daylight_minutes(self, start: datetime, end: datetime) -> int:
        lo, hi = max(start, self.day_plan.sunrise), min(end, self.day_plan.sunset)
        return max(0, int((hi - lo).total_seconds() // 60))

    def describe_window(self, start: datetime, end: datetime) -> str:
        total = int((end - start).total_seconds() // 60)
        daylight = self._daylight_minutes(start, end)
        light = "daylight" if daylight == total else ("dark" if daylight == 0 else f"{format_duration(daylight)} of it daylight")
        return f"{format_clock(start)} – {format_clock(end)} ({format_duration(total)}, {light})"

    def find_open_time(self, duration: Any = None, after: Any = None, before: Any = None, daylight: Any = None, **_: Any) -> str:
        lo = self._start(after) if after not in (None, "") else None
        hi = self._end(before, lo or self.day_plan.day_start) if before not in (None, "") else None
        windows = self.open_windows(lo, hi)
        if daylight not in (None, ""):
            want_light = _as_bool(daylight)
            sunrise, sunset = self.day_plan.sunrise, self.day_plan.sunset
            clipped = []
            for w_start, w_end in windows:
                if want_light:
                    s, e = max(w_start, sunrise), min(w_end, sunset)
                    if s < e:
                        clipped.append((s, e))
                else:
                    for s, e in ((w_start, min(w_end, sunrise)), (max(w_start, sunset), w_end)):
                        if s < e:
                            clipped.append((s, e))
            windows = clipped
        need = parse_duration_minutes(duration) if duration not in (None, "") else 1
        fits = [(s, e) for s, e in windows if (e - s).total_seconds() // 60 >= need]
        if not fits:
            return "No free stretch matches that."
        return "Free: " + "; ".join(self.describe_window(s, e) for s, e in fits) + "."

    def list_schedule(self, **_: Any) -> str:
        return self.schedule_text()

    def schedule_text(self, upcoming_from: Optional[datetime] = None) -> str:
        """The day's spans and free time as plain text, shared by tools and prompts."""
        plan = self.day_plan
        lines = []
        for s in plan.spans_in_view():
            flags = [s.category.value]
            if s.tentative:
                flags.append("UNCONFIRMED invite")
            elif s.is_firm:
                flags.append("fixed calendar commitment")
            if s.status.value != "planned":
                flags.append(s.status.value)
            if upcoming_from and s.end_dt <= upcoming_from:
                flags.append("already over")
            lines.append(f"- {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title} "
                         f"({format_duration(s.duration_minutes)}) [{', '.join(flags)}] id={s.id}")
        windows = self.open_windows(after=upcoming_from)
        free = "; ".join(self.describe_window(s, e) for s, e in windows) or "none"
        scope = "Free time remaining" if upcoming_from else "Free time"
        return ("Scheduled:\n" + ("\n".join(lines) if lines else "- nothing yet")
                + f"\n{scope}: {free}")

    def resolve_calendar_event(self, event: Any, attend: Any = True, **_: Any) -> str:
        span = self._find_span(event)
        if span.source != "calendar":
            raise ToolError(f"{span.title} is not a calendar invite.")
        going = _as_bool(attend)
        if self._state_db and span.source_event_id:
            self._state_db.set_event_decision(span.source_event_id, "attend" if going else "decline")
        self.ambiguous_events = [e for e in self.ambiguous_events if e.id != span.source_event_id]
        if going:
            span.tentative, span.is_firm = False, True
            return f"{self.describe(span)} is confirmed and now fixed on the schedule."
        self.day_plan.spans = [s for s in self.day_plan.spans if s.id != span.id]
        return f"Declined {self.describe(span)}; that time is free."

    # ------------------------------------------------------------------
    # Dispatch & state tracking
    # ------------------------------------------------------------------

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """Run a tool by name. Failures come back as a NOT_DONE sentence; nothing is changed."""
        handlers = {
            "add_event": self.add_event,
            "update_event": self.update_event,
            "remove_event": self.remove_event,
            "swap_events": self.swap_events,
            "clear_time": self.clear_time,
            "find_open_time": self.find_open_time,
            "list_schedule": self.list_schedule,
            "resolve_calendar_event": self.resolve_calendar_event,
        }
        if self.assistant:
            handlers.update(self.assistant.handlers())
        handler = handlers.get(name)
        if handler is None:
            return f"{NOT_DONE}: unknown tool '{name}'. Available: {', '.join(handlers)}."
        args = dict(arguments or {})
        if "_parse_error" in args:
            return f"{NOT_DONE}: {name} {args['_parse_error']}. Write the arguments as a JSON object."
        saved = self.day_plan
        try:
            self.day_plan = self.plan_for(args.pop("day", None))
            result = handler(**args)
            if name not in READ_ONLY_TOOLS:
                self.successful_changes += 1
            if self.day_plan is not saved:
                result = f"[{self.day_plan.target_date.strftime('%A, %B %d')}] {result}"
            return result
        except (ToolError, ClockError) as e:
            return f"{NOT_DONE}: {e}"
        except TypeError as e:
            return f"{NOT_DONE}: bad arguments for {name}: {e}"
        finally:
            self.day_plan = saved

    def capture_snapshot(self) -> PlanSnapshot:
        return PlanSnapshot(
            spans={
                s.id: SpanState(s.title, s.start_dt, s.end_dt, s.category.value, s.status.value, s.tentative, s.is_firm)
                for s in self.day_plan.spans
            },
            ambiguous_event_ids={e.id for e in self.ambiguous_events},
            is_finalized=bool(self.day_plan.is_finalized),
        )

    @staticmethod
    def compute_delta(before: PlanSnapshot, after: PlanSnapshot) -> StateDelta:
        return StateDelta(
            added=[i for i in after.spans if i not in before.spans],
            removed=[i for i in before.spans if i not in after.spans],
            changed=[i for i in after.spans if i in before.spans and after.spans[i] != before.spans[i]],
            resolved_events=list(before.ambiguous_event_ids - after.ambiguous_event_ids),
            finalized_changed=before.is_finalized != after.is_finalized,
        )
