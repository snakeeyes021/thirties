"""A routine part's steps stay together, whatever kind each step is stored as.

A step the routine's note has no line for (one the user always places with the routine, or one added for the day)
is stored as a plain activity with no list item behind it, while the note's own steps are tasks linked to its lines.
Everything that asked "which routine is this step of?" looked only at the note's lines, so such a step was never
part of its part: the day list gave it a row of its own beside the others, and moving the part left it behind.

What a routine's step is, is now answered in one place (``members``): the note's lines, the steps the routine's
usual placement carries, and the steps placed with it on the day (``record``, kept when a part is put on the plan).
A part is the steps put together (the record), or those sitting in the same thirty.

- **Rows** (day_lists.py): a part is one row, steps in order.
- **Moves**: naming any step of a part in put_routine, or moving one in update_event, takes its whole part along
  (``widen``, ``move_along``), unless the user wants that one step on its own (``alone``).
- **Repair** (``regroup``, on each build of the day): steps of a part that got split apart on the day come back
  together.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from thirties_core.clock import format_clock, thirty_of
from thirties_core.models import EventSpan, SpanStatus
from thirties_core.step_times import clean_title, key as step_key

logger = logging.getLogger(__name__)

HALF = timedelta(minutes=30)


# --- what was placed together on a day ---

def _meta(day: date) -> str:
    return f"routine_parts:{day.isoformat()}"


def _load(db: Any, day: date) -> Dict[str, List[List[str]]]:
    if db is None:
        return {}
    try:
        data = json.loads(db.get_sync_meta(_meta(day)) or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def record(db: Any, day: date, note_id: str, parts: List[Dict[str, Any]]) -> None:
    """The parts just put on ``day`` for this routine: each is the steps placed together (by name). A step named
    again leaves the part it was in."""
    if db is None:
        return
    new = [[step_key(str(i)) for i in (p.get("items") or []) if str(i).strip()] for p in parts]
    new = [p for p in new if p]
    if not new:
        return
    data = _load(db, day)
    named = {k for p in new for k in p}
    kept = [[k for k in p if k not in named] for p in data.get(note_id, [])]
    data[note_id] = [p for p in kept if p] + new
    db.set_sync_meta(_meta(day), json.dumps(data))


def detach(db: Any, day: date, note_id: str, name: str) -> None:
    """A step moved on its own is a part of its own from then on."""
    data = _load(db, day)
    mine = step_key(name)
    parts = data.get(note_id)
    if not parts or not any(mine in p and len(p) > 1 for p in parts):
        return
    data[note_id] = [[k for k in p if k != mine] for p in parts]
    data[note_id] = [p for p in data[note_id] if p] + [[mine]]
    db.set_sync_meta(_meta(day), json.dumps(data))


def placed_with(db: Any, day: date, note_id: str) -> Set[str]:
    return {k for p in _load(db, day).get(note_id, []) for k in p}


# --- which routine a step belongs to ---

def members(assistant: Any, note: Dict[str, Any], day: Optional[date] = None) -> Set[str]:
    """The routine's steps by name (lowercased, no leading time): its note's lines, the steps its usual placement
    carries that the note doesn't list, and the ones placed with it on ``day``."""
    tools = assistant.routines
    keys = {step_key(s) for s in tools.steps(note)}
    try:
        keys |= set(tools.known_extras(note))
    except Exception as e:
        logger.debug("No usual extras for %s: %s", note.get("title"), e)
    if day is not None:
        keys |= placed_with(tools.db, day, note["id"])
    return keys


def owners(assistant: Any, day: date) -> Dict[str, str]:
    """Each step name (and its wording as in the note) to the routine it belongs to."""
    out: Dict[str, str] = {}
    from thirties_core.today import note_items
    for note in assistant._routines():
        title = str(note.get("title") or "").strip()
        for text, _ in note_items(note):
            out.setdefault(text.strip().lower(), title)
        for k in members(assistant, note, day):
            out.setdefault(k, title)
    return out


def _routine_of(pt: Any, span: EventSpan, day: date) -> Optional[Tuple[Dict[str, Any], Set[str]]]:
    a = getattr(pt, "assistant", None)
    if a is None or getattr(a, "_db", None) is None:
        return None
    mine = step_key(pt._title(span))
    for note in a._routines():
        keys = members(a, note, day)
        if mine in keys:
            return note, keys
    return None


def _on_plan(pt: Any, plan: Any, keys: Set[str]) -> List[EventSpan]:
    return [s for s in plan.spans if not s.is_envelope and s.source != "calendar" and not s.shown_only
            and s.status == SpanStatus.PLANNED and step_key(pt._title(s)) in keys]


def mates(pt: Any, span: EventSpan, plan: Any = None) -> List[EventSpan]:
    """The other steps of the part this step is in, still to do on the plan: the ones put with it, and the ones in
    its thirty. Empty for something that isn't a routine step."""
    plan = plan or pt.day_plan
    if span.is_envelope or span.status != SpanStatus.PLANNED or span.source == "calendar":
        return []
    try:
        found = _routine_of(pt, span, plan.target_date)
    except Exception as e:
        logger.debug("Couldn't tell which routine %s is in: %s", span.title, e)
        return []
    if found is None:
        return []
    note, keys = found
    mine = step_key(pt._title(span))
    together = next((set(p) for p in _load(pt.assistant.routines.db, plan.target_date).get(note["id"], [])
                     if mine in p), set())
    block = thirty_of(span.start_dt)
    return [s for s in _on_plan(pt, plan, keys)
            if s.id != span.id and (thirty_of(s.start_dt) == block or step_key(pt._title(s)) in together)]


# --- moves ---

def widen(pt: Any, note: Dict[str, Any], plan: Any, parts: Any, alone: bool = False) -> Any:
    """put_routine moving a part that's on the plan: the part's other steps still to do go with the ones named,
    unless the user wants those on their own (``alone``). Steps not on the plan yet change nothing."""
    if alone or getattr(pt.assistant.routines, "placing", False) or not isinstance(parts, list):
        return parts
    named = {step_key(str(i)) for p in parts if isinstance(p, dict) for i in (p.get("items") or [])}
    if not named:
        return parts
    keys = members(pt.assistant, note, plan.target_date)
    on_plan = _on_plan(pt, plan, keys)
    out = []
    for part in parts:
        items = part.get("items") if isinstance(part, dict) else None
        if not items:
            out.append(part)
            continue
        added: List[str] = []
        for text in items:
            span = next((s for s in on_plan if step_key(pt._title(s)) == step_key(str(text))), None)
            for mate in mates(pt, span, plan) if span is not None else []:
                k = step_key(pt._title(mate))
                if k not in named:
                    named.add(k)
                    added.append(clean_title(mate.title))
        out.append({**part, "items": list(items) + added} if added else part)
    return out


def _moved_this_turn(pt: Any) -> Set[str]:
    message = getattr(pt, "user_message", None)
    kept = getattr(pt, "_part_moved", None)
    if not kept or kept[0] is not message:
        kept = (message, set())
        pt._part_moved = kept
    return kept[1]


def together_for(pt: Any, span: EventSpan, alone: bool) -> List[EventSpan]:
    """The steps that go along when this one is moved by the model for the user: its part's. Nothing for a move the
    user made by hand (a drag moves one step), one asked on its own, or a step that already moved with its part
    this turn."""
    from thirties_core.move_scope import _talking
    if alone or _talking(pt) is None or span.id in _moved_this_turn(pt):
        return []
    return mates(pt, span)


def after_move(pt: Any, span: EventSpan, together: List[EventSpan], was_start: Any) -> List[str]:
    """The part goes along by as much as the step moved (``together`` read before it moved); a step that moved
    without them is a part of its own on the day. Returns what to say."""
    plan = pt.day_plan
    found = None
    try:
        found = _routine_of(pt, span, plan.target_date)
    except Exception:
        return []
    if found is None:
        return []
    note = found[0]
    if not together:
        try:
            detach(pt.assistant.routines.db, plan.target_date, note["id"], pt._title(span))
        except Exception as e:
            logger.debug("Couldn't keep the step as a part of its own: %s", e)
        return []
    # the part goes by whole thirties, each step keeping its place inside the thirty (so the part stays in order
    # whichever of its steps was the one moved); a step with a time the user gave keeps it
    delta = thirty_of(span.start_dt) - thirty_of(was_start)
    if not delta:
        return []
    moved = []
    for m in together + [span]:
        if m is span and span.given_start is not None:
            continue
        origin = m.start_dt if m is not span else was_start
        length = m.end_dt - m.start_dt
        start = origin + delta
        end = start + length
        if start < plan.day_start or end > plan.day_end:
            continue  # it can't go there: it stays, and the next build puts it back with its part
        m.start_dt, m.end_dt = start, end
        pt._persist(m)
        _moved_this_turn(pt).add(m.id)
        if m is not span:
            moved.append(m)
    if not moved:
        return []
    plan.spans.sort(key=lambda s: s.start_dt)
    try:  # the part's usual place is where the whole of it went
        pt.assistant.routines.learn(note, [{"start": format_clock(thirty_of(span.start_dt)),
                                            "items": [clean_title(s.title) for s in [span] + moved]}])
    except Exception as e:
        logger.debug("Routine placement not learned from a part's move: %s", e)
    return [f"{', '.join(clean_title(m.title) for m in moved)} moved with it, as one part"]


# --- repair ---

def regroup(assistant: Any, plan: Any) -> int:
    """Steps of a part that sit in different thirties on the day (split apart) go back to where most of the part is.
    A part is the steps put together on the day; for a day with no record of that, the routine's usual parts, but
    then only a step the note has no line for is brought back (the stored kind that got left behind). Returns how
    many steps moved."""
    pt = assistant.pt
    db = assistant.routines.db
    if db is None:
        return 0
    moved = 0
    for note in assistant._routines():
        keys = members(assistant, note, plan.target_date)
        on_plan = {step_key(pt._title(s)): s for s in _on_plan(pt, plan, keys)}
        if len(on_plan) < 2:
            continue
        lines = {step_key(s) for s in assistant.routines.steps(note)}
        recorded = _load(db, plan.target_date).get(note["id"])
        if recorded:
            groups = [(set(p), set(p)) for p in recorded]
        else:
            usual = assistant.routines.book().get(note["id"], note["title"]).parts
            groups = [({step_key(i) for i in p.get("items", [])}, {step_key(i) for i in p.get("items", [])} - lines)
                      for p in usual]
        for part, free in groups:
            here = [on_plan[k] for k in part if k in on_plan]
            blocks: Dict[Any, int] = {}
            for s in here:
                blocks[thirty_of(s.start_dt)] = blocks.get(thirty_of(s.start_dt), 0) + 1
            if len(blocks) < 2:
                continue
            anchor = min(blocks, key=lambda b: (-blocks[b], b))
            cursor = max(s.end_dt for s in here if thirty_of(s.start_dt) == anchor)
            for s in sorted(here, key=lambda s: s.start_dt):
                if thirty_of(s.start_dt) == anchor or step_key(pt._title(s)) not in free or s.given_start is not None:
                    continue
                length = s.end_dt - s.start_dt
                start = max(anchor, min(cursor, anchor + HALF - length))
                s.start_dt, s.end_dt = start, start + length
                cursor = s.end_dt
                pt._persist(s)
                moved += 1
    if moved:
        plan.spans.sort(key=lambda s: s.start_dt)
    return moved
