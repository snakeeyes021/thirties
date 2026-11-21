"""A whole routine said done at once: every step of it, in every part, in one go.

People report a routine as a whole far more often than step by step. The done guard (done_reference.py) only lets a
done through for what the user's words point to, and a routine's name points to all of its steps:

- **The routine named as a whole**: "the whole Kiln check is done", "everything in the kiln check", "all the kiln
  check items done": the routine's name, with a word that takes in all of it (all, whole, every, everything,
  entire). Every step of that routine on today's plan is meant, whichever part of the day it sits in.
- **"Both" or "all of them" to a question about its parts**: when the last message the user saw asked about the
  routine by name, "both", "all", "all of them" or "everything" answers for every step of it. When it asked about
  groups of steps instead, listing steps from parts of routines ("did the morning rigging (...) and the shed
  close-up (...) get done?"), the answer takes in every step of those parts: "both" for exactly two parts, "all"
  or "everything" for two or more. Which steps it ticks is the model's to say; the reply names each one
  (reply_checks.picked_unnamed), and a bare no right after it takes them all back (reopen.py).

Nothing else is widened: a step of another routine or part the words never took in, or a list item they never
named, is still refused; a routine said with an exception ("all of it but the sweep", "both, except the bailers")
isn't read as the whole; and running, going or fine is still no done (write_guard.py, started.py: checked after
this). The last check before Joplin takes a whole answer to a question just asked as it takes a plain yes
(answers_the_question).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

# a word that takes in the whole routine, said beside its name
WHOLE_RE = re.compile(r"\b(?:all|whole|entire|every|everything|each)\b", re.IGNORECASE)
# an answer that takes in everything the question asked about
ALL_ANSWER_RE = re.compile(r"^\W*(?:(?:yes|yeah|yep|yup)\W+)?(both|all|everything|every one|the whole (?:thing|lot))\b",
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


def _said_whole(message: Optional[str]) -> bool:
    if not message or EXCEPT_RE.search(message):
        return False
    from thirties_core.done_words import NO_RE
    return not NO_RE.match(message)


def answered(pt, message: Optional[str], shown: str, title: str) -> Optional[str]:
    """The routine ``title`` is a step of, when the user's whole answer ("both", "all of them", "yep, everything")
    takes in a group of steps the last message asked about and this step is in it; None otherwise.

    The message has to ask (a question mark in what the model wrote, not in Python's "Not marked yet" list under
    it). It asks about a routine it names: every step of it. Otherwise each part of a routine it lists a step of is
    a group it asks about, whichever routine the part is in: "both" takes in exactly two such groups, "all" or
    "everything" two or more."""
    m = ALL_ANSWER_RE.search(message or "")
    if not m or not _said_whole(message):
        return None
    from thirties_core.messages import MISSED_HEADING
    asked = (shown or "").split(MISSED_HEADING)[0]
    if "?" not in asked:
        return None
    routines = _routines(pt)
    mine = [r for r in routines if any(s.title == title for s in r["spans"])]
    named = next((r["name"] for r in mine if _names(asked, r["name"])), None)
    if named:
        return named
    from thirties_core.done_reference import _listed
    groups = [(r["name"], p) for r in routines for p in _parts(r["spans"]) if any(_listed(asked, s.title) for s in p)]
    both = m.group(1).lower() == "both"
    if len(groups) < 2 or (both and len(groups) != 2):
        return None
    return next((name for name, p in groups if any(s.title == title for s in p)), None)


def answers_the_question(message: Optional[str]) -> bool:
    """For the last check before Joplin: the user's words are a whole answer ("both", "all of them") to a question
    the reply just before asked (reopen.reply_before), as a plain yes is. Which steps it takes in was judged when the
    done was logged (answered)."""
    from thirties_core.messages import MISSED_HEADING
    from thirties_core.reopen import reply_before
    return bool(ALL_ANSWER_RE.search(message or "")) and _said_whole(message) \
        and "?" in reply_before.get().split(MISSED_HEADING)[0]


def routine_meant(pt, message: Optional[str], shown: str, title: str) -> Optional[str]:
    """The name of the routine the user's words say is done as a whole, when ``title`` is one of its steps on the
    plan; None otherwise."""
    if not _said_whole(message):
        return None
    for routine in _routines(pt):
        if any(s.title == title for s in routine["spans"]) and WHOLE_RE.search(message) \
                and _names(message, routine["name"]):
            return routine["name"]
    return answered(pt, message, shown, title)


__all__ = ["answered", "answers_the_question", "routine_meant"]
