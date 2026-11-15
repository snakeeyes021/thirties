"""How an item sits in a day, kept by its name like the other lasting facts (item_facts.Lasting):

- **exclusive**: it takes up its time. Nothing else may overlap it, and it may not be put over anything (a show, a
  drive): Thirties never places something across it, and a move that would make them overlap is refused.
- **relies on others**: it happens only when someone else does their part (the sitter feeds the cats). Time passing
  never settles it: it isn't counted done, and it doesn't quietly go "unknown" (kinds.is_moot) the way an ordinary
  fixed item does after a couple of hours. It stays open until someone says what happened.
- **time set by someone else**: a person sets its day and time (a lesson the teacher books). Thirties never places
  it: it isn't offered for open time, isn't put on a day when it's missed, and a redraw or push leaves it where it is.
- **travel**: how long it takes to get there; a "leave by" item goes before it (travel.py).
- **follow-up**: what always comes after it, and how long after ("Put the watch back on", an hour after charging).

The model records them when the user says so (``set_item_traits``); Python applies them everywhere an item is placed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from thirties_core.item_facts import OUTSIDE, Lasting, LastingFacts, item_name

TRAIT_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "set_item_traits",
    "description": "How an item sits in the user's days, kept by its name for every day, when the user says so: "
                   "exclusive (it takes up its time and can't overlap anything, like a show or a drive); relies_on "
                   "(someone else has to do their part, so it's only done when someone says so, never because time "
                   "passed); time_set_by (someone else sets its day and time, so Thirties never schedules it); "
                   "travel (how long it takes to get there: a 'leave by' item is planned before it); follow_up (what "
                   "always comes after it, with follow_after for how long after). Give only what the user said; "
                   "'false' or 'none' clears one.",
    "parameters": {"event": "The item's name (or a planned item's id or title).",
                   "exclusive": "true or false.",
                   "relies_on": "Who it depends on ('the sitter'), 'others' if not said, or 'none'.",
                   "time_set_by": "Who sets its day and time ('Sam'), or 'none'.",
                   "travel": "How long it takes to get there, e.g. '40 minutes' (or 'none').",
                   "follow_up": "What always follows it, e.g. 'Put the watch back on' (or 'none').",
                   "follow_after": "How long after it ends the follow-up comes, e.g. '1 hour' (default: right after)."},
    "required": ["event"],
}]

_OFF = ("", "none", "no", "false", "0", "nobody", "no one", "noone", "clear", "off")


def _lasting(pt) -> Optional[LastingFacts]:
    db = getattr(pt, "_state_db", None)
    return LastingFacts(db) if db is not None else None


def facts_for(pt, span) -> Lasting:
    """Everything that holds for a planned item by its name (or the list item it's linked to); empty without storage."""
    store = _lasting(pt)
    if store is None:
        return Lasting(name=item_name(getattr(span, "title", "")))
    aliases = pt._lasting_aliases(getattr(span, "task_id", None)) if hasattr(pt, "_lasting_aliases") else []
    return store.for_item(span.title, *aliases)


def is_exclusive(pt, span) -> bool:
    if getattr(span, "is_envelope", False) or getattr(span, "source", "") == "calendar":
        return False
    return bool(facts_for(pt, span).exclusive)


def relies_on(pt, span) -> str:
    return facts_for(pt, span).relies_on


def time_set_by(pt, span_or_title, task_id: Optional[str] = None) -> str:
    """Who sets this item's day and time ('' when Thirties may place it)."""
    if isinstance(span_or_title, str):
        store = _lasting(pt)
        if store is None:
            return ""
        aliases = pt._lasting_aliases(task_id) if hasattr(pt, "_lasting_aliases") and task_id else []
        return store.for_item(span_or_title, *aliases).time_set_by
    return facts_for(pt, span_or_title).time_set_by


def stays_put(pt, span) -> bool:
    """Thirties never moves it on its own: fixed (someone else's clock), or its time is set by someone else."""
    return bool(pt._is_fixed(span) or time_set_by(pt, span))


# --- exclusive: nothing overlaps it ---

def overlap_refusal(pt, span, alongside: bool = False) -> Optional[str]:
    """Why ``span`` can't go where it is, because of an exclusive item (it is one, or it lands on one), or None. The
    app's own controls (a drag) are the user's doing and always allowed."""
    if getattr(pt, "by_hand", False) or span.is_envelope or span.status.value != "planned":
        return None
    from thirties_core.clock import format_clock
    others = [s for s in pt.day_plan.spans_overlapping(span.start_dt, span.end_dt)
              if s.id != span.id and not s.is_envelope and s.status.value == "planned"]
    if not others:
        return None
    if is_exclusive(pt, span):
        names = ", ".join(f"{s.title} ({format_clock(s.start_dt)})" for s in others)
        return (f"{span.title} takes up its time (the user said nothing overlaps it), and {names} is there. Put it "
                "where it's clear, or move the other one first.")
    hit = next((s for s in others if is_exclusive(pt, s)), None)
    if hit is not None:
        return (f"{hit.title} ({format_clock(hit.start_dt)} – {format_clock(hit.end_dt)}) takes up its time: nothing "
                f"goes on top of it (the user said so). Put {span.title} before or after it.")
    return None


# --- the tool ---

def set_item_traits(pt, event: Any = None, exclusive: Any = None, relies_on: Any = None, time_set_by: Any = None,
                    travel: Any = None, follow_up: Any = None, follow_after: Any = None, **_: Any) -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import parse_duration_minutes
    store = _lasting(pt)
    if store is None:
        raise ToolError("Facts about items can't be saved here.")
    title = str(event or "").strip()
    if item_name(title) != OUTSIDE:
        try:
            title = pt._find_span(title).title
        except Exception:
            pass  # not planned: the name is as the user said it
    if not item_name(title):
        raise ToolError("Say which item (its name).")
    facts = store.get(title) or Lasting(name=item_name(title))
    facts.title = facts.title or title

    def text(value: Any) -> Optional[str]:
        if value in (None,):
            return None
        v = str(value).strip()
        return "" if v.lower() in _OFF else v

    def minutes(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        if str(value).strip().lower() in _OFF:
            return 0
        try:
            return parse_duration_minutes(value)
        except Exception:
            raise ToolError(f"'{value}' isn't a length, e.g. '40 minutes'.")
    changed = False
    if exclusive not in (None, ""):
        facts.exclusive = str(exclusive).strip().lower() in ("true", "yes", "1") if isinstance(exclusive, str) \
            else bool(exclusive)
        changed = True
    if relies_on not in (None, ""):
        v = text(relies_on)
        facts.relies_on = "others" if v and v.lower() in ("true", "yes", "1", "someone", "someone else") else (v or "")
        changed = True
    if time_set_by not in (None, ""):
        facts.time_set_by = text(time_set_by) or ""
        changed = True
    t = minutes(travel)
    if t is not None:
        facts.travel = t
        changed = True
    if follow_up not in (None, ""):
        facts.follow_up = text(follow_up) or ""
        changed = True
    f = minutes(follow_after)
    if f is not None:
        facts.follow_after = f
        changed = True
    if not changed:
        raise ToolError("Say which trait: exclusive, relies_on, time_set_by, travel or follow_up.")
    store.set(facts)
    out = f"Noted for {facts.title}, every day: " + (", ".join(facts.describe()) or "nothing special") + "."
    if facts.time_set_by:
        out += f" Thirties won't schedule it; it goes on the plan when {facts.time_set_by} gives a time."
    return out


__all__ = ["TRAIT_TOOL_SPECS", "set_item_traits", "is_exclusive", "relies_on", "time_set_by", "stays_put",
           "overlap_refusal", "facts_for"]
