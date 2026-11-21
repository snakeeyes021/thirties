"""Done only for what the user is talking about.

Marking something done ticks it off in the user's lists, so which item it is is never the model's guess. A done
goes through when the user's words point at the item:

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
- **A whole routine**: its name with "all", "whole" or "everything", or "both" / "all of them" to a question about
  its parts: every step of it, in every part (whole_routine.py).
- **An answer to "which one?"**: "the first one", "the last one" for that one of the items the question listed, in
  the order it listed them (open list items it named count too, planned or not).
- **The one just listed**: when the words fit several items equally and the last message the user was shown listed
  exactly one of them ("Not marked yet: Glaze run" beside a Glaze run review call), it's that one.
- **A plain done**: the app's last reply named only this item and the user confirms it or says it happened; the
  item's verb in the past with its object in everyday words ("the chooks got watered") and no other item still to
  do fitting as well; or a done refused earlier for this item, said again (plain_done.py).
- **The one whose time has come**: when the words fit several items equally ("the walk's done" with three walks),
  or the item has copies under one name, it's the one still to do that began last (or is due within the half
  hour: a done is often said a little early); before any has begun, the next one. A copy named by its start time
  ("the 8am walk") is that copy.

Whatever points to it, words that share a word with the item but name something else ("the glaze order is sent"
beside a planned Glaze recipe test) never mark it in that thing's place (done_fit.py): the note names the list
item that fits all the words, or says to ask.

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
    # --- words naming something else that shares a word with it: never this item in its place (done_fit.py) ---
    from thirties_core.done_fit import names_of, names_something_else, refusal as named_else
    names = names_of(pt, title, ids)
    elsewhere = names_something_else(naming, names, others, _listed_items(pt, names))
    if elsewhere:
        return named_else(pt, title, elsewhere), message
    # --- end something else ---
    if BULK_RE.search(message):
        return None, message
    said, shown = getattr(pt, "prior_exchange", None) or ("", "")
    # --- a whole routine said done: every step of it, in every part (whole_routine.py) ---
    from thirties_core.whole_routine import routine_meant
    if routine_meant(pt, message, shown, title):
        return None, message
    # --- end a whole routine ---
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
    # --- only another item that can be the done in this one's place is a rival (plain_done.rivals_named) ---
    if named_others:
        from thirties_core.plain_done import rivals_named
        if rivals_named(pt, naming, title, named_others):
            return refusal, message  # they're talking about other items
    # --- end rivals ---
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
        # --- otherwise the words themselves are the done, read as the model read them (plain_done.py) ---
        elif not POINTER_RE.search(message):
            return plain()
        else:
            return refusal, message
    if not _the_copy_meant(pt, [message, shown], ids):
        return copy_refusal, message
    return None, POINTER_RE.sub(title, message)


__all__ = ["check", "pointed_back", "prior_exchange", "BULK_RE", "MOST_POINTED"]
