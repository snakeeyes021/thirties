"""Not yours today: someone else's events (show-only calendars), in a section of their own.

They're never part of the user's schedule: no card among the day's items, no outcome to report, never moved or
planned around. They're listed last, after the day's timed items and If there's time, in the Agenda and in the
schedule the assistant reads ("Not yours today:" then one plain line each), so the user knows what's going on around
them.

Each can be hidden for today (in the app, or "hide that one" in chat): a choice about this one day, nothing else
changes. "Not mine" for a whole recurring series is resolve_calendar_event (it stays available); hiding a whole
calendar is set_calendar_mode, which the guard in intents.py holds back unless the user says the calendar.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from thirties_core.clock import format_clock

HEADING = "Not yours today"
HIDDEN_KEY = "not_yours_hidden:{day}"   # sync meta: the day's hidden events (calendar event ids, else span ids)


def _key(span) -> str:
    return span.source_event_id or span.id


def hidden(db, day) -> List[str]:
    if db is None:
        return []
    try:
        return list(json.loads(db.get_sync_meta(HIDDEN_KEY.format(day=day.isoformat())) or "[]"))
    except ValueError:
        return []


def listed(plan, db=None) -> List[Any]:
    """The day's events that aren't the user's, in time order, minus those hidden for the day."""
    gone = set(hidden(db, plan.target_date))
    return sorted((s for s in plan.spans_in_view() if s.shown_only and s.end_dt > s.start_dt and _key(s) not in gone),
                  key=lambda s: s.start_dt)


def line(span) -> str:
    return f"{span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)}"


def text_for(plan, db=None) -> str:
    """The section for a schedule: last, plain lines."""
    spans = listed(plan, db)
    return (f"\n\n{HEADING}:\n" + "\n".join(line(s) for s in spans)) if spans else ""


def hide(pt, event: Any) -> str:
    """Hide one of them for the day (by id or title): "hide that one". Nothing else changes."""
    from thirties_core.assistant_tools import ToolError
    db = pt._state_db
    plan = pt.day_plan
    spans = listed(plan, db)
    ref = str(event or "").strip().lower()
    found = [s for s in spans if s.id == event or (s.source_event_id and s.source_event_id == event)
             or s.title.strip().lower() == ref]
    if not found and ref:
        words = set(ref.split())
        scored = [(len(words & set(s.title.lower().split())), s) for s in spans]
        best = max((n for n, _ in scored), default=0)
        found = [s for n, s in scored if n and n == best]
    if not found:
        if not spans:
            raise ToolError("Nothing that isn't the user's is on today.")
        raise ToolError("Which one? Not yours today: " + "; ".join(line(s) for s in spans) + ".")
    if len(found) > 1 and len({s.title.strip().lower() for s in found}) > 1:
        raise ToolError("Which one: " + " or ".join(line(s) for s in found) + "?")
    if db is None:
        raise ToolError("Hiding needs the app's storage.")
    key = HIDDEN_KEY.format(day=plan.target_date.isoformat())
    now_hidden = sorted(set(hidden(db, plan.target_date)) | {_key(s) for s in found})
    db.set_sync_meta(key, json.dumps(now_hidden))
    return f"Hidden for today: {found[0].title}. It's back tomorrow; say \"not mine\" to hide it every time."


def ids_text(plan, db=None) -> str:
    """For the Agenda: the ids it shows in the section, one per line ("- id")."""
    return "\n".join(f"- {s.id}" for s in listed(plan, db))


__all__ = ["HEADING", "listed", "line", "text_for", "hide", "hidden", "ids_text"]
