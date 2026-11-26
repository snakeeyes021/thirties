"""The morning hands over the whole day, and what the user asked for that day is on it.

- **The whole day** (11-07: a morning message left the routines out): Python's own
  morning lists it (day_lists.planned_ahead_text); the model's morning reply is checked for each routine step and
  anchor still to come, sent back once if it leaves some out, and given the day's lines if it still does.
- **Asked for, so on it** (10-30, 10-31: an item the user wanted to be sure of on a day wasn't offered): an item pinned to the
  day goes on the day's plan before the morning is written, in the first free time, so it can't be left out.
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def place_pinned(assistant: Any) -> List[str]:
    """Items pinned to today that aren't on its plan yet: each placed now; a full day moves its lowest item on
    (full_day.py), never the pinned one while anything else can go. Returns "Title at 3:00 PM" for each, and the
    lines saying what moved."""
    pt = assistant.pt
    if pt.scheduler is None or not pt.is_current(pt.day_plan):
        return []
    from thirties_core.clock import format_clock, thirty_of
    from thirties_core.full_day import place_or_move
    from thirties_core.horizons import candidates_for_day
    try:
        pinned = [c for c in candidates_for_day(pt, pt.day_plan.target_date, limit=None) if c.source == "pinned"]
    except Exception as e:  # the morning never fails for want of the plans
        logger.warning("Couldn't read what's pinned to today: %s", e)
        return []
    out = []
    for c in pinned:
        try:
            spot, _, moved = place_or_move(assistant, c.title, c.task, protected=True)
        except Exception as e:
            logger.warning("Couldn't place %s: %s", c.title, e)
            continue
        if spot is not None:
            out.append(f"{c.title} at {format_clock(thirty_of(spot))}")  # Thirties picked it: its block
        out += moved
    return out


MARKERS = ("[Morning, not from the user]", "This is the user's first message of the day, and the day is being built")


def is_morning_turn(brain: Any, turn_start: int) -> bool:
    """The turn is the day's morning (the morning instruction, or the first message of the day built in its reply)."""
    for m in brain.messages[max(0, turn_start - 3):]:
        if m.get("role") == "user" and any(k in str(m.get("content") or "") for k in MARKERS):
            return True
    return False


def left_out(brain: Any, reply: str) -> List[str]:
    """What a morning reply leaves out of the day: each routine part (by its first step) and each anchor still to
    come today that the reply doesn't name."""
    from thirties_core.item_facts import mentions
    from thirties_core.models import SpanStatus
    assistant = getattr(brain.tools, "assistant", None)
    if assistant is None:
        return []
    pt = brain.tools
    now = pt._now()
    daily = assistant.routines.daily_titles()
    titles = [s.title for s in pt.day_plan.spans if not s.is_envelope]
    out = []
    for s in sorted(pt.day_plan.spans, key=lambda s: s.start_dt):
        if s.is_envelope or s.status != SpanStatus.PLANNED or s.end_dt <= now:
            continue
        if s.title.strip().lower() in daily and not mentions(reply, s.title, titles):
            out.append(s.title)
    return out


def missing_line(assistant: Any) -> str:
    """Python's lines for the day, added under a morning reply that still omits routine steps or anchors."""
    from thirties_core.day_lists import planned_ahead_text
    try:
        return planned_ahead_text(assistant)
    except Exception:
        return ""


class _Names:
    """A stand-in plan for one_schedule: the titles a morning listing can name (today's rows, every routine and its
    steps, the anchors, what's paused while the user is away), so a row about something not on today's plan is
    still known as a row."""

    def __init__(self, titles: List[str]) -> None:
        from types import SimpleNamespace
        self.spans = [SimpleNamespace(title=t) for t in titles]


def _known_names(assistant: Any) -> List[str]:
    from thirties_core.today import note_items
    out = [s.title for s in assistant.pt.day_plan.spans if not s.is_envelope]
    try:
        for note in assistant._routines():
            out.append(note["title"])
            out += [text.strip() for text, _ in note_items(note)]
        out += [a.title for a in assistant.routines.book().anchors(include_removed=True)]
    except Exception as e:
        logger.warning("Couldn't read the routine names: %s", e)
    try:
        from thirties_core import away_periods
        for p in away_periods.periods(assistant._db):
            out += [x.get("name", "") for x in p.get("paused") or []]
    except Exception as e:
        logger.warning("Couldn't read what's paused: %s", e)
    return [t for t in dict.fromkeys(t.strip() for t in out) if t]


def lists_day(brain: Any, turn_start: int) -> bool:
    """The turn's morning instruction told the model that the app places the day's list (day_proposal.MORNING_FACTS):
    Python places it, then."""
    from thirties_core.day_proposal import DAY_LIST_PLACED
    return any(m.get("role") == "user" and DAY_LIST_PLACED in str(m.get("content") or "")
               for m in brain.messages[max(0, turn_start - 3):])


def _without_listing(assistant: Any, text: str, view: str) -> str:
    """The text without a day listing the model wrote itself (one_schedule.tidy); every known name counts as shown,
    so its copy of any of them goes."""
    if not text.strip():
        return ""
    from thirties_core import one_schedule
    names = _known_names(assistant)
    return one_schedule.tidy(text, "\n".join([view] + names), _Names(names))


def strip_day_list(brain: Any, turn_start: int, reply: str) -> str:
    """Before the reply's claims are checked: a morning reply loses the day listing the model wrote itself, so its
    copied rows are never checked against the plan or sent back for a rewrite (Python's list goes under it at the
    end: place_day_list)."""
    assistant = getattr(getattr(brain, "tools", None), "assistant", None)
    if assistant is None or not reply or not lists_day(brain, turn_start):
        return reply
    try:
        return _without_listing(assistant, reply, missing_line(assistant)) or reply
    except Exception as e:  # never costs the user their morning
        logger.warning("Couldn't take the model's day listing out: %s", e)
        return reply


def place_day_list(brain: Any, turn_start: int, reply: str) -> str:
    """The morning reply with the day's list as Python lists it (day_lists.planned_ahead_text), placed by Python
    under the model's own sentences, exactly once. A listing the model wrote itself goes (one_schedule.tidy), even
    rows about something that isn't on today's plan (a routine step paused for the time away, an earlier day's row):
    the rows the user reads are today's plan. The first morning inside a time away carries its line on top
    (away_periods.say_once)."""
    assistant = getattr(getattr(brain, "tools", None), "assistant", None)
    if assistant is None or not lists_day(brain, turn_start):
        return reply
    from thirties_core.messages import join_sections
    view = missing_line(assistant).strip()
    text = _without_listing(assistant, (reply or "").replace(view, "") if view else (reply or ""), view)
    try:
        from thirties_core.away_periods import say_once
        text = say_once(assistant, assistant._today(), text)
    except Exception as e:  # never costs the user their morning
        logger.warning("Couldn't add the time away line: %s", e)
    return join_sections(text.strip(), view) if view else text.strip()


__all__ = ["place_pinned", "is_morning_turn", "left_out", "missing_line", "strip_day_list", "place_day_list"]
