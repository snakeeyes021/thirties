"""A time the user stated for their own item, kept on it so it's shown to the minute (shown_times.py).

Thirties shows a schedule in blocks, and never shows a minute time it picked itself (AGENTS.md §7, "Natural
units"). When the user names a time for one of their own items as they place it ("put the parcel drop at 4:45",
"move the call to 5:10"), that time is theirs: the item keeps it as ``given_start`` (and ``given_end`` for an end
they named), it's stored and synced with the item, and every view shows it as said.

Whether the user said it is read from their message this turn, as the done check reads it (done_reference.py):
one clause of the message is about the item and gives that very time (facts.gave_time). A clause is about the item
when it names it (item_facts.mentions; "the parcel and the letters at 4:45" names both), has a word of its name and
names no other item outright ("the afternoon stroll" for Stroll by the canal beside another stroll: stated_times.py),
or points back to it alone ("put it at 4:45" right after a message that named only that item). "Right now" names
no time: the item starts at the current minute under the hood, and shows in its block like any time Thirties worked
out; the time now is said apart ("It's 12:37"). A shift the user asks for ("an hour later") moves a stated time
with the item: they anchored it. Everything else is a time Thirties chose and is never kept:

- a step put after another one, a routine's parts, a day proposal, a carried-over item, an estimate: any placing
  that starts from another tool (put_routine, propose_day...), whatever the message says, except a step's own time
  the user gave for it this turn (stated_times.keep_step_time);
- a change with nobody talking (the background, the app's own buttons);
- a time moved off what the user said (a short item stacked after others in its thirty, a window kept).

The kept time is the time itself, not a flag: once the item moves for any reason, it no longer matches and
stops being shown to the minute.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

# the tools a user's own placing goes through ("" is a direct call: a test or the app's core)
_PLACING = ("", "add_event", "update_event")


# where one thing said ends and the next begins: "the parcel drop is at 4:45, fit the ferns in somewhere" gives
# 4:45 to the parcel, not the ferns (a full stop, but never the one in "p.m.")
_CLAUSE_RE = re.compile(r"[;!?\n]|,\s|(?<![ap]\.m)\.(?:\s|$)|\b(?:then|but|also)\b", re.IGNORECASE)
# items said together: "the parcel and the letters at 4:45" gives both that time; "the ferns at 4 and the parcel at
# 4:45" gives each its own
_AND_RE = re.compile(r"\b(?:and|plus)\b", re.IGNORECASE)
# a pointer back at one item ("put it at 4:45", "move that to 5:10")
_ONE_RE = re.compile(r"\b(?:that one|this one|that|this|it)\b", re.IGNORECASE)


def _clauses(message: str) -> List[str]:
    """The message cut where one thing said ends; items joined by "and" stay together up to the time they share."""
    from thirties_core.facts import _SAID_TIME_RE
    out: List[str] = []
    for part in _CLAUSE_RE.split(message):
        pending: List[str] = []
        for piece in _AND_RE.split(part):
            pending.append(piece)
            if _SAID_TIME_RE.search(piece):
                out.append(" and ".join(pending))
                pending = []
        if pending:
            out.append(" and ".join(pending))
    return out


def _talking(pt: "PlanTools", any_tool: bool = False) -> Optional[str]:
    """The user's message, when this change is the user's own placing of an item (any_tool: whichever tool is
    placing it, for a time the user gave that another tool applies, stated_times.py); else None."""
    message = getattr(pt, "user_message", None)
    if not message or getattr(pt, "by_hand", False):
        return None
    from thirties_core.plan_log import outer_tool
    return message if any_tool or outer_tool() in _PLACING else None


def _about(pt: "PlanTools", clause: str, title: str) -> bool:
    """Whether this clause is about the item: it names it, or points back ("it", "that") to it alone, as a done
    reads a pointer (done_reference.pointed_back: the one item the last message shown, or the user's previous
    message, named), with no other item named in the clause."""
    from thirties_core.item_facts import mentions
    titles = [s.title for s in pt.day_plan.spans if s.title]
    if mentions(clause, title, titles):
        return True
    from thirties_core.stated_times import loosely_named  # "the afternoon stroll" beside another stroll
    if loosely_named(clause, title, titles):
        return True
    if not _ONE_RE.search(clause) or any(mentions(clause, t, titles) for t in titles if t != title):
        return False
    from thirties_core.done_reference import pointed_back
    return set(pointed_back(pt, titles)) == {title}


def stated(pt: "PlanTools", title: str, dt: Optional[datetime], any_tool: bool = False) -> bool:
    """Whether the user's message this turn gives this time for this item, in the same breath."""
    message = _talking(pt, any_tool)
    if message is None or dt is None:
        return False
    from thirties_core.facts import gave_time
    for clause in _clauses(message):
        if gave_time(clause, dt) and _about(pt, clause, title):
            return True
    return False


def keep(pt: "PlanTools", span: Any, start_named: bool, end_named: bool, was: Optional[tuple] = None) -> None:
    """After placing or moving ``span``: keep the start (and end) the user stated for it, when they named one;
    drop any kept time that a time named by the model this turn replaces. ``was``: its (start, end) before a shift
    ("an hour later"): a time the user stated moves with it when the user asked for the shift, since they anchored
    it; any other shift leaves it to its blocks."""
    if was is not None:
        old_start, old_end = was
        delta = span.start_dt - old_start
        message = _talking(pt)
        asked = message is not None and span.end_dt - old_end == delta and \
            any(_about(pt, clause, span.title) for clause in _clauses(message))
        span.given_start = span.given_start + delta if asked and _still(span.given_start, old_start) else None
        span.given_end = span.given_end + delta if asked and _still(span.given_end, old_end) else None
        return
    if start_named:
        span.given_start = span.start_dt if stated(pt, span.title, span.start_dt) else None
    if end_named:
        span.given_end = span.end_dt if stated(pt, span.title, span.end_dt) else None


def _still(given: Optional[datetime], at: datetime) -> bool:
    return given is not None and given.timestamp() == at.timestamp()


__all__ = ["keep", "stated"]
