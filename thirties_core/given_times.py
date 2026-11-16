"""A time the user stated for their own item, kept on it so it's shown to the minute (shown_times.py).

Thirties shows a schedule in blocks, and never shows a minute time it picked itself (AGENTS.md §7, "Natural
units"). When the user names a time for one of their own items as they place it ("put the parcel drop at 4:45",
"move the call to 5:10"), that time is theirs: the item keeps it as ``given_start`` (and ``given_end`` for an end
they named), it's stored and synced with the item, and every view shows it as said.

Whether the user said it is read from their message this turn, as the done check reads it (done_reference.py):
one clause of the message names the item (item_facts.mentions) and gives that very time (facts.gave_time, or the
current minute when they say they're starting it now). Everything else is a time Thirties chose and is never kept:

- a step put after another one, a routine's parts, a day proposal, a carried-over item, an estimate: any placing
  that starts from another tool (put_routine, propose_day...), whatever the message says;
- a change with nobody talking (the background, the app's own buttons);
- a time moved off what the user said (a short item stacked after others in its thirty, a window kept).

The kept time is the time itself, not a flag: once the item moves for any reason, it no longer matches and
stops being shown to the minute.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

# the tools a user's own placing goes through ("" is a direct call: a test or the app's core)
_PLACING = ("", "add_event", "update_event")


# where one thing said ends and the next begins: "the parcel drop is at 4:45, fit the ferns in somewhere" gives
# 4:45 to the parcel, not the ferns (a full stop, but never the one in "p.m.")
_CLAUSE_RE = re.compile(r"[;!?\n]|,\s|(?<![ap]\.m)\.(?:\s|$)|\b(?:and|then|but|also|plus)\b", re.IGNORECASE)


def stated(pt: "PlanTools", title: str, dt: Optional[datetime]) -> bool:
    """Whether the user's message this turn names this item and gives this time for it, in the same breath."""
    message = getattr(pt, "user_message", None)
    if not message or dt is None or getattr(pt, "by_hand", False):
        return False
    from thirties_core.plan_log import outer_tool
    if outer_tool() not in _PLACING:
        return False
    from thirties_core.facts import gave_time
    from thirties_core.item_facts import mentions
    from thirties_core.plan_tools import _NOW_SAID_RE
    now = pt._now().replace(second=0, microsecond=0)
    others = [s.title for s in pt.day_plan.spans]
    for clause in _CLAUSE_RE.split(message):
        said = gave_time(clause, dt) or (dt.replace(second=0, microsecond=0) == now and _NOW_SAID_RE.search(clause))
        if said and mentions(clause, title, others):
            return True
    return False


def keep(pt: "PlanTools", span: Any, start_named: bool, end_named: bool) -> None:
    """After placing or moving ``span``: keep the start (and end) the user stated for it, when they named one;
    drop any kept time that a time named by the model this turn replaces."""
    if start_named:
        span.given_start = span.start_dt if stated(pt, span.title, span.start_dt) else None
    if end_named:
        span.given_end = span.end_dt if stated(pt, span.title, span.end_dt) else None


__all__ = ["keep", "stated"]
