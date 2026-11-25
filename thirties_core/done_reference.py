"""Which item the user's words point to.

For a done during a turn, which item it is for is the model's reading (``done_reading``): Python never refuses it
for pointing elsewhere. What the words say besides goes in the done's result as a note, so the model can ask or
put it right: another item they name or fit better, the items a clock time names when this one isn't planned
then, another copy of the item whose time has come (``also_said_note``). A done stops only on the user's words:
nothing they said is a done, or they name the item as not done (said_for_it.py, write_guard.state_not_done,
done_exceptions.py).

``check`` reads whether the words point to an item, for the asks that change the plan without a done (taking
something off, dropping it for good); for a done it gives only the user's words about the item (a pointer back read
as its name) and the items the reply has to name. The words point at the item when it is:

- **Named in this message**: by its words ("walked the dog" for Walk the dog), by everyday words for them ("dog
  agility done" for Take the puppy to agility, "varnishing done" for Varnish: done_words.py) or by its id.
- **Asked about, or asked for before**: the last message they saw asked whether they meant this one item, and the
  reply isn't a no; or they asked for this done in an earlier message and it was refused (done_words.py).
- **Pointed back to**, when this message names nothing: "that", "it", "yes", "done" for the one item the last
  message they saw named (or, if that named none, what they said just before); "both" for exactly two; "those"
  or "them" for up to MOST_POINTED of them; a count ("all three", "three of them") for exactly that many.
- **A bare done after a list**: "that one's sorted", "done!" with no word of any item in it, right after a reply
  listing several open items: the one of those the model picks. The reply says which, and a "no" takes it back.
- **Asked for in bulk**: "tick off everything from this morning", "all of those": which ones is then the model's to say.
  With a clock time ("every 9:10 thing on the bench is wrapped up", "all of my 2:40 stuff"), it's every item
  planned to start in the thirty it names, and only those: another item sharing their words, planned at another
  time, is never one of them (for a done, the note says so).
- **A whole routine**: its name with "all", "whole" or "everything": every step of it, in every part; or "both" /
  "all of them" to a question about it or its parts: every step of the parts asked about (whole_routine.py). The
  reply names each step ticked so.
- **An answer to "which one?"**: "the first one", "the last one" for that one of the items the question listed, in
  the order it listed them (open list items it named count too, planned or not).
- **The one just listed**: when the words fit several items equally and the last message the user was shown listed
  exactly one of them ("Not marked yet: Glaze run" beside a Glaze run review call), it's that one.
- **A plain done**: the app's last reply named only this item and the user confirms it or says it happened; the
  item's verb in the past with its object in everyday words ("the chooks got watered") and no other item still to
  do fitting as well; or a done refused earlier for this item, said again (plain_done.py). A pointer beside such
  words ("the shawl is done, tick it") is the item they name, never a pick among the items last listed.
- **The one whose time has come**: when the words fit several items equally ("the walk's done" with three walks),
  or the item has copies under one name, it's the one still to do that began last (or is due within the half
  hour: a done is often said a little early); before any has begun, the next one. A copy named by its start time
  ("the 8am walk") is that copy.

Whatever points to it, words that share a word with the item but name another planned or listed item ("the glaze
order is sent" beside Glaze recipe test and a listed Send the glaze order) don't point at it
(done_fit.named_instead); for a done, the note names that item, and the list item that fits all the words. Words
that only may name something else are the model's to read: the done goes on, and its result says what else they
say. Anything else doesn't point at it. The app's own controls (a tick in the Agenda) and the background (nobody
talking) are never held here.

A skip needs a basis too (skip_check): the item named, pointed back to, asked for in bulk, listed in the last
reply the user answers without asking anything, or planned to start inside a stretch of the day the words give by
two times. Whether the words mean a skip is the model's to read.
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
# many at once by their time: a bulk word, then a clock time that says which items ("every 9:10 thing on the
# bench", "all of my 2:40 PM stuff"); never a time something was done at ("did it all at 9:10")
BULK_AT_RE = re.compile(r"\b(?:all|every|each)(?:\s+of)?(?:\s+(?:the|my))?\s+(?P<h>\d{1,2})(?:(?::(?P<m>\d{2}))"
                        r"(?:\s*(?P<ap>[ap])\.?m\b\.?)?|\s*(?P<ap2>[ap])\.?m\b\.?)(?=\s+[^\W\d_])", re.IGNORECASE)
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
    from thirties_core.done_words import named as in_everyday_words
    from thirties_core.item_facts import referenced
    from thirties_core.started import _names
    return bool(text) and (referenced(text, title, others) or _names(text, title, list(others))
                           or _by_initials(text, title, others) or in_everyday_words(text, title, others))


def _another_in_everyday_words(text: str, others: Iterable[str], titles: List[str]) -> bool:
    """Another item is the one the words name, read loosely (done_words.py): "dog agility done" is the puppy's
    class, never whichever agility item's time came."""
    from thirties_core.done_words import named as in_everyday_words
    return any(in_everyday_words(text, t, titles) for t in others)


# --- a short form of an item's name: the first letters of its leading capitalised words ("HS" for Hardware Store:
# hinges) ---
_INITIALS_HEAD_RE = re.compile(r"\s*(?::|\s[-–—]\s|\(|,)")
_NOT_INITIALS = {"it", "is", "in", "on", "at", "to", "me", "my", "up", "ok", "so", "no", "do", "go", "be", "an", "as",
                 "or", "if", "of", "we", "us", "hi", "am", "pm", "id", "by", "oh", "ah"}


def initials(title: str) -> str:
    """The first letters of the capitalised words that open an item's name, before any ':' or ' - ' ("hs" for
    Hardware Store: hinges and glue); "" when fewer than two words open it so."""
    head = _INITIALS_HEAD_RE.split(title or "", maxsplit=1)[0]
    letters = []
    for word in head.split():
        if not word[:1].isupper():
            break
        letters.append(word[0].lower())
    return "".join(letters) if 2 <= len(letters) <= 4 else ""


def _by_initials(text: str, title: str, others: Iterable[str]) -> bool:
    """The message names the item by its initials, written as a word of its own, and no other item has them. In
    lower case only when they can't be an everyday word."""
    mine = initials(title)
    if not mine or any(initials(o) == mine for o in others if o != title):
        return False
    for m in re.finditer(r"(?<![\w'])([A-Za-z]{2,4})(?![\w'])", text or ""):
        word = m.group(1)
        if word.lower() == mine and (word.isupper() or word.lower() not in _NOT_INITIALS):
            return True
    return False
# --- end a short form ---


def _still_to_do(pt: "PlanTools") -> list:
    """Items with no outcome yet: the planned ones, and the ones not marked yet (not_marked.py: one stored as
    unknown is still the user's to mark, as on the Agenda)."""
    from thirties_core.models import SpanStatus
    from thirties_core.not_marked import unmarked_spans
    spans = [s for s in pt.day_plan.spans if not getattr(s, "shown_only", False)]
    unmarked = {s.id for s in unmarked_spans(spans, None)}
    return [s for s in spans if s.status == SpanStatus.PLANNED or s.id in unmarked]


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


def _the_one_listed(message: str, shown: str, title: str, titles: List[str]) -> bool:
    """When the message's words fit this item and others equally ("the glaze run is done" beside Glaze run and
    Glaze run review call), the last message the user was shown picks it: it listed this one in full and none of
    the others ("Not marked yet: Glaze run"). What they were just shown is what they're answering."""
    from thirties_core.item_facts import tied_with
    tied = tied_with(message, title, [t for t in titles if t != title])
    return bool(tied) and _listed(shown, title) and not any(_listed(shown, t) for t in tied)


def _picked_by_time(pt: "PlanTools", message: str, title: str, titles: List[str]) -> Optional[bool]:
    """When the message's words fit this item and others equally, a time it gives picks the one that starts then:
    True for this item, False for another, None when no time it gives tells them apart."""
    from thirties_core.facts import gave_time
    from thirties_core.item_facts import tied_with
    tied = set(tied_with(message, title, [t for t in titles if t != title]))
    if not tied:
        return None

    def starts(names: set) -> list:
        spans = [s for s in pt.day_plan.spans if not getattr(s, "shown_only", False) and s.title in names]
        return [dt for s in spans for dt in (s.start_dt, getattr(s, "planned_start", None)) if dt is not None]
    mine = any(gave_time(message, dt) for dt in starts({title}))
    theirs = any(gave_time(message, dt) for dt in starts(tied))
    if mine != theirs:
        return mine
    return None


def _thirty_said(pt: "PlanTools", message: str) -> Optional[Tuple[Any, Any]]:
    """The thirty a bulk word with a clock time names ("every 9:10 thing": 9:00 to 9:30), as (start, end) on the
    plan's day; without am or pm, the one of the two nearest now. None when the words give no such time."""
    from datetime import timedelta
    from thirties_core.clock import SHORT, thirty_of
    m = BULK_AT_RE.search(message or "")
    if m is None:
        return None
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    half = (m.group("ap") or m.group("ap2") or "").lower()
    if hour > 23 or minute > 59 or (half and not 1 <= hour <= 12):
        return None
    hours = [hour % 12 + (12 if half == "p" else 0)] if half else ([hour] if hour > 12 or hour == 0 else
                                                                    [hour % 12, hour % 12 + 12])
    base = pt.day_plan.day_start.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [thirty_of(base + timedelta(hours=h, minutes=minute)) for h in hours]
    starts = [a if a >= pt.day_plan.day_start else a + timedelta(days=1) for a in starts]  # after midnight: that night
    try:
        now = pt._now()
        start = min(starts, key=lambda a: abs((a - now).total_seconds()))
    except Exception:
        start = starts[0]
    return start, start + SHORT


def _planned_in(pt: "PlanTools", title: str, ids: Iterable[str], thirty: Tuple[Any, Any]) -> bool:
    """The item being marked is planned to start in the thirty (as planned, before any outcome moved it)."""
    spans = [s for s in pt.day_plan.spans if not getattr(s, "shown_only", False)]
    ids = set(ids)
    mine = [s for s in spans if s.id in ids] or [s for s in spans if s.title == title]
    a, b = thirty
    return any(a <= (getattr(s, "planned_start", None) or s.start_dt) < b for s in mine)


def _copies(pt: "PlanTools", ids: Iterable[str]) -> Tuple[Optional[Any], list]:
    """(the item being marked, when it's still to do, and its other copies still to do under the same name)."""
    from thirties_core.item_facts import item_name
    todo = _still_to_do(pt)
    target = next((s for s in todo if s.id in set(ids)), None)
    if target is None:
        return None, []
    return target, [s for s in todo if s.id != target.id and item_name(s.title) == item_name(target.title)]


def _copy_meant(pt: "PlanTools", texts: Sequence[str], ids: Iterable[str]) -> Optional[Any]:
    """The copy a done meant, of an item with several copies still to do, when it isn't this one: the one named by
    its start time, else the one whose time came last, else (none has come yet) the next one. None when it's this
    one, or the item has no copies."""
    from thirties_core.facts import gave_time
    target, copies = _copies(pt, ids)
    if target is None or not copies:
        return None
    for text in texts:
        if gave_time(text, target.start_dt):
            return None
        named = [c for c in copies if gave_time(text, c.start_dt)]
        if named:
            return named[0]
    group = [target] + copies
    begun = _begun(pt, group)
    if begun is None:
        return None
    meant = max(begun, key=lambda s: s.start_dt) if begun else min(group, key=lambda s: s.start_dt)
    return None if meant.id == target.id else meant


def _the_copy_meant(pt: "PlanTools", texts: Sequence[str], ids: Iterable[str]) -> bool:
    """Whether a done meant this copy of an item with several copies still to do (``_copy_meant``). True when it
    has no copies."""
    return _copy_meant(pt, texts, ids) is None


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


def _listed_alone(text: str, title: str) -> bool:
    """A message the user was shown lists the item on its own: its whole title stands as an entry, a list line
    ("- 9:20 AM – Costumes") or one of a series ("Costumes, Props and Gels"), never inside a longer title
    ("Steam the costumes" doesn't list Costumes)."""
    title = (title or "").strip()
    for m in re.finditer(rf"(?<![\w'’]){re.escape(title)}(?![\w'’])", text or "", re.IGNORECASE) if title else ():
        before, after = text[:m.start()].rstrip(" \t"), text[m.end():].lstrip(" \t")
        if (not before or not before[-1].isalnum() or re.search(r"(?<!\w)(?:and|or)$", before, re.IGNORECASE)) \
                and (not after or not after[0].isalnum() or re.match(r"(?:and|or)(?!\w)", after, re.IGNORECASE)):
            return True
    return False


def _listed_items(pt: "PlanTools", names: Sequence[str]) -> List[str]:
    """The open items on the user's lists other than this one, by their wording."""
    try:
        from thirties_core.done_on_lists import _candidates
        from thirties_core.list_reading import name_of
        return [name_of(t) for t in _candidates(pt.assistant) if name_of(t) not in names]
    except Exception:
        return []


def pointed_back(pt: "PlanTools", titles: List[str]) -> List[str]:
    """The items a pointer this turn ("it", "that", "both") can reach: those the last message the user was shown
    lists, else those the user's previous message named."""
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    pool = titles + _open_items_shown(pt, shown, titles)
    return [t for t in pool if _listed(shown, t)] or [t for t in titles if _named(said, t, titles)]


def _picked(pt: "PlanTools", message: str, title: str, shown: str, titles: List[str]) -> bool:
    """A done right after a reply listing several open items, with no word of an item on the plan or in that reply
    ("that one's sorted", "done!"): which one it is is the model's pick. It stands when the item is one of the open
    items that reply listed on its own; the reply then has to say which one it ticked (reply_checks.picked_unnamed)."""
    from thirties_core.done_words import _hits
    from thirties_core.plain_done import _open_titles, _which_words, says_it_happened
    if not says_it_happened(message) or not _listed_alone(shown, title):
        return False
    shown_open = _open_items_shown(pt, shown, titles)
    if title not in _open_titles(pt) + shown_open:
        return False
    which = _which_words(message)
    return not any(_hits(which, t) for t in titles + shown_open)  # a word of the plan's items, or of those shown


def also_said_note(pt: "PlanTools", title: str, ids: Iterable[str] = (), besides: Iterable[str] = ()) -> str:
    """The lines a done's result carries when the user's words may name something besides this item
    (done_fit.also_said), name another item outright (done_fit.named_note), may be meant for another item
    (plain_done.also_fits), give a clock time this item isn't planned at, or fit another copy of it whose time has
    come, so the model can ask or put it right; "" otherwise, or when nobody is talking. The names the words leave
    out of the done are never read as what it's about (done_exceptions.py)."""
    message = getattr(pt, "user_message", None)
    ids = [i for i in ids if i]
    if getattr(pt, "by_hand", False) or not message or any(re.search(rf"\b{re.escape(i)}\b", message) for i in ids):
        return ""
    from thirties_core.done_counts import without_counts
    from thirties_core.done_exceptions import without_exceptions
    from thirties_core.done_fit import also_said, named_instead, named_note, names_of, names_something_else
    left_out = set(besides)
    others = [t for t in _titles(pt) if t not in left_out and t != title]
    from thirties_core.plain_done import also_fits
    names = names_of(pt, title, ids)
    message = without_exceptions(message) or ""
    naming = without_counts(message)
    listed = _listed_items(pt, names)
    instead = named_instead(naming, names, others, listed)
    said = (named_note(pt, title, names_something_else(naming, names, others, listed), instead) if instead
            else also_said(naming, names, others, listed))
    fits = [f for f in also_fits(pt, naming, title, left_out) if f not in instead]
    meant = (f"(The user's message may also mean {', '.join(fits)}: if they meant another item, ask which, or "
             "correct it.)") if fits else ""
    if not said and not meant:
        meant = _elsewhere_note(pt, naming, title, others, message)
    return " ".join(n for n in (said, meant, _time_note(pt, message, title, ids), _copy_note(pt, message, title, ids),
                                _place_note(pt, message, title) or _asked_note(pt, message, title)) if n)


def _place_note(pt: "PlanTools", message: str, title: str) -> str:
    """A note when the words pick an item by its place in the list the last reply gave ("the first one") and it
    isn't this one; "" otherwise."""
    m = ORDINAL_RE.search(message or "")
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if not m or not shown:
        return ""
    titles = [t for t in _titles(pt) if t]
    if title not in titles:
        titles.append(title)
    listed = [t for t in dict.fromkeys(titles + _open_items_shown(pt, shown, titles)) if _listed(shown, t)]
    if title not in listed or _by_place(message, shown, listed, title) is not False:
        return ""
    low = shown.lower()
    ordered = sorted(listed, key=lambda t: low.find(t.lower()))
    place = _PLACE[m.group(1).lower()]
    if place >= len(ordered):
        return ""
    return (f"(The user's message picks {ordered[place]} by its place in the last reply, not {title}: if they meant "
            "that one, correct it.)")


def _asked_note(pt: "PlanTools", message: str, title: str) -> str:
    """A note when the words answer the app's question about whether items are done, or which: the items it asked
    about, when this one wasn't among them, or when it asked about several and the words don't say which; ""
    otherwise."""
    from thirties_core.said_for_it import done_question
    from thirties_core.done_words import asked_about
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if not done_question(shown):
        return ""
    titles = [t for t in _titles(pt) if t]
    if title not in titles:
        titles.append(title)
    pool = titles + _open_items_shown(pt, shown, titles)
    asked = asked_about(shown, pool, titles) or [t for t in pool if _listed(shown, t)]  # a question naming none
    if not asked or _named(message, title, [t for t in titles if t != title]):
        return ""
    if title not in asked:
        return (f"(The user's message answers the question about {', '.join(asked)}, and {title} wasn't in it: if "
                "they didn't mean it, correct it.)")
    if len(asked) > 1 and not (BOTH_RE.search(message) or PLURAL_RE.search(message) or BULK_RE.search(message)
                               or ORDINAL_RE.search(message)):
        return (f"(The user's message answers a question about {', '.join(asked)} without saying which: if it "
                "isn't clear, ask.)")
    return ""


def _elsewhere_note(pt: "PlanTools", message: str, title: str, others: List[str], said_as: str = "") -> str:
    """A note when the done's words fit other items and say nothing of this one ("the mugs are glazed" for Water
    the hens), fit another item still to do just as well (a plain tie), or point back ("that", "those two") to a
    reply that named items; "" otherwise. ``message``: the words with counts taken out, for which item they name;
    ``said_as``: the words as said, for the pointers and counts."""
    from thirties_core.done_words import _hits
    from thirties_core.plain_done import _in_the_done, _stems, misses_it, tied_with_open
    said_as = said_as or message
    if misses_it(message, title):
        said = _stems(_in_the_done(message))
        fit = [t for t in others if _hits(said, t)]
        if not fit and POINTER_RE.search(message):  # a pointer: what the rest of the message names
            fit = [t for t in others if _named(message, t, [x for x in others + [title] if x != t])]
        if fit:
            return (f"(The user's message fits {', '.join(fit)} and says nothing of {title}: if they didn't mean it, "
                    "correct it.)")
        return _pointer_note(pt, said_as, title, message)
    tied = tied_with_open(pt, message, title)
    if tied:
        return (f"(The user's message fits {', '.join(tied)} as well as {title}: if it isn't clear which, ask, or "
                "correct it.)")
    return ""


def _pointer_note(pt: "PlanTools", message: str, title: str, naming: str = "") -> str:
    """A note when the words name no item and point back ("that", "it", "those two") to a reply that named items:
    this one wasn't among them, or "that" could be several of them. ``naming``: the words with counts taken out
    ("those two" never names Fire two test tiles)."""
    from thirties_core.done_counts import pointed_count
    if not (POINTER_RE.search(message) or BOTH_RE.search(message) or pointed_count(message)) \
            or ORDINAL_RE.search(message):
        return ""
    titles = [t for t in _titles(pt) if t]
    if title not in titles:
        titles.append(title)
    naming = naming or message
    if any(_named(naming, t, [x for x in titles if x != t]) for t in titles):
        return ""
    pointed = list(dict.fromkeys(pointed_back(pt, titles)))
    if not pointed:
        return ""
    if title not in pointed:
        return (f"(The user's message points back to a reply that named {', '.join(pointed)}, and {title} wasn't "
                "among them: if they didn't mean it, correct it.)")
    if len(pointed) < 2 or BOTH_RE.search(message) or PLURAL_RE.search(message) or pointed_count(message):
        return ""
    return (f"(The user's message points back to a reply that named {', '.join(pointed)}: if it isn't clear which, "
            "ask, or correct it.)")


def _time_note(pt: "PlanTools", message: str, title: str, ids: Iterable[str]) -> str:
    """A note when the words name the items planned at a clock time ("every 2:40 thing") and this one isn't
    planned then, or give a time that fits another item they fit as well ("the 11am one"); "" otherwise."""
    from thirties_core.clock import format_clock
    thirty = _thirty_said(pt, message)
    if thirty is not None:
        if _planned_in(pt, title, ids, thirty):
            return ""
        return (f"(The user's message names the items planned at {format_clock(thirty[0])}, and {title} isn't "
                "planned then: if they didn't mean it, ask, or correct it.)")
    titles = [t for t in _titles(pt) if t]
    if title not in titles:
        titles.append(title)
    if _picked_by_time(pt, message, title, titles) is False:
        return (f"(The user's message gives a time that fits another item its words fit as well as {title}: if "
                "they meant that one, correct it.)")
    return ""


def _copy_note(pt: "PlanTools", message: str, title: str, ids: Iterable[str]) -> str:
    """A note when this item is on the plan more than once and the done looks meant for another copy of it: the
    one named by its time, or whose time has come; "" otherwise."""
    from thirties_core.clock import format_clock
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    meant = _copy_meant(pt, [message, shown], ids)
    if meant is None:
        return ""
    return (f"(The user's message may mean the {title} at {format_clock(meant.start_dt)} [id {meant.id}]: it's on "
            "the plan more than once; if they meant that one, correct it.)")


def done_reading(pt: "PlanTools", title: str, ids: Iterable[str] = (),
                 besides: Iterable[str] = ()) -> Tuple[str, Optional[str]]:
    """For a done during a turn: (the lines its result carries, the user's words about the item, with a pointer
    back read as its name). Which item a done is for is the model's reading, so nothing here refuses it; what else
    the words say is a note (``also_said_note``). A done the model picked on words naming no item is kept for the
    reply to name (``check``)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return "", message
    _, words = check(pt, title, ids, besides)
    return also_said_note(pt, title, ids, besides), words


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
    if any(re.search(rf"\b{re.escape(i)}\b", message) for i in ids):
        return None, message
    # --- a count is never a name: "those two" counts items, it doesn't name Fire two test tiles (done_counts.py) ---
    from thirties_core.done_counts import without_counts
    naming = without_counts(message)  # which item the words name; pointers, counts and times read them as said
    # --- end a count ---
    # --- words naming another item that shares a word with it: never this item in its place (done_fit.py) ---
    from thirties_core.done_fit import named_instead, names_of, names_something_else, refusal as named_else
    names = names_of(pt, title, ids)
    listed = _listed_items(pt, names)
    instead = named_instead(naming, names, others, listed)
    if instead:
        return named_else(pt, title, names_something_else(naming, names, others, listed), instead), message
    # --- end another item ---
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    # --- a whole routine said done: every step of it, in every part; or a whole answer ("both") to a question
    # about groups of steps, each of which the reply then names (whole_routine.py) ---
    from thirties_core.whole_routine import answered, routine_meant
    if routine_meant(pt, message, shown, title):
        picks = getattr(pt, "picked_dones", None)
        if isinstance(picks, set) and answered(pt, message, shown, title):
            picks.add(title)
        return None, message
    # --- end a whole routine ---
    # --- many at once by their time: every item planned to start in that thirty, and only those ---
    thirty = _thirty_said(pt, message)
    if thirty is not None:
        if _planned_in(pt, title, ids, thirty):
            return None, message
        from thirties_core.clock import format_clock
        return (f"The user's words name the items planned at {format_clock(thirty[0])}, and {title} isn't planned "
                "then, so it isn't marked done. If they meant it too, ask."), message
    # --- end by their time ---
    if BULK_RE.search(message):
        return None, message
    copy_refusal =(f"{title} is on the plan more than once and the user's words don't say which time, so it "
                    "isn't marked done. Mark the one whose time has come, or ask which one they did.")
    if _named(naming, title, titles):
        return (None, message) if _the_copy_meant(pt, [message], ids) else (copy_refusal, message)
    # --- a time the user gives picks one of several items their words fit equally ("the 11am one") ---
    by_time = _picked_by_time(pt, message, title, titles)
    if by_time is not None:
        return (None, message) if by_time else (refusal, message)
    # --- end picked by its time ---
    if _the_one_listed(naming, shown, title, titles):
        return (None, message) if _the_copy_meant(pt, [message, shown], ids) else (copy_refusal, message)
    if _the_one_whose_time_came(pt, naming, title, titles) and not _another_in_everyday_words(naming, others, titles):
        return None, message
    named_others = [t for t in others if _named(naming, t, titles)]
    # --- other items named: the done says nothing of this one, or names one of them outright, so there's no basis
    # for this one. Another item it may be meant for is the model's to read, with a note (plain_done.py) ---
    if named_others:
        from thirties_core.plain_done import misses_it, named_outright, rivals_named
        if misses_it(naming, title) or any(named_outright(naming, o, title)
                                           for o in rivals_named(pt, naming, title, named_others)):
            return refusal, message  # they're talking about other items
    # --- end other items named ---
    # --- a question about one item, and a done already asked for (done_words.py) ---
    from thirties_core.done_words import NO_RE, asked_about
    from thirties_core.item_facts import item_name
    if not NO_RE.match(message):
        asked = asked_about(shown, titles + _open_items_shown(pt, shown, titles), titles)
        pending = {item_name(p) for p in getattr(pt, "pending_dones", None) or ()}
        if asked == [title] or item_name(title) in pending:
            return (None, message) if _the_copy_meant(pt, [message, shown], ids) else (copy_refusal, message)
    else:
        return refusal, message  # a no points to nothing
    # --- end a question about one item, and a done already asked for ---
    pointed = pointed_back(pt, titles)
    # --- "that" after a list of several (not a question asking which): the one the user had just named ---
    if len(set(pointed)) > 1 and POINTER_RE.search(message) and "?" not in shown and not (
            BOTH_RE.search(message) or PLURAL_RE.search(message) or ORDINAL_RE.search(message)):
        narrowed = [t for t in pointed if _named(said, t, titles)]
        if len(set(narrowed)) == 1:
            pointed = narrowed
    # --- end narrowed ---
    # --- a plain done the model read right, where nothing above points to it: answering the app, its verb and
    # object in other words, or said again (plain_done.py) ---
    from thirties_core.plain_done import trusted

    def plain() -> Tuple[Optional[str], Optional[str]]:
        if not trusted(pt, naming, title, titles, shown):
            return refusal, message
        return (None, message) if _the_copy_meant(pt, [message, shown], ids) else (copy_refusal, message)
    # --- end a plain done ---
    if title not in pointed:
        return plain()
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
    # --- a count pointing back at exactly the items just shown ("all three finished") (done_counts.py) ---
    if not ok:
        from thirties_core.done_counts import pointed_count
        ok = pointed_count(message) == many
    # --- end a count pointing back ---
    if not ok:
        from thirties_core.done_counts import pointed_count
        counted = bool(PLURAL_RE.search(message) or BOTH_RE.search(message) or ORDINAL_RE.search(message)
                       or pointed_count(message))
        if counted:
            # --- a done refused for this item before, said again: it stands (plain_done.said_again) ---
            from thirties_core.plain_done import said_again
            if said_again(pt, message, title):
                return (None, message) if _the_copy_meant(pt, [message, shown], ids) else (copy_refusal, message)
            # --- end said again ---
            return refusal, message  # a count or "both" that doesn't fit what was shown: never read past
        # --- a bare done after a list of several ("that one's sorted", "done!"): the model's pick (_picked) ---
        if _picked(pt, message, title, shown, titles):
            if "?" in shown:  # the reply asked which: a bare done doesn't answer it
                return (f"The user's words don't say which item they finished, and the last reply asked them, so "
                        f"{title} isn't ticked. Ask again which one, in one short question."), message
            picks = getattr(pt, "picked_dones", None)
            if isinstance(picks, set):
                picks.add(title)
        # --- end a bare done ---
        # --- otherwise the words themselves are the done, read as the model read them (plain_done.py), a pointer
        # among them too ("the shawl is done, mark it": "it" is the shawl, not one of the items listed) ---
        else:
            return plain()
    if not _the_copy_meant(pt, [message, shown], ids):
        return copy_refusal, message
    return None, POINTER_RE.sub(title, message)


def skip_check(pt: "PlanTools", title: str, start: Optional[Any] = None) -> Optional[str]:
    """Why a skip can't go ahead: like a done, it needs the user's words to name the item, point back to it or ask
    for many at once, read as check reads them. Besides those, words that name it, words that answer a reply
    listing it without asking anything ("too tired, skipping this morning" after a list), and a stretch of the day
    the words give by two times that the item's planned start (``start``) lies in ("from 1:10 to 2:40 I was out"),
    are enough: which of those items, whether the words mean a skip, and which copy of an item with several, are
    the model's to read. None when it stands, and when nobody is talking."""
    picks = getattr(pt, "picked_dones", None)
    pt.picked_dones = None  # a skip is never a done for the reply to name
    try:
        held, _ = check(pt, title)
    finally:
        pt.picked_dones = picks
    if held is None:
        return None
    message, titles = pt.user_message, _titles(pt)
    if _named(message, title, titles) or ("?" not in message and title in pointed_back(pt, titles)):
        return None
    from thirties_core.move_scope import stretches  # the day's stretches, read as a request to clear one reads them
    if start is not None and any(a <= start < b for a, b in stretches(message, pt.day_plan.day_start)):
        return None
    return (f"The user's words don't point to {title}, so it isn't marked skipped. If they meant to skip something, "
            "ask which item.")


__all__ = ["also_said_note", "check", "done_reading", "pointed_back", "prior_exchange", "skip_check", "BULK_AT_RE",
           "BULK_RE", "MOST_POINTED"]
