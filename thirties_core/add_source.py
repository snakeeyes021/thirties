"""A new item the model adds for the user's message cites the words it comes from.

Deciding from the words of a title whether the user asked for it failed both ways: a natural name for what they
said ("Hardware store run" for "I need sandpaper") shares none of their words, and an item nobody asked for can
borrow one of them. So the model now says where an add comes from: each tool that puts a new item on the plan or
a list for the user's message (add_event, add_task, and the new things in add_to_today and propose_day) takes
``because``, the exact span of the user's words the item comes from. What the item is called stays the model's
choice.

Python checks only that the quote is really there, ignoring case, spacing and curly quote marks: in this message, or in
the user's last few messages that a removal ask still counts from (change_asks.earlier_messages). A title that is
itself a span of those words is its own quote. No quote, or one the user never wrote, and nothing is added; the
model is told to cite their words or ask.

Thirties' own adds need no quote: the app's controls, the background, the day proposal that builds the morning,
routines, and the steps a tool takes inside another (scheduling a list item add_task just wrote).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
                         "`": "'", "´": "'"})
# what a model wraps around a quote, or ends it with, that isn't part of what the user wrote
_EDGES = " \t\r\n'\".,;:!?…()[]"


def normal(text: Any) -> str:
    """Lower case, straight quotes, one space between words."""
    return " ".join(str(text or "").translate(_QUOTES).lower().split())


def _without_notes(text: str) -> str:
    """The user's own words: a note Python put beside the message for the model is no quote of theirs."""
    from thirties_core.internal_text import NOTE_OPENER
    at = text.find(NOTE_OPENER)
    return text[:at] if at >= 0 else text


def window(pt: "PlanTools") -> List[str]:
    """What the user said that an add can cite: this message and the recent ones a removal ask still counts from."""
    from thirties_core.change_asks import earlier_messages
    said = [getattr(pt, "user_message", None) or ""] + list(earlier_messages(pt))
    return [normal(_without_notes(str(s))) for s in said if s]


def found(pt: "PlanTools", because: Any) -> bool:
    """The quote is in what the user said (``window``), as whole words."""
    quote = normal(because).strip(_EDGES)
    if not re.search(r"\w", quote):
        return False
    whole = re.compile(rf"(?<!\w){re.escape(quote)}(?!\w)")
    return any(whole.search(said) for said in window(pt))


def talking(pt: "PlanTools", tool: str) -> bool:
    """This add is the model's own call of ``tool`` for the user's message, and not the day proposal that builds
    the morning."""
    from thirties_core.change_scope import _talking
    if _talking(pt, tool) is None:
        return False
    from thirties_core.turn_guards import _building_the_day
    return not (tool == "propose_day" and _building_the_day(pt))


def refused(pt: "PlanTools", tool: str, title: str, because: Any) -> Optional[str]:
    """Why the model can't add this new item for the user's message, or None: ``because`` must quote their words."""
    if not talking(pt, tool):
        return None
    if not normal(because).strip(_EDGES) and found(pt, title):
        return None  # named in the user's own words: the title is the quote ("Hot bath" for "I need a hot bath")
    if not normal(because).strip(_EDGES):
        return (f"{title} isn't added: say which of the user's words it comes from, quoted exactly in because. "
                "If they didn't ask for it, mention it in your reply or ask them.")
    if not found(pt, because):
        return (f"{title} isn't added: \"{str(because).strip()}\" isn't what the user said. Quote their exact words "
                "in because, or ask them whether they want it.")
    return None


__all__ = ["refused", "found", "window", "normal", "talking"]
