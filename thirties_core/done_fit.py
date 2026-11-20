"""A done is for the item the user's words fit, all of them.

People name a finished thing by a short phrase ("the glaze order is sent", "bowls glazed"). The done guards
(done_reference.py for the plan, write_guard.py for Joplin) read that phrase loosely to find the item; this reads it
strictly the other way round: every naming word in the phrase has to be one of the item's words (in any of its
forms, or an everyday word for it: done_words.kin). A phrase that shares a word with the item but carries a naming
word the item doesn't have ("glaze order" beside Glaze recipe test) names something else, so the item isn't ticked
in its place.

- **The phrase**: a run of words next to each other in one clause that names something; small words, done words,
  times, words for how or when ("finally", "this morning") and the like end it.
- **Fits**: some phrase with a word of the item's in it is made only of the item's words. Then the words are about
  this item, as the other checks read them.
- **Names something else**: phrases share the item's words, but none of them fits. Then the item isn't what was
  done; ``elsewhere`` finds the one open list item that fits all the words, if any, for the model to mark instead.
- **A word for which kind**: "the ash paddle is varnished" for Varnish the paddle. Extra words placed ahead of the
  item's own, in a phrase whose last word is the item's, only tell its kind or material; they count against it
  only when another planned or listed item has them. A phrase whose last word the item lacks ("glaze order" beside
  Glaze recipe test, "paddle rack" beside Wax the canoe paddles) still names something else
  (``only_said_which_kind``).
- **Names nothing**: a pointer ("that's done", "yes") or an id has no such phrase; the other checks judge it.
- **A word for how the app shows it**: "the teapot row", "the varnish slot", "that line": a display noun the item
  lacks ("line", "entry", "row", "slot", "listing", "block", "schedule") only points at where the item is shown, so
  the rest of the phrase names it. That settles which item is meant, nothing more: a done still needs the user's
  done words ("the teapot row has the wrong start" isn't one).
- **A word for the thing's papers or bits**: "the firing paperwork's lodged" for Lodge kiln firing permit. A
  container noun ("paperwork", "forms", "stuff", "bits", "errand") the item lacks doesn't make the words name
  something else when the rest of the phrase is the item's, the clause says the item's own verb in the past
  ("lodged" for Lodge), and no other planned or listed item fits the rest of the phrase. A noun for an object of
  its own ("the glaze order is sent" beside Glaze recipe test) still does.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

# words that never name a thing: they end a phrase
_BREAK = {
    # who, and the verbs that say how things stand
    "i", "im", "ive", "id", "ill", "me", "we", "were", "weve", "you", "youre", "they", "theyre", "its",
    "them", "us", "is", "are", "was", "be", "been", "being", "am", "have", "has", "had", "got", "gotten", "get",
    "did", "does", "do", "will", "would", "can", "could", "should", "might", "must", "shall", "isnt", "arent",
    "wasnt", "werent", "dont", "doesnt", "didnt", "havent", "hasnt", "wont", "cant", "couldnt", "shouldnt",
    # small words
    "what", "who", "how", "why", "when", "if", "as", "than", "so", "but", "or", "nor", "yet", "by", "off", "out",
    "over", "about", "around", "after", "before", "since", "until", "till", "through", "via", "per", "like", "too",
    "also", "even", "only", "very", "much", "more", "most", "no", "not", "nope", "yes", "yeah", "yep", "ok", "okay",
    "alright", "sure", "oh", "ah", "um", "uh", "hey", "hi", "lol", "haha", "thx", "pls", "plz", "please", "thanks",
    "here", "now", "then", "there", "one", "ones", "both", "each", "every", "any", "half", "rest", "lot", "bit",
    "few", "couple", "other", "another", "same", "own", "some", "all", "whole", "stuff", "item", "items", "task",
    "tasks", "bits", "old", "new", "big", "small", "extra",
    # everyday verbs said right after an item's name ("the bowls look fine", "the kiln shows hot")
    "say", "says", "show", "shows", "showing", "shown", "look", "looks", "seem", "seems", "need", "needs", "want",
    "wants", "go", "goes", "went", "come", "comes", "came", "make", "makes", "made", "let", "lets", "see", "sees",
    "saw", "feel", "feels", "felt", "think", "know", "knew", "try", "tries", "use", "uses", "give", "gives", "gave",
    "left", "kept", "keeps", "run", "runs", "ran", "sit", "sits", "sat", "stay", "stays", "start", "starts", "stop",
    "stops", "turn", "turns", "took", "takes", "gone", "happening",
    # numbers said in words
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "fifteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "ninety", "hundred", "quarter", "dozen",
    # asking for something else to happen to it (off_today.py, write_guard.py judge those)
    "drop", "clear", "remove", "delete", "take", "skip", "move", "put", "add", "cancel", "scrap", "ditch", "happen",
    "happens",
    # when
    "first", "last", "next", "early", "late", "later", "soon", "ago", "today", "tonight", "tomorrow", "yesterday",
    "morning", "afternoon", "evening", "night", "noon", "midday", "pm", "oclock", "min", "mins", "minute",
    "minutes", "hour", "hours", "week", "day", "again", "already", "still", "just", "finally", "quick", "earlier",
    # done, started, fine, and asking for a tick
    "done", "finished", "finish", "complete", "completed", "sorted", "handled", "set", "ready", "fine",
    "good", "great", "mark", "marked", "tick", "ticked", "check", "checked", "cross", "crossed", "untick",
    "unticked", "unchecked", "unmarked", "leave", "keep", "hold", "wait",
    # said in the past without "-ed": what happened ("lunch eaten", "letter written")
    "eaten", "ate", "written", "wrote", "given", "driven", "drove", "thrown", "threw", "sewn", "drawn", "drew",
    "worn", "torn", "seen", "ridden", "rode", "chosen", "spoken", "spoke", "frozen", "grown", "grew", "blown",
    "flown", "hidden", "begun", "bought", "brought", "caught", "taught", "sold", "told", "found", "built", "sent",
    "spent", "lent", "paid", "swept", "fed", "sung", "swum", "rung",
}
# what a book, a film or a show on the user's media lists is called besides its title
_KIND_WORDS = {"book": ("book", "novel"), "film": ("film", "movie"), "show": ("show", "series", "episode")}
_CLAUSE_RE = re.compile(r"[.;:!?\n,()\"“”+&/]+|\s[-–—]\s|\b(?:and|but|so|then|while|or|plus)\b", re.IGNORECASE)


def _breaks(word: str) -> bool:
    from thirties_core.done_words import _NOT_NAMES
    from thirties_core.item_facts import _MATCH_SKIP, _STOP
    from thirties_core.write_guard import DONE_WORDS_RE, STATE_RE
    return (len(word) < 2 or word.isdigit() or bool(re.match(r"\d", word)) or word in _BREAK or word in _STOP
            or word in _MATCH_SKIP or word in _NOT_NAMES or (len(word) > 4 and word.endswith("ly"))
            or bool(DONE_WORDS_RE.fullmatch(word)) or bool(STATE_RE.fullmatch(word))
            or (len(word) > 4 and word.endswith("ed") and not word.endswith("eed")))  # a past tense: what happened


def _phrases(message: str) -> List[List[str]]:
    """Runs of naming words next to each other, clause by clause."""
    out: List[List[str]] = []
    for clause in _CLAUSE_RE.split(re.sub(r"[‘’'`]", "", str(message or "").lower())):
        run: List[str] = []
        for word in re.findall(r"[a-z0-9]+", clause):
            if _breaks(word):
                if run:
                    out.append(run)
                run = []
            else:
                run.append(word)
        if run:
            out.append(run)
    return out


def _is_its(word: str, title: str) -> bool:
    """The word is one of the item's own words, in any form or as an everyday word for it."""
    from thirties_core.done_words import _words, kin
    from thirties_core.item_facts import _stem
    from thirties_core.write_guard import _IRREGULAR
    mine = {s for s, _ in _words(title)}
    said = (_stem(word), word)
    if any(kin(w, said) for w in mine):
        return True
    raw = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", str(title or "").lower()))
    return any(_IRREGULAR.get(w) == word for w in raw)  # "sent" for Send the parcel


def reading(message: Optional[str], names: Sequence[str],
            others: Iterable[str] = ()) -> Tuple[List[List[str]], List[List[str]]]:
    """(the phrases with a word of the item's, those of them made only of its words). ``names``: the item's
    names (its title on the plan, and the list item it's linked to). A word only another planned item has names
    that item ("bowls paddles done"), so it isn't held against this one."""
    others = [o for o in others if o and o not in names]

    def its(w: str) -> bool:
        return any(_is_its(w, n) for n in names)

    def another(w: str) -> bool:
        return any(_is_its(w, o) for o in others)
    phrases = [[w for w in p if its(w) or not (another(w) or _shown_as(w))] for p in _phrases(message or "")]
    touching = [p for p in phrases if any(its(w) for w in p)]
    return touching, [p for p in touching if all(its(w) for w in p)]


def names_something_else(message: Optional[str], names: Union[str, Sequence[str]],
                         others: Iterable[str] = (), listed: Iterable[str] = ()) -> List[str]:
    """The words of the user's that name something other than this item: phrases share its words but none is
    made only of them ("glaze order" for Glaze recipe test). [] when a phrase fits it, or none touches it, or one
    fits it but for a container noun (``fits_but_for_a_container``). ``listed``: the open items on the user's
    lists, besides the planned ``others``."""
    if not message:
        return []
    names = [names] if isinstance(names, str) else [n for n in names if n]
    others = list(others)
    touching, fitting = reading(message, names, others)
    if not touching or fitting:
        return []
    if fits_but_for_a_container(message, names, others + [t for t in listed if t not in names]):
        return []
    if only_said_which_kind(message, names, others + [t for t in listed if t not in names]):
        return []
    return [" ".join(p) for p in touching]


# --- a word for which kind: "the ash paddle is varnished" names Varnish the paddle ---
def only_said_which_kind(message: str, names: Sequence[str], others: Iterable[str] = ()) -> bool:
    """Every phrase touching the item ends in the item's own words, its head noun last ("paddle" of Varnish the
    paddle), and the extra words ahead of them ("ash" in "the ash paddle") belong to no other planned or listed
    item: they tell its kind, not another thing. The model's reading that the item was meant stands. A phrase that
    ends in the item's verb rather than its head ("the car wash" beside Kiln wash the shelves) names another thing."""
    others = [o for o in others if o and o not in names]

    def its(w: str) -> bool:
        return any(_is_its(w, n) for n in names)

    def its_head(w: str) -> bool:  # the last word of the item's name: its head noun
        return any(_is_its(w, _head(n)) for n in names if _head(n))

    def which_kind(phrase: List[str]) -> bool:
        if not phrase or not its_head(phrase[-1]):
            return False
        # every place the item's own words could start, not only the first word of the item's
        for first in range(1, len(phrase)):
            if all(its(w) for w in phrase[first:]) and not any(_is_its(w, o) for w in phrase[:first] for o in others):
                return True
        return False
    touching = [p for p in ([w for w in phrase if not _shown_as(w)] for phrase in _phrases(message or ""))
                if any(its(w) for w in p)]
    return bool(touching) and all(all(its(w) for w in p) or which_kind(p) for p in touching)


def _head(title: str) -> str:
    """The item's head noun: the last word of its name ("paddle" of Varnish the paddle, "load" of Bisque load)."""
    words = [w for w in re.findall(r"[a-z0-9]+", re.sub(r"[‘’'`]", "", str(title or "").lower())) if not _breaks(w)]
    return words[-1] if words else ""
# --- end a word for which kind ---


# --- a word for how the app shows an item: "the teapot row", "the varnish slot" name the item (which item only;
# whether it's done is the done words' to say: said_for_it.no_done_said) ---
DISPLAY_NOUNS = {"line", "entry", "row", "slot", "listing", "block", "schedule"}


def _shown_as(word: str) -> bool:
    return word in DISPLAY_NOUNS or (word.endswith("s") and word[:-1] in DISPLAY_NOUNS) or \
        (word.endswith("ies") and word[:-3] + "y" in DISPLAY_NOUNS)  # "rows", "entries"
# --- end how the app shows it ---


# --- a word for the thing's papers or bits ---
# nouns for what a thing comes as, not what it is: "the firing paperwork", "the permit forms", "the kiln stuff"
CONTAINER_NOUNS = {"paperwork", "papers", "forms", "form", "stuff", "thing", "things", "bits", "bit", "errand",
                   "errands", "business", "chore", "chores", "job", "jobs", "admin"}


def _container(word: str) -> bool:
    return word in CONTAINER_NOUNS or (word.endswith("s") and word[:-1] in CONTAINER_NOUNS)  # "paperwork's"


def fits_but_for_a_container(message: str, names: Sequence[str], others: Iterable[str] = ()) -> bool:
    """A clause says the item's own verb in the past ("lodged" for Lodge kiln firing permit), and a phrase in it is
    made of the item's words and container nouns ("firing paperwork"), with no other item fitting its other words."""
    from thirties_core.write_guard import _past_of_title
    others = [o for o in others if o and o not in names]
    for clause in _CLAUSE_RE.split(re.sub(r"[‘’'`]", "", str(message or "").lower())):
        mine = [n for n in names if _past_of_title(clause, n)]
        if not mine:
            continue
        for phrase in _phrases(clause):
            words = [w for w in phrase if not _container(w)]
            if len(words) == len(phrase) or not words:
                continue
            if any(all(_is_its(w, n) for w in words) for n in mine) \
                    and not any(all(_is_its(w, o) for w in words) for o in others):
                return True
    return False
# --- end a word for the thing's papers or bits ---


def names_of(pt: Any, title: str, ids: Iterable[str] = ()) -> List[str]:
    """An item's names: its title, and the wording of the list item it's linked to (a planned "Rent" for Pay the
    studio rent)."""
    names = [title]
    ids = set(i for i in ids if i)
    tasks: dict = {}
    for source in (lambda: pt.services.tasks, lambda: pt.assistant.s.tasks):
        try:
            for t in source() or []:
                tasks.setdefault(t.id, t)
        except Exception:
            pass
    try:
        spans = [s for s in pt.day_plan.spans if s.id in ids or s.title == title]
    except Exception:
        spans = []
    linked = {s.task_id for s in spans if getattr(s, "task_id", None)} | ids
    for t in tasks.values():
        if t.id not in linked:
            continue
        if t.title not in names:
            names.append(t.title)
        kinds = [k for k in _KIND_WORDS if k in (getattr(t, "tags", None) or [])]
        if kinds:  # "the lantern book" for Read: The Lantern Almanac
            names.append(t.title + " " + " ".join(w for k in kinds for w in _KIND_WORDS[k]))
    for s in spans:
        try:
            label = pt._title(s)
        except Exception:
            label = ""
        if label and label not in names:
            names.append(label)
    return names


def fits_all(words: str, title: str) -> bool:
    """Every naming word of the phrase is the item's ("glaze order" for Send the glaze order)."""
    phrase = [w for p in _phrases(words) for w in p]
    return bool(phrase) and all(_is_its(w, title) for w in phrase)


def elsewhere(assistant: Any, phrases: Iterable[str]) -> Optional[Any]:
    """The one open list item every word of what the user named fits, or None (none, or several)."""
    if assistant is None:
        return None
    try:
        from thirties_core.done_on_lists import _candidates
        from thirties_core.list_reading import name_of
        tasks = _candidates(assistant)
    except Exception:
        return None
    phrases = [p for p in phrases if p]
    found = [t for t in tasks if any(fits_all(p, name_of(t)) for p in phrases)]
    from thirties_core.item_facts import item_name
    homes = {item_name(t.title) for t in found if t.list_role != "today"}
    found = [t for t in found if t.list_role != "today" or item_name(t.title) not in homes]
    return found[0] if len(found) == 1 else None


def refusal(pt: Any, title: str, named: List[str]) -> str:
    """Why a done for this item doesn't go through when the user's words name something else."""
    said = "; ".join(f'"{p}"' for p in named)
    why = (f"The user's words ({said}) name something other than {title}, so it isn't marked done. ")
    task = elsewhere(getattr(pt, "assistant", None), named)
    if task is not None:
        return why + f"{task.title} on their lists fits what they said: mark that one, with event {task.id}."
    return why + ("No single item on the plan or their lists fits all their words: ask which item they finished, "
                  "in one short question, and record nothing until they say.")


__all__ = ["CONTAINER_NOUNS", "names_something_else", "names_of", "reading", "fits_all",
           "fits_but_for_a_container", "only_said_which_kind", "elsewhere", "refusal"]
