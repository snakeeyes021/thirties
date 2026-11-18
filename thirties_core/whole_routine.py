"""A whole routine said done at once: every step of it, in every part, in one go.

People report a routine as a whole far more often than step by step. The done guard (done_reference.py) only lets a
done through for what the user's words point to, and a routine's name points to all of its steps:

- **The routine named as a whole**: "the whole Kiln check is done", "everything in the kiln check", "all the kiln
  check items done": the routine's name, with a word that takes in all of it (all, whole, every, everything,
  entire). Every step of that routine on today's plan is meant, whichever part of the day it sits in.
- **"Both" or "all of them" to a question about its parts**: when the last message the user saw asked about the
  routine (named it, or listed its steps from two or more of its parts), "both", "all", "all of them" or
  "everything" answers for every step of it.

Nothing else is widened: a step of another routine, or a list item the words never named, is still refused; a
routine said with an exception ("all of it but the sweep") isn't read as the whole; and running, going or fine is
still no done (write_guard.py, started.py: checked after this).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

# a word that takes in the whole routine, said beside its name
WHOLE_RE = re.compile(r"\b(?:all|whole|entire|every|everything|each)\b", re.IGNORECASE)
# an answer that takes in everything the question asked about
ALL_ANSWER_RE = re.compile(r"^\W*(?:(?:yes|yeah|yep|yup)\W+)?(?:both|all|everything|every one|the whole (?:thing|lot))\b",
                           re.IGNORECASE)
# an exception: then it isn't the whole routine
EXCEPT_RE = re.compile(r"\b(?:except|but not|apart from|other than|minus|save for)\b", re.IGNORECASE)
# steps further apart than this on the plan are in different parts of the day
PART_GAP = timedelta(minutes=30)


def _routines(pt) -> List[Dict[str, Any]]:
    """Each routine with its step spans on the day's plan: [{"name", "spans"}]."""
    assistant = getattr(pt, "assistant", None)
    if assistant is None or getattr(assistant, "_db", None) is None:
        return []
    from thirties_core.step_times import key as step_key
    try:
        notes = assistant._routines()
        tools = assistant.routines
        book = tools.book()
    except Exception:
        return []
    out = []
    for note in notes:
        name = str(note.get("title") or "").strip()
        if not name:
            continue
        try:
            parts = book.get(note["id"], name).parts
        except Exception:
            parts = []
        names = {s.lower() for s in tools.steps(note)} | {step_key(i) for p in parts for i in p.get("items", [])}
        prefix = name.lower()

        def own(title: str) -> str:  # "Kiln check › Vent the room" is the step Vent the room
            low = title.strip().lower()
            for sep in (" › ", ": "):
                if low.startswith(prefix + sep):
                    return low[len(prefix) + len(sep):]
            return low
        spans = [s for s in pt.day_plan.spans if not getattr(s, "is_envelope", False)
                 and not getattr(s, "shown_only", False) and step_key(own(s.title)) in names]
        if spans:
            out.append({"name": name, "spans": spans})
    return out


def _parts(spans: list) -> List[list]:
    """The routine's steps in parts of the day: a run of steps close together is one part."""
    parts: List[list] = []
    for s in sorted(spans, key=lambda x: x.start_dt):
        if parts and s.start_dt - max(x.end_dt for x in parts[-1]) <= PART_GAP:
            parts[-1].append(s)
        else:
            parts.append([s])
    return parts


def _names(text: str, name: str) -> bool:
    from thirties_core.item_facts import name_words
    mine = name_words(name)
    return bool(mine) and mine <= name_words(text)


def _asked_about(shown: str, routine: Dict[str, Any]) -> bool:
    """The last message the user saw asks about this routine: names it, or lists its steps from two parts."""
    if "?" not in (shown or ""):
        return False
    if _names(shown, routine["name"]):
        return True
    from thirties_core.done_reference import _listed
    listed = [p for p in _parts(routine["spans"]) if any(_listed(shown, s.title) for s in p)]
    return len(listed) >= 2


def routine_meant(pt, message: Optional[str], shown: str, title: str) -> Optional[str]:
    """The name of the routine the user's words say is done as a whole, when ``title`` is one of its steps on the
    plan; None otherwise."""
    if not message or EXCEPT_RE.search(message):
        return None
    from thirties_core.done_words import NO_RE
    if NO_RE.match(message):
        return None
    for routine in _routines(pt):
        if not any(s.title == title for s in routine["spans"]):
            continue
        if WHOLE_RE.search(message) and _names(message, routine["name"]):
            return routine["name"]
        if ALL_ANSWER_RE.search(message) and _asked_about(shown, routine):
            return routine["name"]
    return None


__all__ = ["routine_meant"]
