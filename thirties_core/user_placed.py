"""Where the user put an item, it stays: nothing Thirties does by itself moves it.

Seen in simulated days: the user asked for an item at a time ("glaze the mugs at 1, after the clay order"), then
started the item before it early; starting it pushed the item they had placed to an hour and a half later, and they
had to ask for it back. Another day, an item the user said was under way ("the kiln is firing now") was moved to the
afternoon in the same reply that answered a question about when it ends.

Now:

- an item the user placed is known: a time they stated for it (given_times.py), or a move or a new item their
  message asked for by naming it (``note``, kept for the day). Starting something else now (PlanTools._make_room)
  leaves it where it is, alongside, and says so; the user moves it when they want it elsewhere;
- an item under way (the user said they started it) keeps its start: a change to it moves its end or its length,
  never its start, unless the user's message gives the item that new time (``start_refused``).

Nothing here holds the app's own controls or the background (nobody talking).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from thirties_core.clock import format_clock

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

KEY = "user_placed"  # in shared settings: {"YYYY-MM-DD": [span ids]}
KEEP_DAYS = 3


def _db(pt: "PlanTools"):
    return getattr(pt, "_state_db", None)


def _record(pt: "PlanTools") -> dict:
    db = _db(pt)
    if db is None:
        return {}
    try:
        data = json.loads(db.get_shared_setting(KEY) or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _talking(pt: "PlanTools", tool: str) -> Optional[str]:
    from thirties_core.change_scope import _talking as talking
    return talking(pt, tool)


def note(pt: "PlanTools", span: "EventSpan", tool: str) -> None:
    """After ``tool`` (update_event or add_event) placed ``span`` for the user's message: kept as the user's place
    when the message names the item (not a bulk "redo the afternoon", where which items move was the model's)."""
    message = _talking(pt, tool)
    db = _db(pt)
    if message is None or db is None or span.is_envelope or span.source == "calendar":
        return
    from thirties_core.change_scope import BULK_CHANGE_RE, _referred_to
    if BULK_CHANGE_RE.search(message) or not _referred_to(pt, message, pt._title(span), (span.id, span.task_id)):
        return
    day = pt.day_plan.target_date
    data = {d: ids for d, ids in _record(pt).items()
            if isinstance(ids, list) and _recent(d, day)}
    ids = data.setdefault(day.isoformat(), [])
    if span.id not in ids:
        ids.append(span.id)
    db.set_shared_setting(KEY, json.dumps(data, sort_keys=True), datetime.now(timezone.utc).isoformat())


def _recent(iso: str, day) -> bool:
    try:
        from datetime import date
        return abs((date.fromisoformat(iso) - day).days) <= KEEP_DAYS
    except ValueError:
        return False


def placed(pt: "PlanTools", span: "EventSpan") -> bool:
    """The user put this item where it is: a time they stated, or a move or new item they asked for by name."""
    if getattr(span, "given_start", None) is not None and span.given_start.timestamp() == span.start_dt.timestamp():
        return True
    ids = _record(pt).get(pt.day_plan.target_date.isoformat()) or []
    return span.id in ids


def stays_line(pt: "PlanTools", span: "EventSpan") -> str:
    """The note when starting something else leaves this item where the user put it."""
    return (f"{pt._title(span)} stays at {format_clock(span.start_dt)}, alongside: the user put it there; move it "
            "only if they say so")


def start_refused(pt: "PlanTools", span: "EventSpan", new_start: datetime) -> Optional[str]:
    """Why update_event can't move the start of an item under way, or None. Under way: the user said they started
    it (doing_now), and it isn't marked yet. Its start is when they began; only their words give it another."""
    from thirties_core.models import SpanStatus
    message = _talking(pt, "update_event")
    if message is None or span.actual_start is None or span.status != SpanStatus.PLANNED or span.is_envelope:
        return None
    if abs((new_start - span.start_dt).total_seconds()) < 60:
        return None
    from thirties_core.given_times import stated
    if stated(pt, span.title, new_start, any_tool=True):
        return None
    title = pt._title(span)
    return (f"{title} is under way: the user started it at {format_clock(span.actual_start)}, so its start stays. "
            f"Change its end or length if they gave one; if it can't finish in time, say so plainly and leave the "
            f"plan as it is. Move {title} to another time only when the user says that time.")


__all__ = ["note", "placed", "stays_line", "start_refused", "KEY"]
