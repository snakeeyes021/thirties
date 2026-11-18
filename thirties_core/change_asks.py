"""An item comes off the plan only on the user's words for it, read as for a done (done_reference.py).

Before, one removal word anywhere in a message was enough ("drop the fern" took Bake bread off as well), and so
was the item's name in a grumble about it (asking how come the oil change sat at night took the oil change off).
Now an item comes off when:

- the user's words point to the item: named in this message, pointed back to ("yes, take it off" after a message
  that named it), or asked for in bulk ("clear the rest"), exactly as done_reference.check reads them; and
- a message naming it without asking for it to go isn't a grumble about its place or how it got there (a
  question about how it ended up there, or a request to return it to where it was).

The app's own controls and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# talking about an item's place, or how it got there, without asking for it to go
COMPLAINT_RE = re.compile(r"\b(?:(?:why|how come) (?:did|would|is|are|was|were|has|have|it|the)\b|never (?:said|asked|told)|did(?:n'?t| not) "
                          r"(?:say|ask|tell|want)|not what i (?:said|asked|wrote)|put (?:\w+ ){0,4}back|back (?:to|where|"
                          r"the way|how)|where i (?:wrote|put|had|said|left)|the way i (?:wrote|had|said|laid)|"
                          r"leave (?:\w+ ){0,3}(?:there|alone|where|be|as)|keep (?:\w+ ){0,3}(?:there|where|as)|"
                          r"stay(?:s)? (?:where|there|put|as)|undo|revert)", re.IGNORECASE)


def removal_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why this item can't come off the plan on the user's words this turn, or None."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None
    from thirties_core.done_reference import POINTER_RE, check
    from thirties_core.plan_tools import REMOVE_WORDS_RE
    title = pt._title(span)
    held, _ = check(pt, title, [i for i in (span.id, span.task_id) if i])
    if held and getattr(pt, "prior_exchange", None) is None and REMOVE_WORDS_RE.search(message) \
            and POINTER_RE.search(message):
        held = None  # "take that off" with nothing known to read "that" against: the model's reading stands
    if held:
        return (f"The user's words don't point to {title}, so it stays on the plan. Take off only what the user "
                "asks to take off; ask them if it isn't clear.")
    if not REMOVE_WORDS_RE.search(message) and COMPLAINT_RE.search(message):
        return (f"The user talked about {title} but didn't ask for it to come off, so it stays on the plan. If "
                "they want it somewhere else, move it there; if it isn't clear, ask.")
    # --- "not yet" is about when, not whether: it stays (a simulated day lost an item the user said wasn't due yet) ---
    if not REMOVE_WORDS_RE.search(message) and NOT_YET_RE.search(message):
        return (f"The user said it isn't time for {title} yet, not that it's off, so it stays on the plan. Leave it "
                "where it is, or move it later if they say when.")
    # --- end "not yet" ---
    return None


# the item's time hasn't come: "the kiln run isn't yet", "not time for the paddle yet", "not now, later"
NOT_YET_RE = re.compile(r"\bnot (?:\w+ ){0,3}yet\b|\b(?:isn'?t|is not|aren'?t|are not) (?:\w+ ){0,3}yet\b|"
                        r"\bnot (?:right )?now\b|\bnot time (?:for|to)\b|\btoo (?:early|soon) for\b", re.IGNORECASE)


__all__ = ["removal_refused", "COMPLAINT_RE"]


def merge_readd(pt: "PlanTools", copies: Any, start: Any, end: Any = None, duration: Any = None) -> Optional[str]:
    """Adding an item the day already has planned, without again=true: it stays one item. When the user's words
    this turn name it and ask for another time, the one copy moves there (re-adding it at the time the user wants
    is moving it); at the time it already has, there's nothing to add. None when it isn't that clear (two copies,
    or the user didn't name it): the caller refuses with how to move it."""
    from thirties_core.clock import thirty_of
    from thirties_core.item_facts import mentions
    from thirties_core.plan_tools import ToolError  # the tools' own refusal (assistant_tools.ToolError)
    copies = list(copies or [])
    if len(copies) != 1:
        return None
    span = copies[0]
    message = getattr(pt, "user_message", None)
    title = pt._title(span)
    others = [pt._title(s) for s in pt.day_plan.spans if s.id != span.id]
    try:
        wanted = pt._start(start) if start not in (None, "") else None
    except Exception:
        return None
    if wanted is None:
        return None
    if thirty_of(wanted) == thirty_of(span.start_dt):
        raise ToolError(f"{pt.describe(span)} is already planned [id {span.id}], at that time: there's nothing to add, "
                        "and it stays one item.")
    if getattr(pt, "by_hand", False) or message is None or not mentions(message, title, others):
        return None
    args = {"start": start}
    if end not in (None, ""):
        args["end"] = end
    elif duration not in (None, ""):
        args["duration"] = duration
    out = pt.update_event(span.id, **args)
    return f"{out} (One copy of it was on the plan already; that copy moved, so there's still one.)"


__all__ += ["merge_readd"]
