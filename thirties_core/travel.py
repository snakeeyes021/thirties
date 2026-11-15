"""Items Thirties plans around other items: a "leave by" item before something away from home, and a follow-up
after something that needs one.

- **Leave by**: "I have to head out for the concert at 6:15". A "Leave for <it>" item runs from the time to leave until it
  starts: that time is the drive, so it takes up its time (nothing else is planned in it, and what was there moves
  along). Its start is the time the user said, or the travel time the user gave for the item (traits.travel) before
  its start. With a travel time on record, Thirties puts it on by itself each day the item is on the plan.
- **Follow-up**: "add an item to take the bread out after it". An item right after another (or some time after it),
  tied to it: it comes after it whenever the day is redrawn, and moves along when it runs long. A follow-up the
  user always wants (traits.follow_up) is put on by itself after each one.

What Thirties puts on by itself is put on once: if the user takes it off, it stays off (``PLANNED_KEY``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from thirties_core.clock import format_clock, format_duration, thirty_of
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

FOLLOW_UP_MINUTES = 5  # a follow-up with no usual length: a quick thing ("put the watch back on")
PLANNED_KEY = "planned_around:{kind}:{span_id}"

TRAVEL_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "leave_by",
        "description": "Plan when the user has to leave for something (\"I have to head out for the concert at 6:15\"): a "
                       "'Leave for ...' item from then until it starts, which nothing else overlaps. Give at (the "
                       "time to leave) or travel (how long getting there takes).",
        "parameters": {"event": "The item they're going to (id or title).",
                       "at": "The time to leave, if said.",
                       "travel": "How long getting there takes, e.g. '40 minutes', if said instead."},
        "required": ["event"],
    },
    {
        "name": "follow_up",
        "description": "Plan something that has to come after an item (\"remind me to put the watch back on after "
                       "it charges\"): a follow-up item right after it ends, or after a while. It stays after it "
                       "when the day changes. For one that always follows, use set_item_traits with follow_up.",
        "parameters": {"event": "The item it follows (id or title).",
                       "title": "What the follow-up is, e.g. 'Put the watch back on'.",
                       "after": "How long after it ends, e.g. '1 hour' (default: right after).",
                       "duration": "How long the follow-up takes (default: its usual length, or a few minutes)."},
        "required": ["event", "title"],
    },
]


def _db(tools):
    return getattr(tools, "_state_db", None)


def _once(tools, kind: str, span_id: str) -> bool:
    """True the first time Thirties plans this by itself (and records it); False after."""
    db = _db(tools)
    if db is None:
        return True
    key = PLANNED_KEY.format(kind=kind, span_id=span_id)
    if db.get_sync_meta(key):
        return False
    db.set_sync_meta(key, tools._now().isoformat())
    return True


def _leave_title(span: EventSpan) -> str:
    return f"Leave for {span.title}"


def _add(tools: "PlanTools", span: EventSpan, fixed: bool, after: Optional[EventSpan] = None) -> None:
    plan = tools.day_plan
    plan.spans.append(span)
    plan.spans.sort(key=lambda s: s.start_dt)
    tools._persist(span)
    if tools._state_db is not None:
        store = tools._facts()
        facts = store.get(span.id)
        facts.anchored = facts.anchored or fixed
        if after is not None:
            facts.after = sorted(set(facts.after) | {after.id})
        store.set(span.id, facts)
    tools._save_plan_after_change()


def plan_leave(tools: "PlanTools", target: EventSpan, leave_at: datetime) -> str:
    """Put a "Leave for" item on from ``leave_at`` until ``target`` starts, moving along what it covers."""
    from thirties_core.assistant_tools import ToolError
    from thirties_core.placement import apply_flow, flexible, make_way
    if leave_at >= target.start_dt:
        raise ToolError(f"Leaving at {format_clock(leave_at)} is after {target.title} starts "
                        f"({format_clock(target.start_dt)}).")
    plan = tools.day_plan
    existing = next((s for s in plan.spans if s.title == _leave_title(target) and s.status == SpanStatus.PLANNED), None)
    skip = {target.id} | ({existing.id} if existing is not None else set())
    # what's in the way, worked out before the drive is on the plan (what's inside a fixed thing never moves)
    chain = sorted((s for s in plan.active_spans() if s.id not in skip and flexible(tools, plan, s)
                    and s.end_dt > leave_at), key=lambda s: s.start_dt)
    if existing is not None:
        was = (existing.start_dt, existing.end_dt)
        existing.start_dt, existing.end_dt = leave_at, target.start_dt
        leave = existing
        tools._persist(leave)
    else:
        was = None
        leave = EventSpan(f"leave-{target.id}"[:40], _leave_title(target), leave_at, target.start_dt,
                          SpanCategory.ACTIVITY, source="user")
        _add(tools, leave, fixed=True)
    flow = make_way(tools, plan, chain, {}, wall=leave, wall_was=(leave_at, leave_at))
    moved = apply_flow(tools, plan, flow, extra_undo={leave.id: was} if was else None)
    out = f"{leave.title} at {format_clock(leave_at)} ({format_duration(int((target.start_dt - leave_at).total_seconds() // 60))} to get there)."
    if moved:
        out += " Moved along: " + "; ".join(f"{s.title} to {format_clock(s.start_dt)}" for s in moved) + "."
    return out


def leave_by(tools: "PlanTools", event: Any = None, at: Any = None, travel: Any = None, **_: Any) -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import parse_duration_minutes
    from thirties_core.traits import facts_for
    target = tools._find_span(event)
    if target.is_envelope:
        raise ToolError("Say what they're leaving for (the item on the plan).")
    if at not in (None, ""):
        leave_at = tools._start(at)
    else:
        minutes = parse_duration_minutes(travel) if travel not in (None, "") else facts_for(tools, target).travel
        if not minutes:
            raise ToolError("Give the time to leave (at) or how long getting there takes (travel).")
        leave_at = target.start_dt - timedelta(minutes=minutes)
    return plan_leave(tools, target, leave_at)


def follow_up(tools: "PlanTools", event: Any = None, title: Any = None, after: Any = None, duration: Any = None,
              **_: Any) -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import parse_duration_minutes
    lead = tools._find_span(event)
    name = str(title or "").strip()
    if not name:
        raise ToolError("Say what the follow-up is.")
    gap = timedelta(minutes=parse_duration_minutes(after)) if after not in (None, "") else timedelta(0)
    minutes = parse_duration_minutes(duration) if duration not in (None, "") else 0
    span = _place_follow_up(tools, lead, name, gap, minutes)
    return f"Added {name} at {format_clock(span.start_dt)}, after {lead.title} [id {span.id}]."


def _place_follow_up(tools: "PlanTools", lead: EventSpan, name: str, gap: timedelta, minutes: int = 0,
                     span_id: Optional[str] = None) -> EventSpan:
    from thirties_core.models import new_span_id
    usual = 0
    lasting = tools._lasting() if tools._state_db is not None else None
    if lasting is not None:
        usual = lasting.for_item(name).minutes
    length = timedelta(minutes=minutes or usual or FOLLOW_UP_MINUTES)
    ended = (lead.actual_end if lead.status in (SpanStatus.DONE, SpanStatus.PARTIAL) and lead.actual_end
             else lead.end_dt)
    start = max(ended + gap, thirty_of(tools._now()) if ended + gap < tools._now() else ended + gap)
    if length >= timedelta(minutes=30):
        start, _ = tools._snap(start, start + length)
    span = EventSpan(span_id or new_span_id(), name, start, start + length, SpanCategory.ACTIVITY, source="user")
    _add(tools, span, fixed=False, after=lead)
    return span


# --- what Thirties puts on by itself (each check) ---

def plan_around(tools: "PlanTools", plan, now: Optional[datetime] = None) -> List[str]:
    """For the day being lived: a "Leave for" item before each item with a travel time on record that's still
    ahead, and the follow-up after each item that always has one. Each once."""
    from thirties_core.traits import facts_for
    if tools._state_db is None:
        return []
    now = now or tools._now()
    out: List[str] = []
    saved = tools.day_plan
    tools.day_plan = plan
    try:
        titles = {s.title for s in plan.spans}
        for span in list(plan.spans):
            if span.is_envelope or span.status == SpanStatus.SKIPPED or span.tentative or span.shown_only:
                continue
            facts = facts_for(tools, span)
            if facts.travel and span.status == SpanStatus.PLANNED and _leave_title(span) not in titles \
                    and span.start_dt - timedelta(minutes=facts.travel) > now and _once(tools, "leave", span.id):
                out.append(plan_leave(tools, span, span.start_dt - timedelta(minutes=facts.travel)))
            if facts.follow_up and span.start_dt <= now + timedelta(hours=12) and _once(tools, "follow", span.id):
                if not any(s.title.strip().lower() == facts.follow_up.strip().lower() and s.start_dt >= span.start_dt
                           for s in plan.spans):
                    made = _place_follow_up(tools, span, facts.follow_up, timedelta(minutes=facts.follow_after),
                                            span_id=f"fu-{span.id}"[:40])
                    out.append(f"{made.title} at {format_clock(made.start_dt)}, after {span.title}")
    finally:
        tools.day_plan = saved
    return out


__all__ = ["TRAVEL_TOOL_SPECS", "leave_by", "follow_up", "plan_leave", "plan_around", "FOLLOW_UP_MINUTES"]
