"""Where things go, decided with judgement: Python lists the options, the model picks.

Three jobs, all deterministic (AGENTS.md §7.3: Python works out the facts and the options; the model chooses):

- ``slot_options``: for a new item with no time, the candidate slots, each with what's around it and how full that
  part of the day is (B8, S5: "Look how cramped the afternoon is and how open the evening is"). "Later" means a real
  gap later on, never the next free block (10-24/10-25: "later this evening" became 5:00, then 7:00, then 9:30).
- ``push_back``: "push everything back two hours" moves every flexible item still to do, including the one happening
  now and the ones past their time that nobody marked (S4, 10-30: "yes obviously include everything from the last
  two hours that's why I'm pushing it back!").
- ``early_tick_times``: when something is ticked done before its time, the times Thirties guesses for it keep to
  blocks, and several early ticks go back to back (soak friction 3: two 9:00 PM items ticked at 8:06 were both
  stored 7:36 – 8:06, across two thirties).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from thirties_core.clock import format_clock, format_duration, thirty_ceil, thirty_of
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

THIRTY = timedelta(minutes=30)
# "Later" starts at least this far from now: the next free block is never "later" (S5).
LATER_AT_LEAST = timedelta(hours=1)
# A gap with this much to spare beyond the item is a real gap, not a squeeze between two things.
BREATHING_ROOM = timedelta(minutes=30)
# A gap this much longer than the item also offers its middle (clear time on both sides).
MIDDLE_FROM = timedelta(hours=1)
MAX_OPTIONS = 8

# Parts of the day by clock hour (the start hour of each), for "how full is the afternoon".
PARTS = ((5, "morning"), (12, "afternoon"), (17, "evening"), (21, "night"))


def part_of_day(dt: datetime) -> str:
    name = "night"
    for hour, label in PARTS:
        if dt.hour >= hour:
            name = label
    return name


def _occupied(plan: DayPlan, start: datetime, end: datetime) -> List[EventSpan]:
    return [s for s in plan.spans_overlapping(start, end) if s.status != SpanStatus.SKIPPED]


def fullness(plan: DayPlan, now: Optional[datetime] = None) -> Dict[str, Tuple[int, int]]:
    """For each part of the day still ahead: (thirties with something in them, thirties awake). Sleep doesn't count;
    work does (it's busy time)."""
    out: Dict[str, Tuple[int, int]] = {}
    for b in plan.blocks:
        if now is not None and b.end_dt <= now:
            continue
        there = _occupied(plan, b.start_dt, b.end_dt)
        if any(s.category == SpanCategory.SLEEP for s in there):
            continue
        part = part_of_day(b.start_dt)
        busy, total = out.get(part, (0, 0))
        out[part] = (busy + (1 if there else 0), total + 1)
    return out


@dataclass
class Slot:
    start: datetime
    end: datetime
    where: str        # "right after Laundry", "in the middle of the stretch", "right before Dinner"
    stretch: Tuple[datetime, datetime]

    def describe(self, plan: DayPlan, full: Dict[str, Tuple[int, int]]) -> str:
        lo, hi = self.stretch
        part = part_of_day(self.start)
        busy, total = full.get(part, (0, 0))
        light = "daylight" if plan.sunrise <= self.start and self.end <= plan.sunset else (
            "dark" if self.end <= plan.sunrise or self.start >= plan.sunset else "partly daylight")
        return (f"{format_clock(self.start)} – {format_clock(self.end)}: {self.where}, in a "
                f"{format_duration(int((hi - lo).total_seconds() // 60))} free stretch ({format_clock(lo)} – "
                f"{format_clock(hi)}, {light}); {part}: {busy} of {total} blocks planned")


def _neighbour_before(plan: DayPlan, t: datetime) -> Optional[EventSpan]:
    ended = [s for s in plan.active_spans() if s.status != SpanStatus.SKIPPED and s.end_dt <= t
             and t - s.end_dt < THIRTY]
    return max(ended, key=lambda s: (s.end_dt, not s.is_envelope)) if ended else None


def _neighbour_after(plan: DayPlan, t: datetime) -> Optional[EventSpan]:
    starting = [s for s in plan.active_spans() if s.status != SpanStatus.SKIPPED and s.start_dt >= t
                and s.start_dt - t < THIRTY]
    return min(starting, key=lambda s: (s.start_dt, s.is_envelope)) if starting else None


def _name(span: EventSpan) -> str:
    return "bedtime" if span.category == SpanCategory.SLEEP else span.title


def candidate_slots(tools: "PlanTools", plan: DayPlan, minutes: int, now: Optional[datetime] = None,
                    after: Optional[datetime] = None, before: Optional[datetime] = None,
                    daylight: Optional[bool] = None, later: bool = False) -> List[Slot]:
    """Candidate slots, in whole thirties, for something that takes ``minutes``: in each free stretch, right after
    what precedes it, in its middle when it's roomy, and right before what follows. ``now`` (today) keeps them ahead
    of the clock; ``later`` keeps them an hour or more out, in real gaps."""
    length = timedelta(minutes=minutes)
    blocks = THIRTY * math.ceil(minutes / 30)  # the thirties it takes
    lo = after
    if now is not None:
        lo = max(lo, thirty_of(now)) if lo else thirty_of(now)
        if later:
            lo = max(lo, thirty_ceil(now + LATER_AT_LEAST))
    windows = _windows(tools, plan, lo, before)
    if daylight is not None:
        clipped = []
        for w_start, w_end in windows:
            parts = [(max(w_start, plan.sunrise), min(w_end, plan.sunset))] if daylight else \
                [(w_start, min(w_end, plan.sunrise)), (max(w_start, plan.sunset), w_end)]
            clipped += [(thirty_ceil(s), thirty_of(e)) for s, e in parts if thirty_of(e) - thirty_ceil(s) >= THIRTY]
        windows = clipped
    fitting = [(s, e) for s, e in windows if e - s >= blocks]
    if later:  # a real gap, with room to breathe, when there is one
        roomy = [(s, e) for s, e in fitting if e - s >= blocks + BREATHING_ROOM]
        fitting = roomy or fitting
    slots: List[Slot] = []
    for w_start, w_end in fitting:
        prev, nxt = _neighbour_before(plan, w_start), _neighbour_after(plan, w_end)
        if now is not None and w_start == thirty_of(now):
            first = "now, the next thing to do"
        elif prev is not None:
            first = f"right after {_name(prev)}"
        else:
            first = "at the start of the stretch"
        slots.append(Slot(w_start, w_start + length, first, (w_start, w_end)))
        if w_end - w_start >= blocks + MIDDLE_FROM:
            mid = thirty_of(w_start + (w_end - w_start - blocks) / 2)
            if mid > w_start:
                clear_before = format_duration(int((mid - w_start).total_seconds() // 60))
                clear_after = format_duration(int((w_end - mid - blocks).total_seconds() // 60))
                slots.append(Slot(mid, mid + length, f"in the middle, with {clear_before} clear before and "
                                                     f"{clear_after} after", (w_start, w_end)))
        end_start = w_end - blocks
        if end_start > w_start and all(s.start != end_start for s in slots):
            last = f"right before {_name(nxt)}" if nxt is not None else "at the end of the stretch"
            slots.append(Slot(end_start, end_start + length, last, (w_start, w_end)))
    if len(slots) > MAX_OPTIONS:  # the first stretch and the roomiest ones
        stretches = sorted({s.stretch for s in slots}, key=lambda w: (w != fitting[0], -(w[1] - w[0]), w[0]))
        keep: List[Slot] = []
        for stretch in stretches:
            group = [s for s in slots if s.stretch == stretch]
            if len(keep) + len(group) > MAX_OPTIONS:
                continue
            keep += group
        slots = sorted(keep, key=lambda s: s.start)
    return slots


def _windows(tools: "PlanTools", plan: DayPlan, after: Optional[datetime], before: Optional[datetime]):
    saved = tools.day_plan
    tools.day_plan = plan
    try:
        return tools.open_windows(after, before)
    finally:
        tools.day_plan = saved


def slot_options_text(tools: "PlanTools", plan: DayPlan, minutes: int, now: Optional[datetime], **kw) -> str:
    slots = candidate_slots(tools, plan, minutes, now, **kw)
    full = fullness(plan, now)
    overview = "How full: " + ", ".join(f"{part} {busy} of {total} blocks planned" for part, (busy, total)
                                         in full.items()) + "." if full else ""
    if not slots:
        return f"No free stretch fits {format_duration(minutes)}" + (" later on" if kw.get("later") else "") + \
            ". It can still share time with something, or go on another day. " + overview
    lines = [f"Options for {format_duration(minutes)} (nothing is placed yet; pick one and give the reason in a few "
             "words, e.g. a quieter part of the day, not right against dinner):"]
    lines += [f"{i}. {s.describe(plan, full)}" for i, s in enumerate(slots, 1)]
    return "\n".join(lines + ([overview] if overview else []))


# --- push everything back (S4) ---

def push_back(tools: "PlanTools", by: int, since: Optional[datetime] = None) -> str:
    """Move every flexible item still to do ``by`` minutes later. Without ``since``, that's everything that started
    no more than ``by`` ago, plus what's happening now: items past their time that weren't marked didn't happen, and
    pushing back is how the user says so. Items that shared time still do; fixed things, work and sleep stay, and an
    item that would land on a fixed thing goes right after it (and so does what follows it). What would run into
    bedtime stays where it was and is named, never dropped."""
    plan = tools.day_plan
    now = tools._now()
    live = plan.day_start <= now < plan.day_end
    delta = timedelta(minutes=by)
    lo = since if since is not None else ((now - delta) if live else plan.day_start)
    facts = tools._facts() if tools._state_db is not None else None

    def daylight(span: EventSpan) -> bool:
        return bool(facts and facts.get(span.id).daylight)

    movable = sorted((s for s in plan.active_spans()
                      if tools._movable(s) and s.status == SpanStatus.PLANNED
                      and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and not tools._is_fixed(s)
                      and (s.start_dt >= lo or (since is None and live and s.start_dt <= now < s.end_dt))),
                     key=lambda s: s.start_dt)
    ids = {s.id for s in movable}
    fixed = [s for s in plan.active_spans() if s.id not in ids and not s.is_envelope and s.status != SpanStatus.SKIPPED
             and tools._is_fixed(s)]
    sleep = [s for s in plan.active_spans() if s.category == SpanCategory.SLEEP]
    placed: Dict[str, Tuple[datetime, datetime]] = {}
    no_room: List[EventSpan] = []
    extra = timedelta(0)
    for span in movable:
        length = span.end_dt - span.start_dt
        want = span.start_dt + delta + extra
        t, end = tools._snap(want, want + length)
        for _ in range(50):
            hit = [f for f in fixed if f.start_dt < end and t < f.end_dt]
            if not hit:
                break
            t, end = tools._snap(max(f.end_dt for f in hit), max(f.end_dt for f in hit) + length)
        extra = max(extra, t - (span.start_dt + delta))
        fits = end <= plan.day_end and not any(b.start_dt < end and t < b.end_dt for b in sleep) \
            and not (daylight(span) and end > plan.sunset)
        if fits:
            placed[span.id] = (t, end)
        else:
            no_room.append(span)
    behind = {s.id for s in movable if s.end_dt <= now}
    if tools._state_db is not None:
        tools._state_db.set_sync_meta(f"redraw_undo:{plan.target_date.isoformat()}", json.dumps(
            {"moved": {s.id: [s.start_dt.isoformat(), s.end_dt.isoformat()] for s in movable if s.id in placed},
             "removed": []}))
    for span in movable:
        if span.id in placed:
            span.start_dt, span.end_dt = placed[span.id]
            tools._persist(span)
    plan.spans.sort(key=lambda s: s.start_dt)
    tools._save_plan_after_change()
    moved = [s for s in movable if s.id in placed]
    if not moved and not no_room:
        return "Nothing to push back: nothing flexible is left to do from then on."
    from thirties_core.clock import agenda_lines
    out = [f"Pushed back {format_duration(by)}:"] + agenda_lines([(s.start_dt, s.end_dt, s.title, "") for s in moved])
    unmarked = [s.title for s in moved if s.id in behind]
    if unmarked:
        out.append("Not marked, so moved along as still to do: " + ", ".join(unmarked) + ".")
    if no_room:
        out.append("Would run into bedtime, so it stays where it was: " + ", ".join(s.title for s in no_room)
                   + (". Plan it tomorrow, or let it go?" if len(no_room) == 1 else ". Plan them tomorrow, or let them go?"))
    return "\n".join(out)


# --- early ticks (soak friction 3) ---

def early_tick_times(plan: DayPlan, span: EventSpan, now: datetime) -> Tuple[datetime, datetime]:
    """When something planned for later is ticked done now, when it (probably) happened: it ended about now, so it
    ends on the block line nearest the tick (a thirty-minute item ticked at 8:06 is 7:30 – 8:00, one block) and keeps
    its planned length. A short one stays inside one block. Items already guessed this way go back to back, the
    newer tick before them, instead of all landing on the same half hour."""
    length = span.end_dt - span.start_dt
    if length >= THIRTY:
        base = thirty_of(now)
        end = base if now - base < timedelta(minutes=15) else base + THIRTY
    else:
        end = now if now - length >= thirty_of(now) else thirty_of(now)
    for _ in range(48):
        start = end - length
        taken = [s for s in plan.spans if s.id != span.id and s.status in (SpanStatus.DONE, SpanStatus.PARTIAL)
                 and s.time_source == "estimated" and not s.is_envelope and s.start_dt < end and start < s.end_dt]
        if not taken:
            return start, end
        end = min(s.start_dt for s in taken)
    return end - length, end
