"""What Profile → Routines shows and changes: each routine (its repeat schedule, its placement, whether it's
skipped) and each daily anchor, read and written through the same core as the assistant's tools (RoutineBook,
RoutineTools.set_routine / set_anchor / skip_routine), so the page and the chat can never disagree.

The page (src/views/routines_page.py) only lays these rows out. Everything here is plain data, for the page and
for tests without a display.

Decisions:
- Editing a routine's placement on the page counts as the user's own (placement_source "user", the same as
  set_routine parts): it stays until they change it, and placing it differently one day doesn't overwrite it.
- "Offer the defaults again" lists the default anchors (lunch, dinner) that aren't active now, each with an Add;
  it doesn't re-arm the one-time chat offer (the page is where the user is choosing already).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.routines import (DEFAULT_ANCHORS, RoutineTools, _clock_of, _day_of, describe_repeat, is_due,
                                    next_due)
from thirties_core.week import DAYS, describe_days

# The repeat choices, in the page's order: (key, label). "days" takes a number, "weekdays" a set of days.
REPEAT_KINDS = [("day", "Every day"), ("days", "Every few days"), ("weekdays", "On some weekdays"),
                ("work_days", "On work days"), ("days_off", "On days off"), ("asked", "Only when asked")]
PLACEMENT_SOURCES = {"user": "Set by you", "learned": "Learned from how you last placed it",
                     "history": "Read from your recent plans", "": "No usual time yet: place it once and it's kept"}


def repeat_text(kind: str, n: Any = None, days: Optional[List[str]] = None) -> str:
    """The words set_routine reads (routines.parse_repeat) for a choice on the page."""
    if kind == "days":
        return f"every {max(2, int(n or 2))} days"
    if kind == "weekdays":
        chosen = [d for d in DAYS if d in set(days or [])]
        if not chosen:
            raise ValueError("Pick at least one day.")
        return " and ".join(chosen)
    return {"day": "every day", "work_days": "work days", "days_off": "days off", "asked": "on request"}[kind]


def _skip_state(r, today: date) -> str:
    snoozed, skipped = _day_of(r.snoozed_until), _day_of(r.skipped_on)
    if snoozed and snoozed > today:
        return ("Skipped today" if (snoozed - today).days == 1 else f"Skipped until {snoozed.strftime('%a %b %-d')}")
    if skipped and skipped >= today:
        return f"Skipped this time ({skipped.strftime('%a %b %-d')})"
    return ""


def routine_rows(routines: RoutineTools) -> List[Dict[str, Any]]:
    """Each routine: title, steps, repeat (key, n, days, words), placement (parts with clock times and fallbacks,
    where it came from), skip state, and whether it's due today / when next."""
    if routines.db is None:
        return []
    book, today, gen = routines.book(), routines.a._today(), routines.gen
    out = []
    for note in routines.a._routines():
        r = book.get(note["id"], note["title"])
        done = routines.last_done(note, r)
        parts, source = routines.parts_for(note, r, today)
        rep = r.schedule
        upcoming = next_due(r, today - timedelta(days=1), gen, done)
        out.append({
            "note_id": note["id"], "title": note["title"], "steps": routines.steps(note),
            "repeat": {"kind": rep.get("every", "day"), "n": int(rep.get("n", 2) or 2),
                       "days": list(rep.get("days") or []), "text": describe_repeat(rep)},
            "parts": [{"items": list(p["items"]), "time": _clock_of(p["at"]), "fallback": p.get("fallback", "")}
                      for p in parts],
            "placement_source": source, "placement_text": PLACEMENT_SOURCES.get(source, source),
            "skip": _skip_state(r, today),
            "due_today": is_due(r, today, gen, done),
            "next": upcoming.strftime("%a %b %-d") if upcoming else "",
            "last_done": done.strftime("%a %b %-d") if done else "",
        })
    return out


def summary(row: Dict[str, Any]) -> str:
    """One line under a routine's name: how often, when, and whether it's skipped."""
    bits = [row["repeat"]["text"].capitalize()]
    if row["parts"]:
        bits.append(" and ".join(p["time"] for p in row["parts"]))
    if row["skip"]:
        bits.append(row["skip"])
    elif not row["due_today"] and row["next"]:
        bits.append(f"next {row['next']}")
    return " · ".join(bits)


def length_text(minutes: int) -> str:
    """'15 min', '1 hour', '1 h 30 min': how long, as the page shows it."""
    h, m = divmod(int(minutes), 60)
    if not h:
        return f"{m} min"
    return (f"{h} hour" + ("s" if h > 1 else "")) if not m else f"{h} h {m} min"


def anchor_rows(routines: RoutineTools) -> List[Dict[str, Any]]:
    if routines.db is None:
        return []
    return [{"title": a.title, "time": _clock_of(a.at), "minutes": a.minutes, "fixed": a.fixed, "days": list(a.days),
             "days_text": describe_days(a.days) if a.days else "every day",
             "summary": f"{_clock_of(a.at)} · {length_text(a.minutes)} · "
                        f"{describe_days(a.days) if a.days else 'every day'}" + (" · fixed" if a.fixed else "")}
            for a in routines.book().anchors()]


def default_offers(routines: RoutineTools) -> List[Dict[str, Any]]:
    """The default anchors that aren't on now (never added, removed, or declined): offered again on the page."""
    have = {a["title"].strip().lower() for a in anchor_rows(routines)}
    return [{"title": t, "time": _clock_of(at), "minutes": m} for t, at, m in DEFAULT_ANCHORS if t.lower() not in have]


# --- changes: all through the tools' own functions ---

def save_repeat(routines: RoutineTools, title: str, kind: str, n: Any = None, days: Optional[List[str]] = None) -> str:
    return routines.set_routine(routine=title, repeat=repeat_text(kind, n, days))


def save_placement(routines: RoutineTools, title: str, parts: List[Dict[str, Any]]) -> str:
    """parts: [{"time": "9:00 AM", "items": [...], "fallback": "..."}]; empty parts are dropped."""
    clean = [{"start": p["time"], "items": [i.strip() for i in p["items"] if i.strip()],
              "fallback": (p.get("fallback") or "").strip()} for p in parts
             if str(p.get("time") or "").strip() and any(i.strip() for i in p.get("items") or [])]
    return routines.set_routine(routine=title, parts=clean)


def resume(routines: RoutineTools, title: str) -> str:
    """Take back a skip: the routine is due again by its schedule."""
    note = routines._note(title)
    book = routines.book()
    r = book.get(note["id"], note["title"])
    r.snoozed_until, r.skipped_on = "", ""
    book.save(r)
    return f"{note['title']} isn't skipped any more."


def save_anchor(routines: RoutineTools, title: str, time: str, minutes: int, fixed: bool, days: Any = None) -> str:
    return routines.set_anchor(title=title, time=time, duration=f"{int(minutes)} minutes", fixed=bool(fixed),
                               days=days if days not in (None, "", []) else "every day")


def remove_anchor(routines: RoutineTools, title: str) -> str:
    return routines.set_anchor(title=title, remove=True)


__all__ = ["REPEAT_KINDS", "PLACEMENT_SOURCES", "repeat_text", "routine_rows", "summary", "anchor_rows",
           "default_offers", "length_text", "save_repeat", "save_placement", "resume", "save_anchor", "remove_anchor"]
