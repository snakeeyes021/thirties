"""Choosing what a calendar is to the user, in chat ("my partner's calendar is show-only", "ignore the Holidays
calendar"): the same per-calendar choice as Preferences → Calendars (``calendar.modes``, keyed by the calendar's
lowercased display name; calendar_engine.calendar_mode reads it).

The model only passes the name and the choice it heard. Python matches the name against the calendars it can
see, exactly (case aside): a calendar's name is the key on every device, so a guess would set a choice nobody
can find again. Anything else comes back with the list, for the model to ask which one. Choices:

- ``mine``: the user's commitments, planned around;
- ``shared``: a household's calendar (everyone's, the user's too): on the user's plan, marked shared
  (others_events.py; a calendar named like "Family" is asked about per series until the user chooses);
- unset, the default: someone else's events are left out of the user's day, shown as theirs, and asked about only
  when the user is invited themselves;
- ``ask``: Thirties asks about each event of someone else's calendar, once per series, naming the calendar;
- ``show``: show only, on the Agenda as someone else's, taking no time, never asked about;
- ``ignore``: never read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from thirties_core.calendar_engine import CALENDAR_MODES, events_to_spans
from thirties_core.connectors import calendars_of

CALENDAR_MODE_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "set_calendar_mode",
    "description": "What one of the user's calendars is to them, when they say so (\"my partner's calendar is "
                   "show-only\", \"ignore the Holidays calendar\", \"that's my partner's calendar\"). mine: their commitments; "
                   "shared: everyone's in the house, theirs too; ask: ask about each event; default: someone else's events are left out unless the user is invited (the starting "
                   "choice); show: show only, takes no time; ignore: never read. Use the calendar's exact name; "
                   "leave calendar out to list them.",
    "parameters": {"calendar": "The calendar's exact name (leave out to list the calendars and their choices).",
                   "mode": "mine, shared, ask, default, show, ignore, or holidays (shown only; one on a work day "
                           "means no work). Leave out when only saying whose it is.",
                   "whose": "Who the calendar belongs to, in the user's words (\"my partner\", \"Sam\"), when they say."},
    "required": [],
}]

# The words a user (or a model) uses for each choice; "" is "ask" (unset).
_MODE_WORDS = {
    "mine": "mine", "my own": "mine", "own": "mine",
    "shared": "shared", "ours": "shared", "family": "shared", "everyone's": "shared", "household": "shared",
    "ask": "ask", "ask me": "ask", "unset": "", "default": "", "leave out": "", "left out": "",
    "show": "show", "show only": "show", "show-only": "show", "shown only": "show", "show_only": "show",
    "not mine": "show", "view only": "show",
    "ignore": "ignore", "hide": "ignore", "ignored": "ignore", "never": "ignore", "off": "ignore",
    "holidays": "holidays", "holiday": "holidays", "days off": "holidays",
}

_LABEL = {"mine": "mine", "shared": "shared, everyone's (on your plan)", "": "left out unless you're invited", "ask": "ask about its events", "show": "show only",
          "ignore": "ignored",
          "holidays": "holidays (no work on them)"}


class CalendarModeError(ValueError):
    pass


def known_calendars(scheduler, plan=None) -> List[Dict[str, Any]]:
    """The calendars Python can see: the connector's own list (name, mine, mode), or else the calendar names on
    the day's events (the mock and connectors with no list), plus any already chosen for."""
    engine = getattr(scheduler, "calendar_engine", None)
    found = list(calendars_of(engine)) if engine is not None else []
    names = {c["name"].strip().lower() for c in found}
    modes = dict(getattr(scheduler.config.calendar, "modes", None) or {})
    extra: List[str] = []
    if not found and plan is not None:
        try:
            extra = [e.calendar_name for e in scheduler.fetch_calendar_events(plan) if e.calendar_name]
        except Exception:
            extra = []
    for name in extra + list(modes):
        if name.strip().lower() not in names:
            names.add(name.strip().lower())
            found.append({"name": name, "mine": False, "mode": modes.get(name.strip().lower(), "")})
    for c in found:  # the stored choice is the truth (the connector may have read the config before a change)
        c["mode"] = modes.get(c["name"].strip().lower(), c.get("mode", ""))
    return found


def _listing(calendars: List[Dict[str, Any]]) -> str:
    return "; ".join(f"{c['name']} ({_LABEL.get(c['mode'] or ('mine' if c.get('mine') else ''), c['mode'])})"
                     for c in calendars)


def set_calendar_mode(scheduler, settings, calendar: Any = None, mode: Any = None, plan=None,
                      whose: Any = None) -> str:
    """Set (or, with no calendar, list) a calendar's choice. Raises CalendarModeError for anything unclear."""
    calendars = known_calendars(scheduler, plan)
    if not calendars:
        raise CalendarModeError("No calendars can be seen here, so there's nothing to choose for. The user can "
                                "add one in GNOME Settings → Online Accounts.")
    if calendar in (None, ""):
        return "Calendars: " + _listing(calendars) + "."
    name = str(calendar).strip()
    match = [c for c in calendars if c["name"].strip().lower() == name.lower()]
    if not match:
        near = [c for c in calendars if any(w in c["name"].lower() for w in name.lower().split() if len(w) > 2)]
        raise CalendarModeError(f"No calendar is called '{name}'. "
                                + (f"Maybe: {_listing(near)}. " if near else "")
                                + f"The calendars are: {_listing(calendars)}. Ask the user which one, then use its "
                                  f"exact name.")
    cal = match[0]
    said = ""
    if whose not in (None, "") and getattr(scheduler, "state_db", None) is not None:
        from thirties_core.others_events import possessive, set_owner
        said = f"{cal['name']}: {possessive(set_owner(scheduler.state_db, cal['name'], str(whose)))} calendar, from now on."
        if mode in (None, ""):
            return said
    word = str(mode or "").strip().lower()
    if word not in _MODE_WORDS:
        raise CalendarModeError(f"'{mode}' isn't a choice: mine, ask, default, show (show only) or ignore.")
    key = _MODE_WORDS[word]
    if key in ("", "ask") and cal.get("mine"):
        raise CalendarModeError(f"{cal['name']} is the user's own calendar, so its events already count as theirs; "
                                f"'{word}' is for other people's calendars. Choose mine, show or ignore.")
    assert key in CALENDAR_MODES or key == ""
    modes = dict(getattr(scheduler.config.calendar, "modes", None) or {})
    lowered = cal["name"].strip().lower()
    if key:
        modes[lowered] = key
    else:
        modes.pop(lowered, None)
    scheduler.config.calendar.modes = modes
    if settings is not None:
        settings.set("calendar.modes", modes)
    return ((said + " ") if said else "") + \
        f"{cal['name']}: {_LABEL[key or ('mine' if cal.get('mine') else '')]} from now on (on every device)."


def refresh_calendar_spans(tools) -> None:
    """Redraw the open day's calendar events under the new choices (the stored plan never holds them)."""
    sched, plan = tools.scheduler, tools.day_plan
    if sched is None:
        return
    events = sched.fetch_calendar_events(plan)
    spans, pending = events_to_spans(events, sched.state_db.get_event_decisions(),
                                     getattr(sched.config.calendar, "modes", None))
    from thirties_core.calendar_outcomes import CalendarOutcomes
    spans = [s for s in spans if s.overlaps(plan.day_start, plan.day_end)]
    CalendarOutcomes(sched.state_db).apply(spans)
    plan.spans = sorted([s for s in plan.spans if s.source != "calendar"] + spans, key=lambda s: s.start_dt)
    tools.ambiguous_events = pending


__all__ = ["CALENDAR_MODE_TOOL_SPECS", "CalendarModeError", "known_calendars", "set_calendar_mode",
           "refresh_calendar_spans"]
