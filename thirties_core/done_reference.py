"""Done only for what the user is talking about.

Marking something done ticks it off in the user's lists, so which item it is is never the model's guess. A done
goes through when the user's words point at the item:

- **Named in this message**: by its words ("walked the dog" for Walk the dog) or by its id.
- **Pointed back to**, when this message names nothing: "that", "it", "yes", "done" for the one item the last
  message they saw named (or, if that named none, what they said just before); "both" for exactly two; "those"
  or "them" for up to MOST_POINTED of them.
- **Asked for in bulk**: "tick off everything from this morning", "all of those": which ones is then the model's to say.

Anything else is refused with a note to ask which item they mean. The app's own controls (a tick in the Agenda)
and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

# many at once, said outright
BULK_RE = re.compile(r"\b(?:everything|every (?:item|task|one|single one|thing)|all of (?:it|them|those|these|that|the|"
                     r"my|today)|all (?:those|these|the rest|the other|my)|them all|those all|the rest|each of "
                     r"(?:them|those|these))\b", re.IGNORECASE)
BOTH_RE = re.compile(r"\b(?:both|the two|those two|these two)\b", re.IGNORECASE)
PLURAL_RE = re.compile(r"\b(?:those|these|them|they|they're|theyre)\b", re.IGNORECASE)
# a pointer back at something just said ("did that", "it's done"), resolved to the item when the check reads the
# user's words about it
POINTER_RE = re.compile(r"\b(?:that one|this one|those|these|them|both|that|this|it)\b", re.IGNORECASE)

# a plural pointer covers a short list; past that it's the whole schedule, and which ones isn't clear
MOST_POINTED = 4


def prior_exchange(messages: Sequence[dict]) -> Tuple[str, str]:
    """(what the user said last, the last message they were shown) before this turn: the exchange their words can
    point back to. Hidden instructions and system checks aren't part of it."""
    def visible(m: dict) -> bool:
        text = str(m.get("content") or "")
        return bool(text.strip()) and not m.get("hidden") and not text.startswith("[System check")
    said = next((str(m["content"]) for m in reversed(messages) if m.get("role") == "user" and visible(m)), "")
    shown = next((str(m["content"]) for m in reversed(messages) if m.get("role") == "assistant" and visible(m)), "")
    return said, shown


def _titles(pt: "PlanTools") -> List[str]:
    plan = pt.day_plan
    return [s.title for s in plan.spans if not getattr(s, "shown_only", False) and s.title]


def _named(text: str, title: str, others: Iterable[str]) -> bool:
    from thirties_core.item_facts import referenced
    from thirties_core.started import _names
    return bool(text) and (referenced(text, title, others) or _names(text, title, list(others)))


def _the_one_whose_time_came(pt: "PlanTools", message: str, title: str, titles: List[str]) -> bool:
    """When the message matches this item and others equally well ("the walk's done" with three walks on the
    plan), a done means the one whose time has come (begun, or due within the half hour: a done is often said a
    little early): it's this one when its time has come and that of none of the others still to do has."""
    from datetime import timedelta
    from thirties_core.item_facts import tied_with
    from thirties_core.models import SpanStatus
    tied = tied_with(message, title, [t for t in titles if t != title])
    if not tied:
        return False
    try:
        now = pt._now()
    except Exception:
        return False
    spans = [s for s in pt.day_plan.spans if not getattr(s, "shown_only", False)]

    def started(name: str) -> bool:
        return any(s.title == name and s.status == SpanStatus.PLANNED and s.start_dt <= now + timedelta(minutes=30)
                   for s in spans)
    return started(title) and not any(started(t) for t in tied)


def _listed(text: str, title: str) -> bool:
    """A message the user was shown lists the item: every word of its title is in it."""
    from thirties_core.item_facts import name_words
    mine = name_words(title)
    return bool(mine) and mine <= name_words(text)


def check(pt: "PlanTools", title: str, ids: Iterable[str] = ()) -> Tuple[Optional[str], Optional[str]]:
    """(why the done can't go ahead, the user's words about the item). The words are the message with a pointer
    back ("I'm in the middle of it") read as the item's name, for the started and not-yet checks (started.py); None
    when nobody is talking."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None, message
    titles = _titles(pt)
    if title not in titles:  # a list item with no time today
        titles.append(title)
    others = [t for t in titles if t != title]
    if _named(message, title, titles) or any(i and re.search(rf"\b{re.escape(i)}\b", message) for i in ids):
        return None, message
    if BULK_RE.search(message):
        return None, message
    if _the_one_whose_time_came(pt, message, title, titles):
        return None, message
    refusal = (f"The user's words don't point to {title}, so it isn't marked done. If they meant it, ask which "
               "item they finished; if they named another item, mark that one by its id.")
    if any(_named(message, t, titles) for t in others):
        return refusal, message  # they're talking about other items
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    pointed = [t for t in titles if _listed(shown, t)] or [t for t in titles if _named(said, t, titles)]
    if title not in pointed:
        return refusal, message
    many = len(set(pointed))
    if BOTH_RE.search(message):
        ok = many == 2
    elif PLURAL_RE.search(message):
        ok = many <= MOST_POINTED
    else:
        ok = many == 1
    if not ok:
        return refusal, message
    return None, POINTER_RE.sub(title, message)


__all__ = ["check", "prior_exchange", "BULK_RE", "MOST_POINTED"]
