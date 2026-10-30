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
import re
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
from thirties_core.calendar_engine import CalendarEvent, series_key
from thirties_core.clock import (
    thirty_ceil,
    unmarked_lines,
    thirty_of,
    ClockError,
    format_clock,
    format_duration,
    parse_day,
    parse_duration_minutes,
    parse_shift_minutes,
    resolve_clock,
)
from thirties_core.message_tools import MESSAGE_READ_ONLY, MESSAGE_TOOL_SPECS, MessageTools
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus, new_span_id
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
            "replace_overlaps": "true only when the user says it replaces what's there ('instead', 'replace'): trims or "
                                "removes the planned activities it covers (never fixed ones).",
            "alongside": "true if it happens during something fixed (an audiobook on the drive to an appointment).",
            "push_later": "true only when the user asks to make room by sliding what follows later, in order.",
            "fixed": "true if its time is set by someone else's clock (an appointment, a ride, a show, a call at a "
                     "set time): it keeps its exact time and never slides. false if it's flexible. Leave it out "
                     "when it's obvious (appointments, flights and rides are fixed; chores are flexible).",
            "day": "Which day, e.g. 'tomorrow', 'Monday', 'Oct 20' (default: the day being planned).",
            "again": "true to plan a second session of something already planned that day (e.g. two dev sessions). "
                     "To move an item, use update_event instead.",
        },
        "required": ["title", "start"],
    },
    {
        "name": "update_event",
        "description": "Change an existing item: new start and/or end, new duration, a shift, or a new title.",
        "parameters": {
            "event": "The item's id (or its exact title).",
            "start": "New start clock time. Without an end or duration, an activity keeps its length (it moves); work and sleep keep their end.",
            "end": "New end clock time. If no start is given, the start stays where it is.",
            "duration": "New length, keeping the start.",
            "shift": "Move the whole item, keeping its length: '+1 hour' is later, '-30 minutes' is earlier.",
            "title": "New title.",
            "replace_overlaps": "true only when the user says it replaces what's there: trims or removes the planned "
                                "activities it covers (never fixed ones).",
            "push_later": "true only when the user asks to slide what follows later to make room.",
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
        "name": "set_item_facts",
        "description": "Record facts about an item that Python then keeps: what it has to come after, a time it "
                       "can't start before, that it needs daylight, or that it stays put. With every_day=true they "
                       "hold for the item by name, every day (\"dinner takes an hour\", \"cats' dinner is at 5\", "
                       "\"PT and brush teeth go with the shower\", \"nothing outside until after the shower\": "
                       "event 'outside'), even if it isn't planned today.",
        "parameters": {"event": "The item's id or title (with every_day, its name).",
                       "after": "Item(s) it must come after, by id or title (e.g. 'Shower').",
                       "not_before": "Clock time it can't start before (e.g. '3:00 PM').",
                       "daylight": "true if it needs daylight (outdoors).",
                       "stays_put": "true if it keeps its time when the day is redrawn (a meal at a set time).",
                       "every_day": "true if the user says it holds every day, not just today.",
                       "length": "Its usual length, e.g. '1 hour' (every day).",
                       "at": "The time it's at every day, e.g. '5:00 PM'; it then never moves (every day).",
                       "goes_with": "Item(s) it's done together with, right after them (every day).",
                       "kind": "When the user says what kind of thing it is: 'once' (done means finished), "
                               "'ongoing' (worked on over time; stays open until finished) or 'daily'.",
                       "day": "The day it's on (default: the day being planned)."},
        "required": ["event"],
    },
    {
        "name": "redraw",
        "description": "Redraw the rest of today from now: the planned items still to do (including ones that "
                       "weren't reported) are laid out again in order, around fixed commitments and the items' "
                       "facts, never earlier than they were planned. Only when the user asks for a new plan or "
                       "agrees to one. Says what doesn't fit before bedtime. Can be undone with undo_redraw.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "undo_redraw",
        "description": "Put today back the way it was before the last redraw.",
        "parameters": {},
        "required": [],
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
        "name": "slot_options",
        "description": "Where something new could go when the user gives no time, or says 'later': the candidate "
                       "slots, each with what's around it and how full that part of the day is. Read-only; pick one "
                       "with a short reason (don't cram a packed stretch), then add it.",
        "parameters": {
            "duration": "How long it takes, e.g. '1 hour' (default 30 minutes).",
            "after": "Earliest clock time to consider.",
            "before": "Latest clock time to consider.",
            "daylight": "true for daylight only, false for dark only.",
            "later": "true when the user said 'later' (a real gap later on, not the next free block).",
            "day": "Which day (default: the day being planned).",
        },
        "required": [],
    },
    {
        "name": "push_back",
        "description": "Push the rest of today back by an amount ('push everything back two hours'). Every flexible "
                       "item still to do moves that much later, including the one happening now and ones past their "
                       "time that weren't marked. Fixed things, work and sleep stay; an item that would land on a "
                       "fixed thing goes right after it. Says what no longer fits before bedtime. Undo with undo_redraw.",
        "parameters": {
            "by": "How much later, e.g. '2 hours'.",
            "since": "Only items from this clock time on (default: everything not done from that long ago on).",
        },
        "required": ["by"],
    },
    {
        "name": "set_sharing",
        "description": "Record what the user said about two kinds of items sharing time, when it holds generally "
                       "('dev sessions can go alongside anything', 'never a call during the shower'), so Python stops "
                       "or keeps flagging them.",
        "parameters": {
            "first": "One kind of item, e.g. 'dev' or 'Shower'.",
            "second": "The other kind, or 'anything'.",
            "together": "true if they can share time, false if one has to move.",
        },
        "required": ["first", "second", "together"],
    },
    {
        "name": "list_schedule",
        "description": "Show a day's schedule (any day). Read-only; you will see the result before replying.",
        "parameters": {"day": "Which day, e.g. 'tomorrow' or 'Oct 20' (default: the day being planned).",
                       "exact": "true to show minute times of items shorter than 30 minutes (only if the user asks)."},
        "required": [],
    },
    {
        "name": "resolve_calendar_event",
        "description": "Record the user's decision on an unconfirmed calendar invite. \"Not mine\" (someone else's "
                       "event) is attend=false. On someone else's calendar it covers the whole recurring series.",
        "parameters": {
            "event": "The invite's id (or its exact title).",
            "attend": "true if the user is going, false if not (or it isn't theirs).",
            "series": "true/false to say whether it holds for every time this recurring event comes up (\"I go "
                      "every week\", \"just this once\"); leave out otherwise.",
        },
        "required": ["event", "attend"],
    },
]

from thirties_core.day_proposal import PROPOSAL_READ_ONLY, PROPOSAL_TOOL_SPECS  # noqa: E402
from thirties_core.list_tools import LIST_READ_ONLY, LIST_TOOL_SPECS  # noqa: E402 (after assistant_tools)
from thirties_core.routines import ROUTINE_TOOL_SPECS  # noqa: E402

SCHEDULE_TOOL_SPECS = TOOL_SPECS
TOOL_SPECS = (SCHEDULE_TOOL_SPECS + ASSISTANT_TOOL_SPECS + ROUTINE_TOOL_SPECS + PROPOSAL_TOOL_SPECS + MESSAGE_TOOL_SPECS
              + LIST_TOOL_SPECS)
READ_ONLY_TOOLS = ({"list_schedule", "find_open_time", "slot_options"} | ASSISTANT_READ_ONLY | PROPOSAL_READ_ONLY
                   | MESSAGE_READ_ONLY | LIST_READ_ONLY)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "attend", "accept", "going")
    return bool(value)


_JOINER = re.compile(r"\s+(?:/|&|\+|and|with)\s+|\s*/\s*", re.IGNORECASE)


def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in sorted(intervals, key=lambda iv: (iv[0].timestamp(), iv[1].timestamp())):  # instants (DST)
        if merged and start.timestamp() <= merged[-1][1].timestamp():
            merged[-1] = (merged[-1][0], max(merged[-1][1], end, key=lambda t: t.timestamp()))
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


ENVELOPE_WORDS_RE = re.compile(r"\b(work\w*|job|shift|office|sleep\w*|slept|bed\w*|wak\w*|woke|awake|nap\w*|"
                               r"alarm|night|tired|late|day off|off (?:today|tomorrow)|holiday|vacation|pto|sick)\b", re.IGNORECASE)
# Items may share time, so trimming, removing or sliding another item happens only when the user's message asks for it
# (10-21: "removed Throne"; 10-27: a one-hour item split at 8:00). Broad on purpose: a false refusal costs a retry.
REPLACE_WORDS_RE = re.compile(r"\b(instead|replac\w*|rather|swap\w*|drop\w*|cancel\w*|scrap\w*|ditch\w*|remov\w*|"
                              r"clear\w*|bump\w*|nix|get rid|in place of|over\s?(?:write|ride)\w*|trim\w*|cut\w*|"
                              r"shorten\w*)\b", re.IGNORECASE)
PUSH_WORDS_RE = re.compile(r"\b(push\w*|back|later|slid\w*|shift\w*|bump\w*|delay\w*|postpon\w*|mov\w*|make room|"
                           r"after|behind|late|run(?:s|ning)? (?:over|long)|ran (?:over|long)|overr?an|reflow\w*|"
                           r"rest of|everything|resched\w*|slip\w*)\b", re.IGNORECASE)

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
        self.user_message: Optional[str] = None  # this turn's message, when a person is talking (see _envelope_ok)
        self.shown_schedule: Optional[str] = None  # what show_schedule laid out this turn (message_tools)

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

    def is_current(self, plan: DayPlan) -> bool:
        """The day being lived: today, or the day that starts at the coming sunrise once it's near (planning at 7 on a
        dark morning is planning that day; Today lists it). The evening before isn't: that's planning ahead."""
        today = self.today()
        return plan.target_date == today or (plan.target_date == today + timedelta(days=1)
                                             and self._now() >= plan.day_start - timedelta(hours=3))

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

    def _separate_things(self, title: str, ignore: Optional[str] = None) -> None:
        """One item per thing: a title joining two things the user already has ("Thirties dev / Message Brad")
        is refused, so each keeps its own outcome, check-in and statistics."""
        parts = [p.strip().lower() for p in _JOINER.split(title) if p.strip()]
        if len(parts) < 2:
            return
        known = {s.title.lower() for s in self.day_plan.spans if s.id != ignore and not s.is_envelope}
        if self.services is not None:
            known |= {t.title.lower() for t in self.services.tasks}

        def is_known(part: str) -> bool:
            return len(part) > 2 and any(part == k or (len(part) > 5 and k.startswith(part)) for k in known)

        matched = [p for p in parts if is_known(p)]
        if len(matched) >= 2:
            raise ToolError(f"'{title}' is {len(parts)} separate things. Give each its own item; items can share "
                            f"the same time.")

    def _start(self, text: Any) -> datetime:
        return resolve_clock(str(text), self.day_plan.day_start)

    def _end(self, text: Any, start: datetime) -> datetime:
        return resolve_clock(str(text), self.day_plan.day_start, after=start)

    def _stack_short(self, start: datetime, end: datetime, ignore: Optional[str] = None) -> Tuple[datetime, datetime]:
        """Something under 30 minutes put at the start of a thirty that already holds short items goes after them,
        so three five-minute errands at 5:00 run 5:00, 5:05, 5:10. Only the new item moves, and only inside its
        thirty: a minute time the user gave is kept, and when the thirty is full it simply shares the time."""
        length = end - start
        if length >= timedelta(minutes=30) or start != thirty_of(start):
            return start, end
        block = thirty_of(start)
        shorts = [s for s in self.day_plan.spans_overlapping(block, block + timedelta(minutes=30))
                  if s.id != ignore and not s.is_envelope and s.duration_minutes < 30
                  and s.start_dt >= block and s.end_dt <= block + timedelta(minutes=30)]
        if not any(s.overlaps(start, end) for s in shorts):
            return start, end
        after = max(s.end_dt for s in shorts)
        if after + length <= block + timedelta(minutes=30):
            return after, after + length
        return start, end

    @staticmethod
    def _snap(start: datetime, end: datetime) -> Tuple[datetime, datetime]:
        """The user lives in thirties: something 30 minutes or longer starts on a thirty, and anything that
        wouldn't fit in the thirty it was put in moves to the next one. Lengths stay as they are."""
        length = end - start
        if length >= timedelta(minutes=30):
            begin = thirty_ceil(start)
        elif start + length <= thirty_of(start) + timedelta(minutes=30):
            return start, end
        else:
            begin = thirty_ceil(start)
        return begin, begin + length

    def _user_said(self, dt: datetime) -> bool:
        """A minute time the user said in this message ("8:47") is a real time, not one Thirties picked."""
        if not self.user_message or dt.minute in (0, 30):
            return False
        from thirties_core.clock import real_times
        return ((dt.hour % 12) or 12, dt.minute) in real_times(self.user_message)

    def _keep_to_blocks(self, start: datetime, end: datetime) -> Tuple[datetime, datetime]:
        """A flexible item Thirties places keeps to blocks (10-28 "9:35 – Work + Feed kitties", 10-29 "8:47 – Torquio
        dev"); a time the user gave is kept as it is (AGENTS §7.1: rounded only when Thirties picks a time), so a
        one-hour item the user put at 7:30 is never cut or moved at a block line (10-27)."""
        if self._user_said(start):
            return start, end
        return self._snap(start, end)

    def _is_fixed(self, span: EventSpan) -> bool:
        """Set by someone else's clock: a calendar event, a firm commitment, or an item that stays put (today, or
        at a fixed time every day: item_facts)."""
        if span.source == "calendar" or span.is_firm:
            return True
        if self._state_db is None:
            return False
        if not span.is_envelope and self._lasting().for_item(span.title).fixed_at:
            return True
        try:
            return self._facts().get(span.id).anchored
        except ToolError:
            return False

    def _set_fixed(self, span: EventSpan, fixed: bool) -> None:
        if self._state_db is None:
            return
        store = self._facts()
        facts = store.get(span.id)
        facts.anchored = fixed
        store.set(span.id, facts)

    def _lasting(self):
        """What holds every day for items by name (item_facts.LastingFacts), or None without a database."""
        from thirties_core.item_facts import LastingFacts
        return LastingFacts(self._state_db) if self._state_db is not None else None

    def _lasting_aliases(self, task_id: Optional[str]) -> List[str]:
        """Other names an item's lasting facts may be kept under: the list item it's linked to."""
        task = next((t for t in (self.services.tasks if self.services else []) if task_id and t.id == task_id), None)
        return [task.title] if task is not None else []

    def describe(self, span: EventSpan) -> str:
        """An item with its real times, and the block(s) it's in: the model reads both, and tells a schedule in
        blocks but one item by its real time and block ("12:15, in the 12:00 block")."""
        length = format_duration(span.duration_minutes)
        first, last = thirty_of(span.start_dt), thirty_of(span.end_dt - timedelta(seconds=1))
        where = f"in the {format_clock(first)} block" if first == last else \
            f"blocks {format_clock(first)} – {format_clock(thirty_ceil(span.end_dt))}"
        on_blocks = span.start_dt == first and span.end_dt == thirty_ceil(span.end_dt)
        times = f"{format_clock(span.start_dt)} – {format_clock(span.end_dt)} ({length})"
        fixed = ", fixed" if span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and self._is_fixed(span) else ""
        return f"{span.title}, {times}" + ("" if on_blocks and not fixed else f" ({where}{fixed})")

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
        """The hard conflicts: active spans this span may not overlap.

        Planned items may overlap, nest and coincide freely (10-26: "Just make them overlap! Things are allowed to
        overlap!"), and anything may sit on work and sleep. The only hard conflicts are fixed things (calendar
        commitments and fixed-time items), and work or sleep against another span of the same kind.
        """
        others = [s for s in self.day_plan.spans_overlapping(span.start_dt, span.end_dt) if s.id != span.id
                  and s.status != SpanStatus.SKIPPED]  # a skipped item didn't happen: its time is free
        if span.is_envelope:
            return [s for s in others if s.category == span.category]
        return [s for s in others if not s.is_envelope and self._is_fixed(s)]

    def _sharing(self, span: EventSpan) -> List[EventSpan]:
        """The other planned items this span shares time with (not work or sleep, not fixed ones)."""
        return [s for s in self.day_plan.spans_overlapping(span.start_dt, span.end_dt) if s.id != span.id
                and s.status != SpanStatus.SKIPPED and not s.is_envelope and not self._is_fixed(s)]

    def _sharing_rules(self):
        if self._state_db is None:
            return None
        from thirties_core.sharing import SharingRules
        return SharingRules(self._state_db)

    def _push_wanted(self, span: EventSpan, push_later: Any, alongside: Any) -> bool:
        """push_later, or the user's own words asking for it ("throw my shower to 3pm and push everything to make
        room") when the item lands on something planned: Python does the moving, the model needn't remember to."""
        if _as_bool(push_later):
            return True
        if _as_bool(alongside) or span.is_envelope:
            return False
        from thirties_core.sharing import wants_push
        return wants_push(self.user_message) and any(s.status == SpanStatus.PLANNED for s in self._sharing(span))

    def _push_asked(self) -> None:
        """Sliding other items later happens only when the user asks for it ("push the rest back")."""
        if self.user_message is not None and not PUSH_WORDS_RE.search(self.user_message):
            raise ToolError("Items can share time: add or move it without push_later and nothing else moves. Slide "
                            "what follows only when the user asks ('push the rest back').")

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
        start_t, end_t, s_t, e_t = start.timestamp(), end.timestamp(), span.start_dt.timestamp(), span.end_dt.timestamp()
        if start_t <= s_t and e_t <= end_t:  # instants: wall times repeat on the DST fall-back night
            self._drop(span)
            return f"removed {before}"
        if s_t < start_t and end_t < e_t:
            tail = EventSpan(
                new_span_id(), span.title, end, span.end_dt, span.category, source=span.source,
                task_id=span.task_id, status=span.status,
            )
            span.end_dt = start
            self.day_plan.spans.append(tail)
            self._persist(span)
            self._persist(tail)
            return f"split {before} around the cleared time"
        if s_t < start_t:
            span.end_dt = start
        else:
            span.start_dt = end
        self._persist(span)
        return f"trimmed {before} to {format_clock(span.start_dt)} – {format_clock(span.end_dt)}"

    def _place(self, span: EventSpan, replace_overlaps: bool, alongside: bool = False) -> List[str]:
        """Place a new or moved span: one coherent model for overlapping, nested and coincident items.

        Placing an item never trims, splits, pushes or removes another: they share the time, and the result says so
        (so a reply reads them as one entry, "Redfox dev + Thirties dev"). A fixed thing (a calendar commitment, a
        fixed-time item) is a hard conflict: reported, never overwritten, unless the new item really happens during
        it (alongside=true). replace_overlaps, when the user said so, trims the flexible planned items at its edges
        and removes the ones it covers; one that runs around it is never split (10-27: a one-hour item was split into
        two half-hours at 8:00), it stays and shares the time.
        """
        hard = self._conflicts(span)
        if span.is_envelope:
            if not hard:
                return []
            if not replace_overlaps:
                raise ToolError(f"{span.title} at {format_clock(span.start_dt)} – {format_clock(span.end_dt)} would "
                                "overlap " + "; ".join(f"{self.describe(s)} [id {s.id}]" for s in hard)
                                + ". Change that one instead, or retry with replace_overlaps=true.")
            return [self._carve(s, span.start_dt, span.end_dt) for s in hard]
        if hard and not alongside:
            calendar = any(s.source == "calendar" or s.is_firm for s in hard)
            raise ToolError(
                f"{span.title} at {format_clock(span.start_dt)} – {format_clock(span.end_dt)} would overlap "
                + "; ".join(self.describe(s) for s in hard)
                + (", which is a fixed calendar commitment." if calendar else ", which is fixed (it keeps its time).")
                + " Pick another time, or add it with alongside=true if it really happens during it.")
        sharing = self._sharing(span)
        if not replace_overlaps:
            from thirties_core.sharing import needs_a_word, said_together
            ask = not alongside and not said_together(self.user_message)
            rules = self._sharing_rules()
            return [f"alongside {self.describe(s)} [id {s.id}], both kept"
                    + (f"; if they can't happen together, push_later=true moves {s.title} and what follows (ask the "
                       f"user if it isn't clear)" if ask and needs_a_word(span, s, rules) else "")
                    for s in sharing]
        if self.user_message is not None and not REPLACE_WORDS_RE.search(self.user_message):
            raise ToolError("Items can share time, so nothing needs replacing: add it without replace_overlaps and "
                            "the other items keep their times. Replace only when the user says so ('instead').")
        notes = []
        for s in sharing:
            if s.status != SpanStatus.PLANNED:
                notes.append(f"{s.title} already happened, kept")
            elif s.start_dt < span.start_dt and span.end_dt < s.end_dt:
                notes.append(f"{self.describe(s)} runs around it and stays (an item is never split)")
            elif self._cut_short(s, span):  # its usual length holds unless the user said otherwise
                notes.append(f"{self.describe(s)} keeps its usual length, so it stays alongside")
            else:
                notes.append(self._carve(s, span.start_dt, span.end_dt))
        return notes

    def _cut_short(self, span: EventSpan, by: EventSpan) -> bool:
        """Carving ``by`` out of ``span`` would leave it shorter than its usual length, and the user didn't ask."""
        overlap = min(span.end_dt, by.end_dt) - max(span.start_dt, by.start_dt)
        try:
            self._lasting_ok(span, span.start_dt, span.end_dt - overlap)
        except ToolError:
            return True
        return False

    def _movable(self, span: EventSpan) -> bool:
        return not span.is_envelope and span.source != "calendar" and not span.tentative

    def _push_following(self, anchor: EventSpan) -> List[str]:
        """Slide planned activities that collide with ``anchor`` (and each other) later, keeping order."""
        cursor = anchor.end_dt
        moved: List[Tuple[EventSpan, timedelta]] = []
        now = self._now()
        followers = sorted((s for s in self.day_plan.active_spans()
                            if s.id != anchor.id and self._movable(s) and not self._is_fixed(s)
                            and s.end_dt > anchor.start_dt
                            and s.start_dt >= anchor.start_dt - timedelta(minutes=1)
                            and s.status == SpanStatus.PLANNED and not s.covers(now)),
                           key=lambda s: s.start_dt)  # what's done, or happening right now, stays where it is
        for span in followers:
            if span.start_dt >= cursor:
                break
            target, _ = self._snap(cursor, cursor + (span.end_dt - span.start_dt))
            delta = target - span.start_dt
            moved.append((span, delta))
            cursor = span.end_dt + delta
        for span, delta in moved:
            new_start, new_end = span.start_dt + delta, span.end_dt + delta
            firm = [c for c in self.day_plan.spans_overlapping(new_start, new_end)
                    if c.id != span.id and not c.is_envelope and self._is_fixed(c)]
            if firm:
                raise ToolError(f"Sliding {span.title} later would run into {firm[0].title} "
                                f"({format_clock(firm[0].start_dt)}), a fixed commitment.")
            sleep = [c for c in self.day_plan.spans_overlapping(new_start, new_end) if c.category == SpanCategory.SLEEP]
            if sleep:
                raise ToolError(f"Sliding {span.title} later would push it into bedtime "
                                f"({format_clock(sleep[0].start_dt)}).")
        notes = []
        for span, delta in moved:
            span.start_dt, span.end_dt = span.start_dt + delta, span.end_dt + delta
            self._persist(span)
            notes.append(f"{self.describe(span).replace(', ', ' now ', 1)}")
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
        fixed: Any = None,
        again: Any = False,
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
        if not task_id and self.assistant is not None and cat == SpanCategory.ACTIVITY:
            same = self.assistant._exact_task(str(title))  # worded exactly as a list item: it's that item
            task_id = same.id if same is not None and not same.id.startswith("media:") else None
        if task_id and cat == SpanCategory.ACTIVITY:
            cat = SpanCategory.TASK
        if not _as_bool(again) and cat not in (SpanCategory.WORK, SpanCategory.SLEEP):
            # Adding what's already planned that day makes a ghost copy that nags and rolls over (10-27 to 10-30:
            # "Email back GitHub" five times, "So Voice! dev" listed twice): moving is update_event.
            same = [s for s in self.day_plan.spans if s.status == SpanStatus.PLANNED and not s.is_envelope
                    and s.source != "calendar"
                    and ((task_id and s.task_id == task_id) or s.title.strip().lower() == str(title).strip().lower())]
            if same:
                raise ToolError(f"{self.describe(same[0])} is already planned [id {same[0].id}]. To move it, use "
                                "update_event with that id; to plan a second session as well, add it with again=true.")
        self._separate_things(str(title))
        start_dt = self._start(start)
        end_dt = None
        if end not in (None, ""):
            end_dt = self._end(end, start_dt)
        elif duration not in (None, ""):
            end_dt = start_dt + timedelta(minutes=parse_duration_minutes(duration))
        from thirties_core.item_facts import for_new_item  # its usual length and fixed time (R6)
        start_dt, end_dt, lasting = for_new_item(self._lasting(), str(title), self._lasting_aliases(task_id), start_dt,
                                                 end_dt, _as_bool(alongside), self.user_message,
                                                 [s.title for s in self.day_plan.spans], self.day_plan.day_start)
        if end_dt is None:
            raise ToolError("Give an end time or a duration.")

        from thirties_core.facts import looks_fixed
        is_fixed = looks_fixed(str(title)) if fixed in (None, "") else _as_bool(fixed)
        is_fixed = is_fixed or bool(self._state_db is not None and self._lasting().for_item(str(title)).fixed_at)
        if cat not in (SpanCategory.WORK, SpanCategory.SLEEP) and not is_fixed:
            start_dt, end_dt = self._keep_to_blocks(start_dt, end_dt)  # flexible: keeps to blocks; fixed: real time
        if not _as_bool(alongside) and not is_fixed:
            start_dt, end_dt = self._stack_short(start_dt, end_dt)
        span = EventSpan(new_span_id(), str(title).strip(), start_dt, end_dt, cat, task_id=task_id)
        if self._push_wanted(span, push_later, alongside) and not span.is_envelope and not _as_bool(alongside):
            self._push_asked()
            firm = [c for c in self.day_plan.spans_overlapping(start_dt, end_dt)
                    if not c.is_envelope and c.status == SpanStatus.PLANNED and self._is_fixed(c)]
            if firm:
                raise ToolError(f"{span.title} would overlap {self.describe(firm[0])}, which is fixed.")
            notes = self._push_following(span)
        else:
            notes = self._place(span, _as_bool(replace_overlaps), alongside=_as_bool(alongside))
        self.day_plan.spans.append(span)
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._persist(span)
        if is_fixed and not span.is_envelope and self._state_db is not None:
            self._set_fixed(span, True)
        self._save_plan_after_change()
        extra = f" ({'; '.join(lasting + notes)})" if lasting or notes else ""
        weather = self.assistant.weather_warning(span) if self.assistant else None
        if weather:
            extra += f" Weather heads-up: {weather}."
        if self.assistant and self.is_current(self.day_plan):
            listed = self.assistant.list_today(span)
            extra += f" {listed}" if listed else ""
        elif self.assistant and self.day_plan.target_date > self.today():
            from thirties_core.day_lists import ahead_note
            extra += f" {ahead_note(self.assistant, self.day_plan)}"
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
        fixed: Any = None,
        **_: Any,
    ) -> str:
        span = self._find_span(event)
        self._envelope_ok(span)
        if fixed not in (None, ""):
            self._set_fixed(span, _as_bool(fixed))
        if span.source == "calendar":
            raise ToolError(f"{span.title} comes from the user's calendar; change it there.")
        if title:
            self._separate_things(str(title))
        if to_day not in (None, ""):
            if span.status == SpanStatus.PLANNED and span.end_dt > self._now():
                self._lasting_ok(span, span.start_dt + timedelta(days=1), span.end_dt + timedelta(days=1))
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
            elif start not in (None, "") and not span.is_envelope:
                new_end = new_start + (span.end_dt - span.start_dt)  # "move X to 5" moves it, keeping its length
        if new_end <= new_start:
            raise ToolError(f"That would make {span.title} end ({format_clock(new_end)}) before it starts ({format_clock(new_start)}).")
        if new_start == span.start_dt and new_end == span.end_dt and not title:
            raise ToolError("Nothing to change: give a new start, end, duration, shift or title.")
        self._lasting_ok(span, new_start, new_end)

        if not span.is_envelope and not self._is_fixed(span) and new_start != span.start_dt:
            new_start, new_end = self._keep_to_blocks(new_start, new_end)  # a new length alone keeps the start
        if start not in (None, "") and shift in (None, "") and not self._is_fixed(span):
            new_start, new_end = self._stack_short(new_start, new_end, ignore=span.id)
        moved = EventSpan(span.id, (title or span.title).strip(), new_start, new_end, span.category,
                          source=span.source, task_id=span.task_id, status=span.status, notes=span.notes)
        if not span.is_envelope and self._push_wanted(moved, push_later, False):
            self._push_asked()
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
        auto_skipped = False
        if (span.status == SpanStatus.PLANNED and span.end_dt <= self._now() and not span.is_envelope
                and span.source != "calendar" and target_plan.target_date > source_plan.target_date):
            # It's over and didn't happen: moving it on is a skip, and it counts as put off.
            span.status, auto_skipped = SpanStatus.SKIPPED, True
            self._persist(span)
            if span.task_id and self._state_db and (self.assistant is None or self.assistant.put_off_counts(span)):
                self._state_db.record_task_deferred(span.task_id)
        if span.status != SpanStatus.PLANNED:
            # Keep today's record (done/skipped) as history; plan a fresh copy on the new day.
            moved.id = new_span_id()
            target_plan.spans.append(moved)
            target_plan.spans.sort(key=lambda s: s.start_dt)
            self._persist(moved)
            if self.assistant and self.is_current(target_plan):
                self.assistant.list_today(moved)
            return (f"Planned {moved.title} again on {target_plan.target_date.strftime('%A, %B %d')}, "
                    f"{format_clock(new_start)} – {format_clock(new_end)} "
                    + ("(the earlier one is marked skipped)." if auto_skipped else "(today's record stays)."))
        source_plan.spans = [s for s in source_plan.spans if s.id != span.id]
        span.title, span.start_dt, span.end_dt, span.source = moved.title, new_start, new_end, "user"
        target_plan.spans.append(span)
        target_plan.spans.sort(key=lambda s: s.start_dt)
        self._persist(span)
        listed = ""
        if self.assistant and self.is_current(target_plan):
            listed = self.assistant.list_today(span)
        return f"Moved {before} to {target_plan.target_date.strftime('%A, %B %d')}, {format_clock(new_start)} – {format_clock(new_end)}." + (f" {listed}" if listed else "")

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
                    raise ToolError(f"Swapping would make {span.title} overlap {clash[0].title}, which is fixed.")
        for span in (a, b):
            self._persist(span)
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._save_plan_after_change()
        first, second = (b, a) if b.start_dt < a.start_dt else (a, b)
        shared = [f"{span.title} now shares time with "
                  + ", ".join(o.title for o in self._sharing(span) if o.id not in (a.id, b.id))
                  for span in (first, second) if any(o.id not in (a.id, b.id) for o in self._sharing(span))]
        return f"Swapped: {self.describe(first)}; {self.describe(second)}." + (f" ({'; '.join(shared)})" if shared else "")

    def _lasting_ok(self, span: EventSpan, new_start: datetime, new_end: datetime) -> None:
        """What the user said holds every day for an item isn't undone behind their back (10-26 and 10-28: dinner cut
        to 30 minutes; 10-30: the cats' dinner moved to 6): a fixed item moves, and an item is cut below its usual
        length, only when the user's message names it."""
        if span.is_envelope or span.source == "calendar":
            return
        from thirties_core.item_facts import change_refused
        why = change_refused(self._lasting(), span, self._lasting_aliases(span.task_id), new_start, new_end,
                             self.user_message, [s.title for s in self.day_plan.spans], self.day_plan.day_start)
        if why:
            raise ToolError(why)

    def _envelope_ok(self, span: EventSpan) -> None:
        """Work and sleep change only when the user talks about them (10-27: placing the morning routine moved Work
        to 10:30, and the user had to say "Work starts at 7am")."""
        if span.is_envelope and self.user_message is not None and not ENVELOPE_WORDS_RE.search(self.user_message):
            raise ToolError(f"{span.title} only changes when the user talks about it; leave it as it is and fit the "
                            "rest around it (items can sit alongside work).")

    def remove_event(self, event: Any, **_: Any) -> str:
        span = self._find_span(event)
        self._envelope_ok(span)
        if span.source == "calendar":
            if span.tentative:
                raise ToolError(f"{span.title} is an unconfirmed invite; use resolve_calendar_event with attend=false.")
            raise ToolError(f"{span.title} comes from the user's calendar. If they aren't going, decline it with "
                            f"resolve_calendar_event (attend=false); to delete it, they remove it from their calendar.")
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
        """Free stretches within the canvas, in whole thirties (a thirty with anything in it isn't free),
        optionally bounded. With ``after`` (now), the current thirty counts if it's empty."""
        windows = self._open_minutes(thirty_of(after) if after else None, before)
        out = []
        for start, end in windows:
            start, end = thirty_ceil(start), thirty_of(end)
            if end - start >= timedelta(minutes=30):
                out.append((start, end))
        return out

    def _open_minutes(self, after: Optional[datetime] = None, before: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
        lo = max(self.day_plan.day_start, after) if after else self.day_plan.day_start
        hi = min(self.day_plan.day_end, before) if before else self.day_plan.day_end
        if lo >= hi:
            return []
        busy = _merge_intervals([(max(s.start_dt, lo), min(s.end_dt, hi))
                                 for s in self.day_plan.spans_overlapping(lo, hi) if s.status != SpanStatus.SKIPPED])
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

    def slot_options(self, duration: Any = None, after: Any = None, before: Any = None, daylight: Any = None,
                     later: Any = False, **_: Any) -> str:
        """Where something new could go (B8): Python lists the slots and what's around them; the model picks."""
        from thirties_core.placement import slot_options_text
        minutes = parse_duration_minutes(duration) if duration not in (None, "") else 30
        now = self._now()
        live = self.day_plan.day_start <= now < self.day_plan.day_end
        return slot_options_text(
            self, self.day_plan, minutes, now if live else None,
            after=self._start(after) if after not in (None, "") else None,
            before=self._end(before, self.day_plan.day_start) if before not in (None, "") else None,
            daylight=_as_bool(daylight) if daylight not in (None, "") else None, later=_as_bool(later))

    def set_sharing(self, first: Any = "", second: Any = "", together: Any = True, **_: Any) -> str:
        """A lasting answer to "alongside, or push it back?" for two kinds of items."""
        rules = self._sharing_rules()
        if rules is None:
            raise ToolError("Preferences can't be saved here.")
        try:
            pair = rules.set(str(first), str(second), _as_bool(together))
        except ValueError as e:
            raise ToolError(str(e))
        return f"Noted: {pair} " + ("can share time." if _as_bool(together) else "can't share time; one moves.")

    def push_back(self, by: Any = None, since: Any = None, **_: Any) -> str:
        """"Push everything back two hours" (S4): the rest of the day moves, past unmarked items included."""
        from thirties_core.placement import push_back
        if by in (None, ""):
            raise ToolError("Say how much later, e.g. '2 hours' (or use redraw to lay the rest of today out again).")
        minutes = parse_shift_minutes(by) if str(by).strip()[:1] in "+-" else parse_duration_minutes(by)
        if minutes <= 0:
            raise ToolError("push_back moves things later; to move one item earlier, use update_event.")
        return push_back(self, minutes, self._start(since) if since not in (None, "") else None)

    # --- facts and redrawing ---

    def _facts(self):
        from thirties_core.facts import SpanFacts
        if self._state_db is None:
            raise ToolError("Facts about items can't be saved here.")
        return SpanFacts(self._state_db)

    def set_item_facts(self, event: Any, after: Any = None, not_before: Any = None, daylight: Any = None,
                       stays_put: Any = None, every_day: Any = None, length: Any = None, at: Any = None,
                       goes_with: Any = None, kind: Any = None, **_: Any) -> str:
        if _as_bool(every_day) or any(v not in (None, "", []) for v in (length, at, goes_with, kind)):
            from thirties_core.item_facts import record_lasting  # by name, every day (R6)
            return record_lasting(self, event, after=after, not_before=not_before, daylight=daylight,
                                  stays_put=stays_put, length=length, at=at, goes_with=goes_with, kind=kind)
        span = self._find_span(event)
        store = self._facts()
        facts = store.get(span.id)
        if after not in (None, "", []):
            refs = after if isinstance(after, list) else [a for a in re.split(r"\s*(?:,|;| and )\s*", str(after)) if a]
            ids = [self._find_span(r).id for r in refs]
            if span.id in ids:
                raise ToolError("An item can't come after itself.")
            facts.after = sorted(set(facts.after) | set(ids))
        if not_before not in (None, ""):
            facts.not_before = self._start(not_before)
        if daylight not in (None, ""):
            facts.daylight = _as_bool(daylight)
        if stays_put not in (None, ""):
            facts.anchored = _as_bool(stays_put)
        store.set(span.id, facts)
        titles = {s.id: s.title for s in self.day_plan.spans}
        return f"Noted for {span.title}: " + (", ".join(facts.describe(titles)) or "nothing special") + "."

    def _behind(self, now: datetime) -> List[EventSpan]:
        """Planned items whose time has passed with nothing reported (not assumed done)."""
        return [s for s in self.day_plan.active_spans() if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
                and s.status == SpanStatus.PLANNED and not s.tentative and s.end_dt.timestamp() <= now.timestamp()]

    def redraw(self, **_: Any) -> str:
        import json
        plan = self.day_plan
        now = self._now()
        if not (plan.day_start.timestamp() <= now.timestamp() < plan.day_end.timestamp()):
            raise ToolError("Redrawing works on today, from now; for another day, move items directly.")
        from thirties_core.item_facts import redraw_facts
        start_from = thirty_ceil(now)
        facts = redraw_facts(plan.spans, self._facts(), self._lasting(), plan.day_start)  # today's and every day's
        movable = sorted((s for s in plan.active_spans() if self._movable(s) and s.status == SpanStatus.PLANNED
                          and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and not facts[s.id].anchored
                          and (s.start_dt >= start_from or s.end_dt <= now)), key=lambda s: s.start_dt)
        movable = self._in_fact_order(movable, facts)
        ids = {s.id for s in movable}
        original = {s.id: (s.start_dt, s.end_dt) for s in movable}
        blockers = [s for s in plan.active_spans() if s.id not in ids and not s.tentative and s.end_dt > start_from
                    and s.status == SpanStatus.PLANNED]
        ends: Dict[str, datetime] = {s.id: s.end_dt for s in plan.spans if s.id not in ids}
        placed: Dict[str, Tuple[datetime, datetime]] = {}
        no_room: List[EventSpan] = []
        cursor = start_from
        for span in movable:
            length = span.end_dt - span.start_dt
            f = facts[span.id]
            partner = next((p for p in movable if p.id in placed and p.id != span.id and length >= timedelta(minutes=30)
                            and original[p.id][0] < span.end_dt and span.start_dt < original[p.id][1]
                            and original[p.id][1] - original[p.id][0] >= timedelta(minutes=30)), None)
            if partner is not None:  # it happens alongside something (an audiobook during dinner): it moves with it
                t = placed[partner.id][0] + (span.start_dt - original[partner.id][0])
                placed[span.id] = (t, t + length)
                ends[span.id] = t + length
                continue
            earliest = max([cursor] + ([span.start_dt] if span.start_dt >= start_from else [])
                           + ([f.not_before] if f.not_before else [])
                           + [ends[a] for a in f.after if a in ends])
            if f.daylight:
                earliest = max(earliest, plan.sunrise)
            t = earliest
            for _ in range(200):
                t, end = self._snap(t, t + length)
                hit = [b for b in blockers if b.start_dt < end and t < b.end_dt]
                hit += [s for s in movable if s.id in placed and placed[s.id][0] < end and t < placed[s.id][1]
                        and (length >= timedelta(minutes=30) or placed[s.id][1] - placed[s.id][0] >= timedelta(minutes=30))]
                if not hit:
                    break
                t = max(b.end_dt if b.id not in placed else placed[b.id][1] for b in hit)
            fits = end <= plan.day_end and not (f.daylight and end > plan.sunset) and not any(
                b.start_dt < end and t < b.end_dt for b in blockers if b.category == SpanCategory.SLEEP)
            if not fits:
                no_room.append(span)
                ends[span.id] = span.end_dt
                continue
            placed[span.id] = (t, end)
            ends[span.id] = end
            cursor = end
        if self._state_db is not None:
            self._state_db.set_sync_meta(f"redraw_undo:{plan.target_date.isoformat()}", json.dumps(
                {"moved": {s.id: [s.start_dt.isoformat(), s.end_dt.isoformat()] for s in movable if s.id in placed},
                 "removed": [s.to_dict() for s in no_room]}))
        behind = {s.id for s in self._behind(now)}
        for span in movable:
            if span.id in placed:
                span.start_dt, span.end_dt = placed[span.id]
                self._persist(span)
        for span in no_room:  # it can't stay where it was: something else may be there now
            self._drop(span)
        plan.spans.sort(key=lambda s: s.start_dt)
        self._save_plan_after_change()
        from thirties_core.clock import agenda_lines  # things moved together still read as one entry (A + B)
        lines = agenda_lines([(s.start_dt, s.end_dt, s.title, "") for s in movable if s.id in placed])
        out = ["Redrew the rest of today:" if lines else "Nothing to redraw: nothing flexible is left today."] + lines
        if no_room:
            out.append("Doesn't fit before bedtime, so it's off today's plan: " + ", ".join(s.title for s in no_room)
                       + (". Plan it tomorrow, or let it go?" if len(no_room) == 1 else
                          ". Plan them tomorrow, or let them go?"))
        unreported = [s.title for s in movable if s.id in behind and s.id in placed]
        if unreported:
            out.append("Not reported, so kept as still to do: " + ", ".join(unreported) + ".")
        return "\n".join(out)

    @staticmethod
    def _in_fact_order(spans: List[EventSpan], facts) -> List[EventSpan]:
        """Planned order, except that an item comes after whatever it has to come after (and right after what it
        goes with)."""
        from thirties_core.item_facts import fact_order
        return fact_order(spans, facts)

    def undo_redraw(self, **_: Any) -> str:
        import json
        raw = self._state_db.get_sync_meta(f"redraw_undo:{self.day_plan.target_date.isoformat()}") if self._state_db else None
        if not raw:
            raise ToolError("There's no redraw to undo today.")
        before = json.loads(raw)
        restored = []
        for span in self.day_plan.spans:
            if span.id in before["moved"]:
                span.start_dt, span.end_dt = (datetime.fromisoformat(x) for x in before["moved"][span.id])
                self._persist(span)
                restored.append(span.title)
        for data in before["removed"]:
            if not any(s.id == data["id"] for s in self.day_plan.spans):
                span = EventSpan.from_dict(data)
                self.day_plan.spans.append(span)
                self._persist(span)
                restored.append(span.title)
        self.day_plan.spans.sort(key=lambda s: s.start_dt)
        self._state_db.set_sync_meta(f"redraw_undo:{self.day_plan.target_date.isoformat()}", "")
        return "Put back as it was before the redraw: " + (", ".join(restored) or "nothing to restore") + "."

    def list_schedule(self, exact: Any = False, **_: Any) -> str:
        return self.schedule_text(exact=_as_bool(exact))

    @staticmethod
    def _flags(s: EventSpan, upcoming_from: Optional[datetime]) -> List[str]:
        flags = [s.category.value]
        if s.tentative:
            flags.append("UNCONFIRMED invite")
        elif s.is_firm:
            flags.append("fixed calendar commitment")
        if s.status.value != "planned":
            flags.append(s.status.value)
        if upcoming_from and s.end_dt <= upcoming_from:
            flags.append("already over")
        return flags

    def schedule_text(self, upcoming_from: Optional[datetime] = None, exact: bool = False) -> str:
        """The day's spans and free time as plain text, shared by tools and prompts. Items that share time (overlap,
        nest or coincide) are one entry joined with +, so they're told as one ("Redfox dev + Thirties dev")."""
        plan = self.day_plan
        lines = []
        shown_blocks = set()
        # someone else's events (show-only calendars) are for the Agenda only: the assistant never brings them up (O1)
        in_view = [s for s in plan.spans_in_view() if not s.shown_only]
        clusters: List[List[EventSpan]] = []
        for s in (x for x in in_view if not x.is_envelope):
            if clusters and s.start_dt < max(x.end_dt for x in clusters[-1]):
                clusters[-1].append(s)
            else:
                clusters.append([s])
        shared = {s.id: c for c in clusters if len(c) > 1 for s in c}

        def when(start: datetime, end: datetime) -> str:
            start, end = (start, end) if exact else (thirty_of(start), thirty_ceil(end))
            return f"{format_clock(start)} – {format_clock(end)}"

        for s in in_view:
            if s.id in shared:
                group = shared[s.id]
                if group[0].id != s.id:
                    continue
                lo, hi = min(x.start_dt for x in group), max(x.end_dt for x in group)
                lines.append(f"- {when(lo, hi)}: " + " + ".join(
                    f"{x.title}" + ("" if (x.start_dt, x.end_dt) == (lo, hi) else f" ({when(x.start_dt, x.end_dt)})")
                    + f" [{', '.join(self._flags(x, upcoming_from))}] id={x.id}" for x in group)
                    + " (sharing this time: one entry)")
                continue
            if s.duration_minutes < 30 and thirty_of(s.start_dt) == thirty_of(s.end_dt - timedelta(seconds=1)) \
                    and not s.is_envelope and not exact:
                block = thirty_of(s.start_dt)
                if block in shown_blocks:
                    continue
                shown_blocks.add(block)
                group = [x for x in in_view if x.duration_minutes < 30 and not x.is_envelope and x.id not in shared
                         and thirty_of(x.start_dt) == block and x.end_dt <= block + timedelta(minutes=30)]
                lines.append(f"- {format_clock(block)} – {format_clock(block + timedelta(minutes=30))}: " + ", then ".join(
                    f"{x.title}{' [' + x.status.value + ']' if x.status.value != 'planned' else ''} id={x.id}"
                    for x in group) + " (short items sharing this block, in this order)")
                continue
            lines.append(f"- {when(s.start_dt, s.end_dt)}: {s.title} [{', '.join(self._flags(s, upcoming_from))}] "
                         f"id={s.id}")
        windows = self.open_windows(after=upcoming_from)
        free = "; ".join(self.describe_window(s, e) for s, e in windows) or "none"
        scope = "Free time remaining" if upcoming_from else "Free time"
        now = self._now()
        from thirties_core.messages import missed_for, missed_lines  # one per item, not the block the user is in (C2)
        from thirties_core.day_lists import pencilled_text
        behind = missed_for(self, now) if plan.day_start <= now < plan.day_end else []
        return ("Scheduled:\n" + ("\n".join(lines) if lines else "- nothing yet")
                + f"\n{scope}: {free}"
                + (pencilled_text(self.assistant, plan) if self.assistant else "")
                + ("\nNot marked yet (past their time, not assumed done):\n"
                   + "\n".join(missed_lines(behind)) if behind else ""))

    def resolve_calendar_event(self, event: Any, attend: Any = True, series: Any = None, **_: Any) -> str:
        span = self._find_span(event)
        if span.source != "calendar":
            raise ToolError(f"{span.title} is not a calendar invite.")
        going = _as_bool(attend)
        invite = next((e for e in self.ambiguous_events if e.id == span.source_event_id), None)
        # Obvious default: a decision on someone else's event is about the series (O1: the wife's rehearsals came
        # back every week); on the user's own calendar it's about this one time.
        whole = _as_bool(series) if series not in (None, "") else bool(invite is not None and not invite.is_primary)
        if self._state_db and span.source_event_id:
            if whole:
                self._state_db.set_event_decision(series_key(invite or span.source_event_id),
                                                  "attend" if going else "not_mine")
            else:
                self._state_db.set_event_decision(span.source_event_id, "attend" if going else "decline")
        decided = {span.source_event_id}
        if whole:  # the rest of the series on this day, too
            key = series_key(invite or span.source_event_id)
            decided |= {e.id for e in self.ambiguous_events if series_key(e) == key}
        self.ambiguous_events = [e for e in self.ambiguous_events if e.id not in decided]
        every = " (every time it comes up)" if whole else ""
        if going:
            for s in self.day_plan.spans:
                if s.source_event_id in decided:
                    s.tentative, s.is_firm = False, True
            return f"{self.describe(span)} is confirmed and now fixed on the schedule{every}."
        self.day_plan.spans = [s for s in self.day_plan.spans if s.source_event_id not in decided]
        return f"Declined {self.describe(span)}{every}; that time is free."

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
            "set_item_facts": self.set_item_facts,
            "redraw": self.redraw,
            "undo_redraw": self.undo_redraw,
            "slot_options": self.slot_options,
            "set_sharing": self.set_sharing,
            "push_back": self.push_back,
        }
        if self.assistant:
            handlers.update(self.assistant.handlers())
        handlers.update(MessageTools(self).handlers())
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
