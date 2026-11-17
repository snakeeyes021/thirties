"""Only what the user asked about changes: a move, a new item or the usual hours need their words, like a removal.

Taking an item off already needs the user's words for it (change_asks.py). Other changes didn't, and simulated days
showed what that costs: asked to move one item, the model moved two more nobody named "to make it fit"; asked about
one thing, it shrank another and planned a second copy; a new item nobody mentioned went on the plan; told a bedtime,
it saved work hours as well. Each time the user had to notice and undo it.

Now, when a change is called for the user's message (the tool is the one the model called, not part of Thirties'
own planning: a day proposal, a routine, the morning set-up), it goes ahead only when the message refers to it:

- **moving an item** (update_event): the message names it, points back to it, or gives a time for it in words
  that fit it (done_reference.check, stated_times.loosely_named), or asks for a bulk change ("redo the afternoon",
  "fix the schedule", "move everything");
- **a new item** (add_event): its words are in the message, the message points back to it (yes to an offer), or the
  user asked Thirties to plan ("plan my afternoon", "fit in what you can");
- **the usual week** (set_week): work hours change when the message talks about work, bedtime and waking when it
  talks about sleep.

Refused, the model is told what to do instead: leave it, put the named item alongside, suggest it, or ask. The
app's own controls and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# Starting something now moves what it covers out of its way (PlanTools._make_room), but only this far past where
# it was: further than that, it stays alongside and the result says so (an item planned for the afternoon was sent
# to the night, unasked)
MAKE_ROOM_REACH = timedelta(hours=2)
# a change to the whole plan, or a stretch of it, said outright: which items is then the model's to say
BULK_CHANGE_RE = re.compile(
    r"\b(?:re-?do|re-?plan|re-?arrange|re-?shuffle|re-?organi[sz]e|re-?work|re-?jig|shuffle|move (?:things|stuff|it all)"
    r"(?: around)?|(?:the|my) whole (?:day|plan|schedule|morning|afternoon|evening)|(?:fix|sort(?: out)?|clean up|tidy"
    r"(?: up)?|redo|rethink|adjust|rejig|re-?build) (?:the|my|today'?s|this) (?:day|plan|schedule|agenda|morning|"
    r"afternoon|evening|timeline)|make (?:room|space|it (?:all )?fit|everything fit)|(?:fit|squeeze) (?:it|them|"
    r"everything|things) in|work (?:it )?around|plan around|whatever (?:works|fits)|wherever (?:it )?fits|"
    r"move (?:the )?(?:rest|others|everything)|push (?:the )?(?:rest|everything))\b", re.IGNORECASE)
# a yes to what was just offered ("sounds good", "ok do that")
AGREE_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|sounds (?:good|great|fine)|do (?:it|that)|go (?:ahead|for it)|"
                      r"please do|perfect|great|that works|fine|let'?s do (?:it|that))\b", re.IGNORECASE)
# the user asking Thirties to plan (a new item from their lists may then go on)
PLAN_ASK_RE = re.compile(
    r"\b(?:plan|lay (?:\w+ )?out|map (?:\w+ )?out|line (?:\w+ )?up|slot|fill|schedule|organi[sz]e|set up|sort out|"
    r"fit (?:in|it|them|what|some)|suggest|what should i|help me (?:with|plan|figure)|put together|build|find (?:me|"
    r"something|one|a)|pick|choose|recommend|something (?:to|for|easy|quick|fun))\b",
    re.IGNORECASE)
_WORK_RE = re.compile(r"\b(?:work\w*|job|shift|office|clock(?:ing)? (?:in|out)|day off|days off|off (?:at|on|"
                      r"today|tomorrow|fridays?|mondays?)|hours)\b", re.IGNORECASE)
_SLEEP_RE = re.compile(r"\b(?:sleep\w*|slept|bed\w*|wak\w*|woke|awake|up at|get(?:ting)? up|alarm|night\w*|"
                       r"turn in|lights out)\b", re.IGNORECASE)


def _talking(pt: "PlanTools", tool: str) -> Optional[str]:
    """The user's message, when this change is the model's own call of ``tool`` for it; else None."""
    message = getattr(pt, "user_message", None)
    if message is None or getattr(pt, "by_hand", False):
        return None
    from thirties_core.plan_log import is_model_call, outer_tool
    return message if is_model_call() and outer_tool() == tool else None


def _clauses(message: str) -> list:
    from thirties_core.given_times import _clauses as clauses
    return clauses(message)


def _titles(pt: "PlanTools") -> list:
    return [s.title for s in pt.day_plan.spans if s.title and not getattr(s, "shown_only", False)]


def _referred_to(pt: "PlanTools", message: str, title: str, ids: Any = ()) -> bool:
    """The message refers to the item: named (done_reference._named), a clause with a word of its name and no other
    item named outright (stated_times.loosely_named), pointed back to or asked for in bulk (done_reference.check), or
    a yes to whatever was offered when what was offered isn't known here."""
    from thirties_core.done_reference import _named, check, pointed_back
    from thirties_core.stated_times import loosely_named
    titles = _titles(pt)
    others = [t for t in titles if t != title]
    if _named(message, title, others) or any(re.search(rf"\b{re.escape(i)}\b", message) for i in ids if i):
        return True
    if any(loosely_named(clause, title, titles) for clause in _clauses(message)):
        return True
    held, _ = check(pt, title, [i for i in ids if i])
    if held is None:
        return True
    if AGREE_RE.search(message):
        prior = getattr(pt, "prior_exchange", None)
        return prior is None or title in pointed_back(pt, titles + [title])
    return False


def move_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why update_event can't change this item for the user's message, or None."""
    message = _talking(pt, "update_event")
    if message is None or span.is_envelope or span.source == "calendar":
        return None  # work and sleep, and calendar events, have their own rules
    if BULK_CHANGE_RE.search(message):
        return None
    title = pt._title(span)
    if _referred_to(pt, message, title, (span.id, span.task_id)):
        return None
    return (f"The user's message doesn't name {title}, so it stays where it is: change only what they asked about. "
            f"If what they named needs this time, put it alongside (alongside=true), or ask them before moving "
            f"{title}.")


def add_refused(pt: "PlanTools", title: str) -> Optional[str]:
    """Why add_event can't put this new item on for the user's message, or None."""
    message = _talking(pt, "add_event")
    if message is None or BULK_CHANGE_RE.search(message) or PLAN_ASK_RE.search(message):
        return None
    from thirties_core.item_facts import match_words
    if match_words(title) & match_words(message):
        return None
    if _referred_to(pt, message, title):
        return None
    return (f"The user didn't ask for {title} this turn, so it isn't added. Mention it in your reply if it matters, "
            "and add it when they say so.")


def week_refused(pt: "PlanTools", work: bool, sleep: bool) -> Optional[str]:
    """Why set_week can't save these usual hours for the user's message, or None."""
    message = _talking(pt, "set_week")
    if message is None:
        return None
    if work and not _WORK_RE.search(message):
        return ("The user's message doesn't talk about their work hours, so those stay as they are: save only what "
                "they said (bedtime or wake), without work_start, work_end or no_work.")
    if sleep and not _SLEEP_RE.search(message):
        return ("The user's message doesn't talk about bedtime or waking, so those stay as they are: save only the "
                "work hours they gave, without bedtime or wake.")
    return None


__all__ = ["move_refused", "add_refused", "week_refused", "BULK_CHANGE_RE", "PLAN_ASK_RE", "AGREE_RE", "MAKE_ROOM_REACH"]
