"""Where things go, decided with judgement: Python lists the options, the model picks.

Three jobs, all deterministic (AGENTS.md §7.3: Python works out the facts and the options; the model chooses):

- ``slot_options``: for a new item with no time, the candidate slots, each with what's around it and how full that
  part of the day is (B8, S5: a crowded afternoon next to an open evening). "Later" means a real
  gap later on, never the next free block (10-24/10-25: an item for the evening became 5:00, then 7:00, then 9:30).
- ``push_back``: pushing everything back by N hours moves every flexible item still to do, including the one happening
  now and the ones past their time that nobody marked (S4, 10-30: what's past its time and unmarked
  is part of what's being pushed back).
- ``early_tick_times``: when something is ticked done before its time, the times Thirties guesses for it keep to
  blocks, and several early ticks go back to back (soak friction 3: two 9:00 PM items ticked at 8:06 were both
  stored 7:36 – 8:06, across two thirties).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
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
    from thirties_core.breathing import clip  # room after work: not where Thirties puts things
    saved = tools.day_plan
    tools.day_plan = plan
    try:
        return clip(plan, tools.open_windows(after, before))
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


# --- making way: push everything back, and the rest moving along when something runs long ---

def inside_fixed(tools: "PlanTools", plan: DayPlan, span: EventSpan) -> Optional[EventSpan]:
    """The fixed event this item sits inside (a lesson's parts inside the lesson), or None: what's inside a fixed
    event happens with it, so it never moves on its own."""
    for f in plan.active_spans():
        if f.id == span.id or f.is_envelope or f.status == SpanStatus.SKIPPED or not tools._is_fixed(f):
            continue
        if f.start_dt <= span.start_dt and span.end_dt <= f.end_dt and f.end_dt - f.start_dt > span.end_dt - span.start_dt:
            return f
    return None


def flexible(tools: "PlanTools", plan: DayPlan, span: EventSpan) -> bool:
    """Thirties may move it on its own: a planned activity or task, not fixed, not timed by someone else, not inside
    a fixed event."""
    from thirties_core.traits import time_set_by
    return (tools._movable(span) and span.status == SpanStatus.PLANNED
            and span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and not tools._is_fixed(span)
            and not time_set_by(tools, span) and inside_fixed(tools, plan, span) is None)


@dataclass
class Flow:
    placed: Dict[str, Tuple[datetime, datetime]]  # what moves, to where
    no_room: List[EventSpan]                     # would run into bedtime (or past daylight): stays where it was
    kept: List[EventSpan]                        # in the way of nothing: kept its time (there was room)
    meals_kept: List[EventSpan]                  # meals that stayed put while the rest went round them
    late: List[str]                              # a meal that had to go past its window
    dark: List[EventSpan] = field(default_factory=list)  # of no_room: those that only ran past daylight, not bedtime


def make_way(tools: "PlanTools", plan: DayPlan, chain: List[EventSpan], seed: Dict[str, timedelta],
             wall: Optional[EventSpan] = None, wall_was: Optional[Tuple[datetime, datetime]] = None,
             clear_after: timedelta = timedelta(0)) -> Flow:
    """Where each item of ``chain`` (flexible items, in start order) goes when the day ahead moves along.

    ``seed`` gives the items that move by a set amount (pushing back: what's behind or first in line). ``wall`` is an
    item that runs long (still doing it): it now ends later than ``wall_was``. Everything else moves only as far as
    it has to: each keeps up to half an hour of the room it had before it, and the rest of a gap absorbs the push, so
    what's far enough off keeps its time (11-09: the rest could stay where it was). Fixed things, what's inside them and
    what someone else times never move; what lands on them goes right after. A meal stays roughly where it is: the
    rest goes round it, and it moves only when the thing running long covers it, never past its window (10-31:
    lunch and dinner moved by the whole push). Lengths are kept. What would run into bedtime stays where it was.
    ``clear_after``: what moves past the wall starts at least this long after it (breathing room after work)."""
    from thirties_core.meals import late_line, window as meal_window
    from thirties_core.traits import is_exclusive, time_set_by
    facts = tools._facts() if tools._state_db is not None else None
    ids = {s.id for s in chain}
    sleep = [s for s in plan.active_spans() if s.category == SpanCategory.SLEEP]
    walls = [s for s in plan.active_spans() if s.id not in ids and not s.is_envelope and s.status == SpanStatus.PLANNED
             and (tools._is_fixed(s) or is_exclusive(tools, s) or time_set_by(tools, s))
             and (wall is None or s.id != wall.id)]
    if wall is not None:
        walls.append(wall)
    out = Flow({}, [], [], [], [])

    def daylight(span: EventSpan) -> bool:
        return bool(facts and facts.get(span.id).daylight)

    def hits(t: datetime, end: datetime, obstacles) -> List:
        return [o for o in obstacles if o[0] < end and t < o[1]]

    obstacles = [(w.start_dt, w.end_dt + (clear_after if w is wall else timedelta(0)), w.id) for w in walls]
    # meals first: in place, unless what runs long covers them (then just past it, inside their window)
    meals = [s for s in chain if s.id not in seed and meal_window(s.title, plan.day_start) is not None]
    for meal in meals:
        length = meal.end_dt - meal.start_dt
        t, end = meal.start_dt, meal.end_dt
        if wall is not None and wall.start_dt < end and t < wall.end_dt:
            closes = meal_window(meal.title, plan.day_start)[1]
            t, end = tools._snap(wall.end_dt, wall.end_dt + length)
            if end > closes:
                out.late.append(late_line(meal.title, closes, f"{wall.title} is still going"))
        if (t, end) != (meal.start_dt, meal.end_dt):
            out.placed[meal.id] = (t, end)
        else:
            out.meals_kept.append(meal)
        obstacles.append((t, end, meal.id))
    shifts: Dict[str, timedelta] = {m.id: (out.placed[m.id][0] - m.start_dt if m.id in out.placed else timedelta(0))
                                    for m in meals}
    front_was = wall_was[1] if wall_was else None   # where the moving run ended, as planned
    front_now = wall.end_dt if wall is not None else None  # and where it ends now
    done: List[EventSpan] = list(meals)
    for span in chain:
        if span.id in shifts:
            continue
        s0, e0 = span.start_dt, span.end_dt
        length = e0 - s0
        if span.id in seed:
            shift = seed[span.id]
            window = meal_window(span.title, plan.day_start)
            if window is not None and e0 + shift > window[1]:  # a meal behind its time moves, but no further than needs be
                shift = max(min(shift, max(window[1] - e0, tools._now() - s0)), timedelta(0))
        else:
            partner = next((p for p in reversed(done) if p.start_dt <= s0 < p.end_dt), None)
            if partner is None and wall is not None and wall_was and wall.start_dt <= s0 < wall_was[1]:
                shift = timedelta(0)  # it started during what runs long: it goes on alongside it
            elif partner is not None:
                shift = shifts[partner.id]  # it shares time with the one before it: they move together
            elif front_was is None or front_now is None:
                shift = timedelta(0)
            else:
                room = max(s0 - front_was, timedelta(0))
                need = front_now + min(room, BREATHING_ROOM)
                if wall is not None and clear_after and s0 >= (wall_was[1] if wall_was else wall.end_dt):
                    need = max(need, wall.end_dt + clear_after)
                shift = max(need - s0, timedelta(0))
        t, end = s0 + shift, e0 + shift
        if shift > timedelta(0):
            t, end = tools._snap(t, end)
            for _ in range(50):
                hit = hits(t, end, obstacles)
                if not hit:
                    break
                t, end = tools._snap(max(h[1] for h in hit), max(h[1] for h in hit) + length)
        before_bed = end <= plan.day_end and not any(b.start_dt < end and t < b.end_dt for b in sleep)
        fits = before_bed and not (daylight(span) and end > plan.sunset)
        if not fits:
            out.no_room.append(span)
            if before_bed:
                out.dark.append(span)  # it needs daylight: that's what it would run past, not bedtime
            t, end = s0, e0
        elif (t, end) != (s0, e0):
            out.placed[span.id] = (t, end)
        elif front_now is not None:
            out.kept.append(span)
        shifts[span.id] = t - s0
        done.append(span)
        front_was = max(front_was, e0) if front_was else e0
        front_now = max(front_now, end) if front_now else end
    return out


def apply_flow(tools: "PlanTools", plan: DayPlan, flow: Flow, extra_undo: Optional[Dict[str, Tuple[datetime, datetime]]] = None) -> List[EventSpan]:
    """Put the moves on the plan, keeping what was there for undo (undo_redraw). Returns the moved items."""
    moved = [s for s in plan.spans if s.id in flow.placed]
    if tools._state_db is not None:
        before = {s.id: [s.start_dt.isoformat(), s.end_dt.isoformat()] for s in moved}
        for sid, (a, b) in (extra_undo or {}).items():
            before[sid] = [a.isoformat(), b.isoformat()]
        tools._state_db.set_sync_meta(f"redraw_undo:{plan.target_date.isoformat()}",
                                      json.dumps({"moved": before, "removed": []}))
    for span in moved:
        span.start_dt, span.end_dt = flow.placed[span.id]
        tools._persist(span)
    plan.spans.sort(key=lambda s: s.start_dt)
    tools._save_plan_after_change()
    return sorted(moved, key=lambda s: s.start_dt)


def plain_lines(spans: List[EventSpan]) -> List[str]:
    """What moved, by thirties as the agenda lays it out, as plain lines (time first, never '- ' bullets)."""
    from thirties_core.clock import agenda_lines
    return [line[2:] if line.startswith("- ") else line.strip()
            for line in agenda_lines([(s.start_dt, s.end_dt, s.title, "") for s in spans])]


def flow_lines(flow: Flow, moved: List[EventSpan]) -> List[str]:
    """The reply's lines about what else happened: kept, meals, no room."""
    out = []
    if flow.kept:  # the push was absorbed by a gap: from there on, the day is as it was
        out.append(f"There was room before {flow.kept[0].title}, so it and what follows keep their times.")
    if flow.meals_kept and moved:
        names = " and ".join(s.title for s in flow.meals_kept)
        out.append(f"{names} stays where it was; the rest goes around it." if len(flow.meals_kept) == 1 else
                   f"{names} stay where they were; the rest goes around them.")
    out += flow.late
    dark = [s for s in flow.no_room if s in flow.dark]
    bed = [s for s in flow.no_room if s not in flow.dark]
    for spans, why in ((bed, "Would run into bedtime"), (dark, "Needs daylight and would run past sunset")):
        if spans:
            out.append(f"{why}, so " + ("it stays where it was: " if len(spans) == 1 else "they stay where they were: ")
                       + ", ".join(s.title for s in spans)
                       + (". Plan it for another day, or let it go?" if len(spans) == 1 else
                          ". Plan them for another day, or let them go?"))
    return out


def push_back(tools: "PlanTools", by: int, since: Optional[datetime] = None) -> str:
    """Move the day ahead ``by`` minutes later. Without ``since``, from the thirty that was ``by`` ago on (whole
    blocks: 10-31, a late wake-up and a one-hour push left Feed fish at 8:00 behind Make coffee at 8:06), plus what's
    happening now: items past their time that weren't marked didn't happen, and pushing back is how the user says so.
    Those, and the first thing in line, move by the full amount; what follows moves only as far as it has to
    (make_way): fixed things and what's inside them, work and sleep stay, meals stay roughly in place, and a gap
    later in the day absorbs the push. What would run into bedtime stays where it was and is named, never dropped."""
    plan = tools.day_plan
    now = tools._now()
    live = plan.day_start <= now < plan.day_end
    delta = timedelta(minutes=by)
    lo = since if since is not None else (thirty_of(now - delta) if live else plan.day_start)
    chain = sorted((s for s in plan.active_spans() if flexible(tools, plan, s)
                    and (s.start_dt >= lo or (since is None and live and s.start_dt <= now < s.end_dt))),
                   key=lambda s: s.start_dt)
    if not chain:
        return "Nothing to push back: nothing flexible is left to do from then on."
    behind = {s.id for s in chain if live and s.start_dt <= now}
    from thirties_core.meals import window as meal_window
    first = min((s.start_dt for s in chain if s.id not in behind and meal_window(s.title, plan.day_start) is None),
                default=None)  # the first thing in line (a meal ahead stays roughly where it is)
    seed = {s.id: delta for s in chain if s.id in behind or (not behind and s.start_dt == first)}
    flow = make_way(tools, plan, chain, seed)
    moved = apply_flow(tools, plan, flow)
    if not moved and not flow.no_room:
        return "Nothing to push back: nothing flexible is left to do from then on."
    out = [f"Pushed back {format_duration(by)}:"] + plain_lines(moved)
    unmarked = [s.title for s in moved if s.id in behind and s.end_dt - delta <= now]
    if unmarked:
        out.append("Not marked, so moved along as still to do: " + ", ".join(unmarked) + ".")
    return "\n".join(out + flow_lines(flow, moved))


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


# --- out of work: on an on-site day, what Thirties placed inside work moves out by itself (on_site.py) ---

def _routine_name(tools: "PlanTools", span: EventSpan) -> str:
    """The routine a planned step belongs to ("" for anything else)."""
    a = getattr(tools, "assistant", None)
    if a is not None:
        try:
            found = a._routine_of(span.title)
        except Exception:
            found = None
        if found is not None:
            return found[0]["title"]
    return "routine" if span.source == "routine" else ""


def _placed_by_thirties(tools: "PlanTools", span: EventSpan) -> bool:
    """A planned item whose time Thirties chose: not a calendar event, not fixed or timed by someone else, not a
    time the user stated for it."""
    from thirties_core.traits import time_set_by
    return (span.status == SpanStatus.PLANNED and span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
            and span.source != "calendar" and not span.tentative and not span.is_firm and not span.shown_only
            and span.given_start is None and not tools._is_fixed(span) and not time_set_by(tools, span))


def _units(tools: "PlanTools", plan: DayPlan, works: List[EventSpan], now: datetime) -> Tuple[List[List[EventSpan]], List[EventSpan]]:
    """What has to come out of work, as units that move together (a routine's steps back to back are one unit), and
    what stays though it's inside work because its time isn't Thirties' to change."""
    from thirties_core.meals import meal_word

    def inside(s: EventSpan) -> bool:
        return any(w.start_dt < s.end_dt and s.start_dt < w.end_dt for w in works)

    def going(s: EventSpan) -> bool:  # still ahead, and not what the user is doing right now before work
        if s.end_dt <= now:
            return False
        return s.start_dt >= now or any(w.start_dt <= now < w.end_dt for w in works)

    ahead = sorted((s for s in plan.spans if s.status == SpanStatus.PLANNED and not s.is_envelope and going(s)
                    and meal_word(s.title) is None and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)),
                   key=lambda s: s.start_dt)
    kept = [s for s in ahead if inside(s) and s.source != "calendar" and not _placed_by_thirties(tools, s)]
    movable = [s for s in ahead if _placed_by_thirties(tools, s)]
    chains: List[List[EventSpan]] = []
    for s in movable:
        name = _routine_name(tools, s)
        last = chains[-1] if chains else None
        if (name and last and _routine_name(tools, last[0]) == name
                and s.start_dt <= max(x.end_dt for x in last)):
            last.append(s)  # the next step of the same routine, back to back: they go together
        else:
            chains.append([s])
    return [c for c in chains if any(inside(s) for s in c)], kept


def _free_start(plan: DayPlan, busy: List[Tuple[datetime, datetime]], length: timedelta, near: datetime,
                lo: datetime, hi: datetime, daylight: bool) -> Optional[datetime]:
    """The free start on a thirty nearest ``near`` where ``length`` fits between ``lo`` and ``hi`` with nothing in
    ``busy`` (in daylight when it needs it)."""
    best = None
    t = thirty_ceil(lo)
    while t + length <= hi:
        end = t + length
        ok = not any(a < end and t < b for a, b in busy)
        if ok and daylight:
            ok = plan.sunrise <= t and end <= plan.sunset
        if ok and (best is None or abs(t - near) < abs(best - near)):
            best = t
        t += THIRTY
    return best


def _busy(plan: DayPlan, leaving: set, after: datetime) -> List[Tuple[datetime, datetime]]:
    return [(s.start_dt, s.end_dt) for s in plan.spans if s.id not in leaving and s.status != SpanStatus.SKIPPED
            and not s.shown_only and s.end_dt > after]


def out_of_work(tools: "PlanTools", plan: DayPlan, works: List[EventSpan]) -> List[str]:
    """An on-site day: move what Thirties placed inside work out, by itself (no asking). Each unit (one item, or a
    routine's steps back to back) goes to the free time nearest where it was, before or after work, keeping its
    length, its order inside and the thirties; with no room left today, a later day this week by fit, never
    tomorrow by itself (not_today.py; where a daily thing already has its own time tomorrow, today's simply doesn't
    happen). What the user timed, fixed things and meals stay. One plain
    line for each, saying where it went."""
    from thirties_core.if_time import _waking_end
    from thirties_core.replan import outdoors as needs_daylight  # by its wording, or the user said so
    now = tools._now()
    live = plan.day_start <= now < plan.day_end
    lo = max(plan.day_start, now) if live else plan.day_start
    units, kept = _units(tools, plan, works, now if live else plan.day_start)
    lines: List[str] = []
    if not units and not kept:
        return lines
    leaving = {s.id for u in units for s in u}
    busy = _busy(plan, leaving, lo)
    hi = _waking_end(plan)
    changed = False
    tomorrow = None
    for unit in units:
        first, last = unit[0].start_dt, max(s.end_dt for s in unit)
        length = last - first
        names = ", ".join(s.title for s in unit)
        daylight = any(needs_daylight(s.title, tools) for s in unit)
        t = _free_start(plan, busy, length, first, lo, hi, daylight)
        if t is not None:
            shift = t - first
            for s in unit:
                s.start_dt, s.end_dt = s.start_dt + shift, s.end_dt + shift
                tools._persist(s)
                busy.append((s.start_dt, s.end_dt))
            changed = True
            side = "before work" if t + length <= min(w.start_dt for w in works) else "after work"
            lines.append(f"{names}: now {format_clock(t)}, {side}.")
            continue
        # nothing fits today: a later day, except what's due today or overdue, which stays (due_today.py)
        from thirties_core.due_today import deadline_words, held_due
        due = [d for d in (held_due(tools, s.title, s.task_id) for s in unit) if d is not None]
        if due:
            lines.append(f"{names}: {deadline_words(min(due), tools.today())} and no free time outside work is "
                         "left, so it stays at "
                         f"{format_clock(first)}, during work.")
            continue
        if tomorrow is None:
            tomorrow = tools.plan_for(plan.target_date + timedelta(days=1))
        there = [s for s in unit if any(o.status == SpanStatus.PLANNED and o.title.strip().lower() ==
                                        s.title.strip().lower() for o in tomorrow.spans)]
        if len(there) == len(unit):  # a daily thing: tomorrow has its own
            for s in unit:
                s.status = SpanStatus.SKIPPED
                tools._persist(s)
            changed = True
            lines.append(f"{names}: no free time outside work today, so not today; it's on tomorrow's plan as usual.")
            continue
        # --- a later day this week by fit, or the day the user named; never tomorrow by itself (not_today.py) ---
        from thirties_core.not_today import day_words, where_to
        day, _ = where_to(tools, unit[0].title, int(length.total_seconds() // 60), plan.target_date)
        later = tools.plan_for(day)
        when = day_words(day, plan.target_date)
        # --- end later day ---
        t2 = _free_start(later, _busy(later, set(), later.day_start), length,
                         first + timedelta(days=(day - plan.target_date).days),
                         later.day_start, _waking_end(later), daylight)
        if t2 is None:
            lines.append(f"{names}: no free time outside work today or {when}, so it stays where it was.")
            continue
        try:
            for s in unit:
                tools._move_to_day(s, later.target_date.isoformat(), format_clock(t2 + (s.start_dt - first)),
                                   None, f"{int((s.end_dt - s.start_dt).total_seconds() // 60)} minutes", None)
        except Exception:
            lines.append(f"{names}: no free time outside work today, and it couldn't go on {when}, so it stays "
                         "where it was.")
            continue
        changed = True
        lines.append(f"{names}: no free time outside work today, so {when} at {format_clock(t2)}.")
    from thirties_core.traits import time_set_by
    for s in kept:
        who = time_set_by(tools, s)
        why = "you gave it that time" if s.given_start is not None else f"{who} sets its time" if who else \
            "it stays put"
        lines.append(f"{s.title} stays at {format_clock(s.start_dt)}, during work: {why}.")
    if changed:
        plan.spans.sort(key=lambda s: s.start_dt)
        tools._save_plan_after_change()
    return lines
