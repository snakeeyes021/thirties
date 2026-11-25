"""How a done's words fit the items: what the notes on a done say, and what the asks that change the plan read.

Which item the user means is the model's call: an everyday name or a nickname for it, a reply that refers back
to what the app asked, the done said again in exasperation. A done never stops here (said_for_it.py holds the only
stops: nothing said is a done, or the item named as not done). ``also_fits`` gives the note a done's result
carries when the words may be meant for another item (done_reference.also_said_note); done_reference.check reads
the rest for the asks that change the plan without a done. What it finds:

- **No done said**: nothing in the words says it happened or confirms it ("the chooks are getting watered" is
  under way, not done).
- **Something else named**: the words name another item outright (all its words, or its head noun: done_fit.py),
  or the clauses that say the done name other items and say nothing of this one ("The bench can wait, the skis got
  waxed" for Fix the bench: ``misses_it``).
- **A plain tie**: the words fit another item still to do as well as this one, and say nothing beyond what the two
  share ("the watering's done" beside Water the hens and Water the goats). A word that names no item ("the chooks
  got watered") is what the model read to pick this one.
- **Nothing about it**: no word of the item, in any of its forms ("laundry done" for a vet visit, a bare "mark it
  done"), and no reply of the app's naming it to answer.

The words point to the item, on the model's reading (``trusted``):

- **Answering the app**: the app's last reply named this item and no other one still to do, and the user confirms
  (a yes, a request for the tick, an insisting word) or says it happened.
- **The model's reading**: a done said with a word of the item (its verb, say) and the rest in the user's own
  words, with nothing that names another item and no plain tie. When another item may be the one meant (it fits
  the words better, done or not, a routine or a list item: "the goats got watered" for Brush the goats beside
  Water the goats; or it's named in the done and still to do), the done goes on and its result names that item, so
  the model can put it right (``also_fits``). An item already done, one named only beside the done ("Skip
  the bench"), a later item whose name is a word of this one's, and any, when the words also say a word of this
  item that none of them has, aren't named.
- **Said again**: a done the user asked for earlier, refused for this same item, said again.

Complaints beside a done ("quit checking on that one", "you don't need to ask") are talk to the app, never a
word about the item: they don't make the done read as under way or not done (write_guard.state_not_done).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

# words before a past that make it under way or still to come ("getting watered", "to be watered")
_UNDER_WAY = {"getting", "being", "get", "be", "gets", "need", "needs", "want", "wants", "to"}
# a reply that confirms what the app just named, or insists on it
CONFIRM_RE = re.compile(
    r"\b(?:yes|yep|yeah|yup|yea|correct|right|exactly|sure|indeed|that one|this one|the one|that'?s (?:it|the one)|"
    r"obvious(?:ly)?|of course|duh|clearly|already (?:told|said)|(?:told|tell) you|i (?:just |already )?said|"
    r"said so|said that|how many times|(?:mark|tick|check|cross|log)\w*\s+(?:it|that|this|them|those|off|done)|"
    r"do it|just do|go ahead)\b", re.IGNORECASE)

# a clause said to the app about the conversation ("quit checking on that one", "you don't need to ask")
_TO_THE_APP_RE = re.compile(
    r"\b(?:you|your|you'?re|youre|u|ur|yourself)\b|\b(?:stop|quit|keep)\s+(?:asking|checking|bugging|nagging)\b|"
    r"\b(?:asking|ask me|told|said|obvious(?:ly)?|how many times)\b", re.IGNORECASE)
# a pointer to the item as what the clause is about ("it isn't", "that's not", "they're still"): never talk to the app
_ABOUT_A_POINTER_RE = re.compile(
    r"\b(?:it|that|this|they|them|those|these)(?:'s|'re|s|re)?\s+(?:is\s+|are\s+|was\s+|were\s+)?(?:still\s+)?"
    r"(?:not|n'?t|isn'?t|aren'?t|wasn'?t|weren'?t|running|going|on|fine|ok|okay|good|started)\b", re.IGNORECASE)


def _stems(text: Any) -> List[tuple]:
    from thirties_core.done_words import _words
    return _words(text)


def _open_titles(pt: Any) -> List[str]:
    from thirties_core.done_reference import _still_to_do
    return list(dict.fromkeys(s.title for s in _still_to_do(pt) if s.title))


# words that say how, or ask for the tick, never which item
_NOT_WHICH = {"yes", "yep", "yeah", "yup", "yea", "correct", "exactly", "sure", "indeed", "obvious", "obviously",
              "course", "duh", "clearly", "mark", "tick", "check", "cross", "log", "please", "now", "ok", "okay",
              "one", "too", "also", "finally", "just", "all", "got", "get", "are", "is", "was", "were", "have", "has",
              "had", "been", "its", "they", "them", "theyre", "those", "these", "it", "that", "this", "off", "go",
              "ahead", "so", "say", "times", "many", "how", "i", "you", "me", "we", "us", "lol", "ugh", "omg"}


def _which_words(message: str) -> List[tuple]:
    """(stem, word) for each word of the message that can say which item: not a done word, a yes, a pointer or a
    word saying how."""
    from thirties_core.done_words import _NOT_NAMES
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import DONE_WORDS_RE
    out = []
    for stem, word in _stems(message):
        if word in _NOT_NAMES or stem in _NOT_NAMES or word in _NOT_WHICH or stem in _NOT_WHICH:
            continue
        if DONE_WORDS_RE.fullmatch(word) or (FINISHED_RE.fullmatch(word) and not word.endswith("ed")):
            continue
        out.append((stem, word))
    return out


def _other_names(pt: Any, title: str) -> List[str]:
    """Everything else the words could be about: the other items on the plan (done ones too), the routines this item
    isn't a step of, and what is still open on the lists."""
    from thirties_core.done_reference import _listed_items, _titles
    from thirties_core.item_facts import item_name
    names = list(_titles(pt))
    try:
        from thirties_core.whole_routine import _routines
        names += [r["name"] for r in _routines(pt) if not any(sp.title == title for sp in r["spans"])]
    except Exception:
        pass
    names += _listed_items(pt, [title])
    return [n for n in dict.fromkeys(names) if n and item_name(n) != item_name(title)]


# --- the part of the message that says the done ---
def done_clauses(message: str) -> List[str]:
    """The clauses of the message with a done word or a past in them ("the skis got waxed" in "The bench can wait,
    the skis got waxed"): what a done is read from. Every such clause counts, however it puts the done ("got to
    finish the tapers"): whether it says the item happened is the model's reading. A list ahead of the done shares
    it: names alone, joined to it by commas or "and" ("bobbins and dusting and shuttle oiling are all wrapped up").
    The whole message when no clause has one."""
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import _CLAUSE_RE, DONE_WORDS_RE
    parts = re.split(f"({_CLAUSE_RE.pattern})", message or "", flags=re.IGNORECASE)
    pieces, seps = parts[0::2], parts[1::2]
    said = []
    for i, piece in enumerate(pieces):
        if not piece.strip() or not (DONE_WORDS_RE.search(piece) or FINISHED_RE.search(piece)):
            continue
        first = i
        while first > 0 and _LIST_SEP_RE.fullmatch(seps[first - 1]) and _a_name_alone(pieces[first - 1]):
            first -= 1
        said.append("".join(parts[2 * first:2 * i + 1]))
    return said or [message or ""]


# what joins the entries of a list
_LIST_SEP_RE = re.compile(r",|and", re.IGNORECASE)


def _a_name_alone(piece: str) -> bool:
    """The stretch is nothing but a name ("bobbins", "the dusting"): one run of naming words, with only small words
    beside it (done_fit.py reads the runs)."""
    from thirties_core.done_fit import _phrases
    from thirties_core.item_facts import _STOP
    runs = _phrases(piece)
    words = re.findall(r"[a-z0-9]+", re.sub(r"[‘’'`]", "", (piece or "").lower()))
    return len(runs) == 1 and all(w in runs[0] or w in _STOP for w in words)


def _in_the_done(message: str) -> str:
    return " ; ".join(done_clauses(message))


def _a_word_of_its_own(said: List[tuple], title: str, others: Iterable[str]) -> bool:
    """A word of the user's (stem, word) fits a word of this item and no word of the others: the words name this
    item beside them, however well they fit the others ("treadle" in "Someone shifted my selvedge swatch over to
    1:10. Treadle: wrapped up.")."""
    from thirties_core.done_words import _words, kin
    mine = {s for s, _ in _words(title)}
    theirs = {s for o in others for s, _ in _words(o)}
    return any(any(kin(t, w) for t in mine) and not any(kin(t, w) for t in theirs) for w in said)


def misses_it(message: str, title: str) -> bool:
    """Nothing in the clauses that say the done touches this item ("The bench can wait, the skis got waxed" for Fix
    the bench): the done is said of something else, so the words give no basis for this one."""
    from thirties_core.done_words import _hits
    return not _hits(_stems(_in_the_done(message)), title)


def named_outright(message: str, other: str, title: str) -> bool:
    """The clauses that say the done name the other item outright, not this one: a phrase in them is made only of
    the other item's words with one this item lacks ("dog agility done" for Take the puppy to agility, beside
    Agility drills), or every naming word of the other item is said ("the hens got watered" for Water the hens)."""
    from thirties_core.done_fit import _is_its, _phrases, fits_all
    from thirties_core.done_words import _words
    done = _in_the_done(message)
    if any(fits_all(" ".join(p), other) and not fits_all(" ".join(p), title) for p in _phrases(done)):
        return True
    said = [w for _, w in _words(done)]
    theirs = [w for _, w in _words(other)]
    return bool(theirs) and all(any(_is_its(s, t) for s in said) for t in theirs)


def rivals_named(pt: Any, message: str, title: str, named: Iterable[str]) -> List[str]:
    """Of the other items the message names, those the done may be meant for instead: still to do, named in a clause
    that says the done, and not fitting those words only with words this item has too, no better than it does; or
    fitting the words better, done or not. An item already done or skipped (a routine step ticked this morning), one
    named only beside the done ("Skip the bench"), and a later item whose name is a word of this one's (Race beside
    Wax the skis for the race) are not. [] when the words say a word of this item that none of them has (it's named
    beside them)."""
    from thirties_core.done_reference import _named, _titles
    from thirties_core.done_words import _hits, _words
    from thirties_core.item_facts import item_name
    open_names = {item_name(t) for t in _open_titles(pt)}
    titles = _titles(pt)
    clauses = done_clauses(message)
    mine_words = {s for s, _ in _words(title)}
    named = [o for o in named if item_name(o) != item_name(title)]
    said = _stems(" ; ".join(clauses))
    if named and _a_word_of_its_own(said, title, named):
        return []  # it's named beside them ("Someone shifted my selvedge swatch over to 1:10. Treadle: wrapped up.")
    out = []
    for other in named:
        if len(_hits(said, other)) > len(_hits(said, title)):
            out.append(other)  # the words fit it better, done or not
            continue
        if item_name(other) not in open_names:
            continue
        for clause in clauses:
            if not _named(clause, other, titles):
                continue
            words = _stems(clause)
            theirs, mine = _hits(words, other), _hits(words, title)
            if theirs and theirs <= mine_words and len(mine) >= len(theirs):
                continue  # every word naming it names this one too: the plain-tie check reads it
            out.append(other)
            break
    return out


def also_fits(pt: Any, message: str, title: str, besides: Iterable[str] = ()) -> List[str]:
    """The other items the done may be meant for instead of this one: those named that can be the done in its place
    (``rivals_named``), and any item the words of the done fit better, done or not, a routine or a list item ("the
    goats got watered" for Brush the goats beside Water the goats), when they say no word of this one that the
    other lacks. Whether the user meant one of them is the model's reading; [] when the words say nothing of this
    item (``misses_it``: no basis, refused before)."""
    from thirties_core.done_reference import _named, _titles
    from thirties_core.done_words import _hits
    if misses_it(message, title):
        return []
    left_out = set(besides)
    titles = _titles(pt)
    named = [t for t in titles if t != title and t not in left_out and _named(message, t, titles)]
    said = _stems(_in_the_done(message))
    mine = _hits(said, title)
    better = [o for o in _other_names(pt, title) if o not in left_out and len(_hits(said, o)) > len(mine)]
    if better and _a_word_of_its_own(said, title, better):
        better = []
    return list(dict.fromkeys(rivals_named(pt, message, title, named) + better))
# --- end the part that says the done ---


def by_the_models_reading(pt: Any, message: str, title: str) -> bool:
    """A done said in the user's own words, with a word of the item in the clauses that say it, and with no plain
    tie: no other item still to do the words fit as well while saying nothing beyond what the two share, and no
    other item the words fit better named outright (``named_outright``). Another item the words fit better is
    otherwise the model's to read, with a note (``also_fits``). Only the clauses that say the done are read: an item
    named beside it ("the bench can wait") is no rival."""
    from thirties_core.done_words import _hits, kin
    from thirties_core.item_facts import item_name
    if not says_it_happened(message):
        return False
    done = _in_the_done(message)
    which = _which_words(done)
    said = _stems(done)
    mine = _hits(said, title)
    if not mine:
        return False  # nothing in the words touches the item: no basis for it
    if any(named_outright(message, o, title) for o in also_fits(pt, message, title)):
        return False  # the words name another item outright: never this one in its place
    rivals = [o for o in _open_titles(pt) if item_name(o) != item_name(title)]
    level = [o for o in rivals if _hits(said, o) and len(_hits(said, o)) == len(mine)]
    if not level:
        return True
    shared = [w for t in [title] + level for w, _ in _stems(t)]
    return any(not any(kin(t, sw) for t in shared) for sw in which)  # a word beyond the tie: the model's to read


def _named_in(shown: str, pool: Sequence[str], titles: Sequence[str]) -> List[str]:
    from thirties_core.done_reference import _listed
    from thirties_core.done_words import named
    return list(dict.fromkeys(t for t in pool if _listed(shown, t) or named(shown, t, titles)))


def says_it_happened(message: str) -> bool:
    """The words say something happened: a done word or a past, not one still under way or to come ("getting
    watered", "needs to be fed")."""
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import DONE_WORDS_RE
    for m in list(DONE_WORDS_RE.finditer(message or "")) + list(FINISHED_RE.finditer(message or "")):
        before = re.findall(r"[a-z]+", (message or "")[:m.start()].lower())
        if not before or before[-1] not in _UNDER_WAY:
            return True
    return False


def answers_the_app(pt: Any, message: str, title: str, titles: Sequence[str], shown: str) -> bool:
    """The app's last reply named this item and no other one still to do, and the user confirms it or says it
    happened."""
    if not shown or not (CONFIRM_RE.search(message) or says_it_happened(message)):
        return False
    from thirties_core.done_reference import _open_items_shown
    from thirties_core.item_facts import item_name
    pool = list(dict.fromkeys([title] + _open_titles(pt) + _open_items_shown(pt, shown, list(titles))))
    named = {item_name(t) for t in _named_in(shown, pool, titles)}
    return named == {item_name(title)}


def said_again(pt: Any, message: str, title: str) -> bool:
    """A done the user asked for in an earlier message, refused for this same item, said again: the words share
    what names the thing with that earlier request."""
    from thirties_core.item_facts import item_name, match_words
    from thirties_core.done_words import _NOT_NAMES
    asked = getattr(pt, "done_asks", None) or {}
    earlier = [said for t, said in asked.items() if item_name(t) == item_name(title)]
    if not earlier or not says_it_happened(message):
        return False
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import DONE_WORDS_RE

    def naming(text: str) -> set:
        return {w for w in match_words(text) if w not in _NOT_NAMES and not FINISHED_RE.fullmatch(w)
                and not DONE_WORDS_RE.fullmatch(w)}

    def plain(text: str) -> str:
        return " ".join(re.findall(r"[a-z]+", text.lower()))
    mine = naming(message)
    return any(mine & naming(said) or plain(said) == plain(message) for said in earlier)


def trusted(pt: Any, message: str, title: str, titles: Sequence[str], shown: str) -> bool:
    """Whether the model's reading of which item the user means stands (the module docstring says when)."""
    return (answers_the_app(pt, message, title, titles, shown) or by_the_models_reading(pt, message, title)
            or said_again(pt, message, title))


# --- the dones asked for before, and refused as not pointing to the item ---
_REFUSED_RE = re.compile(r"The user's words don't point to (.+?), so it isn't marked done\.")


def done_asks(messages: Sequence[dict]) -> Dict[str, str]:
    """{item: what the user said} for each done the user asked for in their recent messages that was refused as
    not pointing to the item, and that hasn't been marked since or been called off with a no. Read from the
    conversation before this turn."""
    from thirties_core.done_words import TURNS_BACK, _DONE_ASK_RE, says_no
    from thirties_core.started import FINISHED_RE, said_not_done

    def visible_user(m: dict) -> bool:
        text = str(m.get("content") or "")
        return (m.get("role") == "user" and bool(text.strip()) and not m.get("hidden")
                and not text.startswith("[System check"))
    starts = [i for i, m in enumerate(messages) if visible_user(m)]
    if not starts:
        return {}
    asks: Dict[str, str] = {}
    said = ""
    for m in messages[starts[-TURNS_BACK] if len(starts) >= TURNS_BACK else starts[0]:]:
        if visible_user(m):
            said = str(m.get("content") or "")
            for title in list(asks):
                if says_no(said) or said_not_done(said, title):
                    asks.pop(title)
        elif m.get("role") == "tool" and m.get("name") == "log_outcome":
            content = str(m.get("content") or "")
            found = _REFUSED_RE.search(content)
            if found and (FINISHED_RE.search(said) or _DONE_ASK_RE.search(said)):
                asks[found.group(1)] = said
            elif not content.startswith("NOT_DONE"):
                for title in list(asks):
                    if title in content:
                        asks.pop(title)
    return asks
# --- end the dones asked for before ---


# --- talk to the app beside a done ---
def to_the_app(clause: str) -> bool:
    """The clause is said to the app about the conversation ("quit checking on that one", "you don't need to
    ask"), not about the item: no pointer to the item is what it says is under way or not done."""
    return bool(_TO_THE_APP_RE.search(clause or "")) and not _ABOUT_A_POINTER_RE.search(clause or "")


def without_talk_to_the_app(clauses: Iterable[str], message: str) -> List[str]:
    """The clauses that can say something about an item: talk to the app is left out when the message also says
    it happened or confirms it."""
    clauses = list(clauses)
    if not (says_it_happened(message) or CONFIRM_RE.search(message)):
        return clauses
    return [c for c in clauses if not to_the_app(c)]
# --- end talk to the app ---


__all__ = ["CONFIRM_RE", "also_fits", "answers_the_app", "by_the_models_reading", "done_asks", "misses_it",
           "named_outright", "rivals_named", "said_again", "to_the_app", "trusted", "without_talk_to_the_app"]
