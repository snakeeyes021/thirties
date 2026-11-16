"""Done only for what the user is talking about.

Marking something done ticks it off in the user's lists, so which item it is is never the model's guess. A done
goes through when the user's words point at the item:

- **Named in this message**: by its words ("walked the dog" for Walk the dog) or by its id.
- **Pointed back to**, when this message names nothing: "that", "it", "yes", "done" for the one item the last
  message they saw named (or, if that named none, what they said just before); "both" for exactly two; "those"
  or "them" for up to MOST_POINTED of them.
- **Asked for in bulk**: "tick off everything from this morning", "all of those": which ones is then the model's to say.
- **An answer to "which one?"**: "the first one", "the last one" for that one of the items the question listed, in
  the order it listed them (open list items it named count too, planned or not).
- **The one whose time has come**: when the words fit several items equally ("the walk's done" with three walks),
  or the item has copies under one name, it's the one still to do that began last (or is due within the half
  hour: a done is often said a little early); before any has begun, the next one. A copy named by its start time
  ("the 8am walk") is that copy.

Anything else is refused with a note to ask which item they mean. The app's own controls (a tick in the Agenda)
and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence, Tuple  # noqa: F401

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
# an answer picking one of the items a question listed, by its place in the list
ORDINAL_RE = re.compile(r"\b(?:the\s+)?(first|second|third|fourth|last|1st|2nd|3rd|4th)(?:\s+one)?\b"
                        r"(?!\s+(?:thing|time|of|in|on|at|half|hour))", re.IGNORECASE)
_PLACE = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "last": -1}
# how early a done is still for an item: said a little before its time
EARLY_MINUTES = 30


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


def _still_to_do(pt: "PlanTools") -> list:
    from thirties_core.models import SpanStatus
    return [s for s in pt.day_plan.spans if not getattr(s, "shown_only", False) and s.status == SpanStatus.PLANNED]


def _begun(pt: "PlanTools", spans: list) -> Optional[list]:
    """Of these items still to do, those whose time has come; None when the time isn't known."""
    from datetime import timedelta
    try:
        now = pt._now()
    except Exception:
        return None
    return [s for s in spans if s.start_dt <= now + timedelta(minutes=EARLY_MINUTES)]


def _the_one_whose_time_came(pt: "PlanTools", message: str, title: str, titles: List[str]) -> bool:
    """When the message matches this item and others equally well ("the walk's done" with three walks on the
    plan), a done means the one whose time has come: it's this one when, of those still to do, it's the one that
    began last (an earlier one left unmarked doesn't make the current one unclear)."""
    from thirties_core.item_facts import tied_with
    tied = tied_with(message, title, [t for t in titles if t != title])
    if not tied:
        return False
    group = [s for s in _still_to_do(pt) if s.title == title or s.title in tied]
    begun = _begun(pt, group)
    if not begun:
        return False
    latest = max(s.start_dt for s in begun)
    last = [s for s in begun if s.start_dt == latest]
    return all(s.title == title for s in last)


def _copies(pt: "PlanTools", ids: Iterable[str]) -> Tuple[Optional[Any], list]:
    """(the item being marked, when it's still to do, and its other copies still to do under the same name)."""
    from thirties_core.item_facts import item_name
    todo = _still_to_do(pt)
    target = next((s for s in todo if s.id in set(ids)), None)
    if target is None:
        return None, []
    return target, [s for s in todo if s.id != target.id and item_name(s.title) == item_name(target.title)]


def _the_copy_meant(pt: "PlanTools", texts: Sequence[str], ids: Iterable[str]) -> bool:
    """Whether a done meant this copy of an item with several copies still to do: the one named by its start time,
    else the one whose time came last, else (none has come yet) the next one. True when it has no copies."""
    from thirties_core.facts import gave_time
    target, copies = _copies(pt, ids)
    if target is None or not copies:
        return True
    for text in texts:
        if gave_time(text, target.start_dt):
            return True
        if any(gave_time(text, c.start_dt) for c in copies):
            return False
    group = [target] + copies
    begun = _begun(pt, group)
    if begun is None:
        return True
    meant = max(begun, key=lambda s: s.start_dt) if begun else min(group, key=lambda s: s.start_dt)
    return meant.id == target.id


def _by_place(message: str, shown: str, pointed: List[str], title: str) -> Optional[bool]:
    """An answer picking an item by its place in the list just shown ("the first one"): whether it's this one.
    None when the answer doesn't pick by place, or the list's order can't be read."""
    m = ORDINAL_RE.search(message or "")
    if not m or len(pointed) < 2:
        return None
    low = shown.lower()
    where = {t: low.find(t.lower()) for t in pointed}
    if any(i < 0 for i in where.values()):
        return None
    ordered = sorted(pointed, key=lambda t: where[t])
    place = _PLACE[m.group(1).lower()]
    if place >= len(ordered):
        return False
    return ordered[place] == title


def _open_items_shown(pt: "PlanTools", shown: str, titles: List[str]) -> List[str]:
    """Open list items whose full wording appears in the last reply (a "which one?" question lists them)."""
    assistant = getattr(pt, "assistant", None)
    if assistant is None or not shown:
        return []
    low = shown.lower()
    found = []
    for t in getattr(getattr(assistant, "s", None), "tasks", None) or []:
        name = (t.title or "").strip()
        if name and name not in titles and name not in found and not t.id.startswith("media:") \
                and re.search(rf"(?<!\w){re.escape(name.lower())}(?!\w)", low):
            found.append(name)
    return found


def _listed(text: str, title: str) -> bool:
    """A message the user was shown lists the item: every word of its title is in it."""
    from thirties_core.item_facts import name_words
    mine = name_words(title)
    return bool(mine) and mine <= name_words(text)


def pointed_back(pt: "PlanTools", titles: List[str]) -> List[str]:
    """The items a pointer this turn ("it", "that", "both") can reach: those the last message the user was shown
    lists, else those the user's previous message named."""
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    pool = titles + _open_items_shown(pt, shown, titles)
    return [t for t in pool if _listed(shown, t)] or [t for t in titles if _named(said, t, titles)]


def check(pt: "PlanTools", title: str, ids: Iterable[str] = (),
          besides: Iterable[str] = ()) -> Tuple[Optional[str], Optional[str]]:
    """(why the done can't go ahead, the user's words about the item). The words are the message with a pointer
    back ("I'm in the middle of it") read as the item's name, for the started and not-yet checks (started.py); None
    when nobody is talking. Titles in besides are left out of the comparison (an item the done was found not to
    be about)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None, message
    ids = [i for i in ids if i]
    left_out = set(besides)
    titles = [t for t in _titles(pt) if t not in left_out]
    if title not in titles:  # a list item with no time today
        titles.append(title)
    others = [t for t in titles if t != title]
    refusal = (f"The user's words don't point to {title}, so it isn't marked done. If they meant it, ask which "
               "item they finished; if they named another item, mark that one by its id.")
    if any(re.search(rf"\b{re.escape(i)}\b", message) for i in ids) or BULK_RE.search(message):
        return None, message
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    copy_refusal = (f"{title} is on the plan more than once and the user's words don't say which time, so it "
                    "isn't marked done. Mark the one whose time has come, or ask which one they did.")
    if _named(message, title, titles):
        return (None, message) if _the_copy_meant(pt, [message], ids) else (copy_refusal, message)
    if _the_one_whose_time_came(pt, message, title, titles):
        return None, message
    if any(_named(message, t, titles) for t in others):
        return refusal, message  # they're talking about other items
    pointed = pointed_back(pt, titles)
    if title not in pointed:
        return refusal, message
    many = len(set(pointed))
    picked = _by_place(message, shown, list(dict.fromkeys(pointed)), title)
    if picked is not None:
        ok = picked
    elif BOTH_RE.search(message):
        ok = many == 2
    elif PLURAL_RE.search(message):
        ok = many <= MOST_POINTED
    else:
        ok = many == 1
    if not ok:
        return refusal, message
    if not _the_copy_meant(pt, [message, shown], ids):
        return copy_refusal, message
    return None, POINTER_RE.sub(title, message)


__all__ = ["check", "pointed_back", "prior_exchange", "BULK_RE", "MOST_POINTED"]
