"""A done is for the item the user's words fit, all of them.

People name a finished thing by a short phrase ("the glaze order is sent", "bowls glazed"). The done guards
(done_reference.py for the plan, write_guard.py for Joplin) read that phrase loosely to find the item; this reads it
strictly the other way round: every naming word in the phrase has to be one of the item's words (in any of its
forms, or an everyday word for it: done_words.kin). A phrase that shares a word with the item but carries a naming
word the item doesn't have ("glaze order" beside Glaze recipe test) may name something else. Whether it does is
the model's reading: the done goes through with a note saying what else the words say, so the model can ask or put
it right, also when those words name another planned or listed item. The asks that change the plan without a done
read such words as not pointing at this item (done_reference.check).

- **The phrase**: a run of words next to each other in one clause that names something; small words, done words,
  times, words for how or when ("finally", "this morning") and the like end it.
- **Fits**: some phrase with a word of the item's in it is made only of the item's words. Then the words are about
  this item, as the other checks read them.
- **Names something else**: phrases share the item's words, but none of them fits (``names_something_else``). The
  done goes through, and the result says the words also say that phrase (``also_said``).
- **Names another item**: such a phrase is all another planned or listed item's words ("glaze order" beside Send
  the glaze order), or carries that item's head noun ("the teapot lid" beside Sand the lid) (``named_instead``).
  The asks that change the plan without a done don't read that as this item; ``elsewhere`` finds the one open list
  item that fits all the words, if any.
- **A word for which kind**: "the ash paddle is varnished" for Varnish the paddle. Extra words placed ahead of the
  item's own, in a phrase whose last word is the item's, only tell its kind or material; they count against it
  only when another planned or listed item has them. A phrase whose last word the item lacks ("glaze order" beside
  Glaze recipe test, "paddle rack" beside Wax the canoe paddles) still names something else, and so does one
  without the item's own word for which kind ("the kitchen light" beside Fix the bike light)
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
    # who did it ("fixed the fence myself")
    "myself", "yourself", "ourselves", "themselves", "itself",
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
    """The words of the user's that may name something other than this item: phrases share its words but none is
    made only of them ("glaze order" for Glaze recipe test). [] when a phrase fits it, or none touches it, or one
    fits it but for a container noun (``fits_but_for_a_container``). ``listed``: the open items on the user's
    lists, besides the planned ``others``. Spotted only: whether the words mean another thing is the model's call,
    unless they name another item (``named_instead``)."""
    if not message:
        return []
    from thirties_core.said_for_it import without_ask_words  # "put the bins down as done" names the bins
    message = without_ask_words(message)
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


def named_instead(message: Optional[str], names: Union[str, Sequence[str]],
                  others: Iterable[str] = (), listed: Iterable[str] = ()) -> List[str]:
    """The other planned or listed items the user's words name where they share a word with this item: a phrase
    that may name something else (``names_something_else``) is all of that item's words ("glaze order" for Send
    the glaze order, beside Glaze recipe test), or one of its words this item lacks is that item's head noun ("the
    teapot lid" beside Sand the lid). Ticking this item then would tick one other than the one named."""
    names = [names] if isinstance(names, str) else [n for n in names if n]
    others, listed = list(others), list(listed)
    pool = list(dict.fromkeys(o for o in others + listed if o and o not in names))
    found: List[str] = []
    for phrase in names_something_else(message, names, others, listed):
        extra = [w for w in phrase.split() if not any(_is_its(w, n) for n in names)]
        found += [o for o in pool if o not in found and (fits_all(phrase, o) or any(
            _head(o) and _is_its(w, _head(o)) for w in extra))]
    return found


def also_said(message: Optional[str], names: Union[str, Sequence[str]],
              others: Iterable[str] = (), listed: Iterable[str] = ()) -> str:
    """One plain line for the model when the words may name something besides this item but name no other item:
    what else they say, so it can ask or put it right. "" when there's nothing to say."""
    phrases = names_something_else(message, names, others, listed)
    if not phrases:
        return ""
    said = ", ".join(f'"{p}"' for p in phrases)
    return f"(The user's message also says {said}: if they meant another item, ask which, or correct it.)"


# --- a word for which kind: "the ash paddle is varnished" names Varnish the paddle ---
def only_said_which_kind(message: str, names: Sequence[str], others: Iterable[str] = ()) -> bool:
    """Every phrase touching the item ends in the item's own words, its head noun last ("paddle" of Varnish the
    paddle), and the extra words ahead of them ("ash" in "the ash paddle") belong to no other planned or listed
    item: they tell its kind, not another thing. The model's reading that the item was meant stands. A phrase that
    ends in the item's verb rather than its head ("the car wash" beside Kiln wash the shelves) names another thing,
    and so does one that leaves out the word the item's own name says which kind with ("the kitchen light" beside
    Fix the bike light): the extra word stands in its place."""
    others = [o for o in others if o and o not in names]

    def its(w: str) -> bool:
        return any(_is_its(w, n) for n in names)

    def its_head(phrase: List[str]) -> bool:  # ends in the item's head noun, with its own word for which kind
        return any(_head(n) and _is_its(phrase[-1], _head(n))
                   and (not _kind_word(n) or any(_is_its(w, _kind_word(n)) for w in phrase[:-1])) for n in names)

    def which_kind(phrase: List[str]) -> bool:
        if not phrase or not its_head(phrase):
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


def _kind_word(title: str) -> str:
    """The word right before the item's head noun that says which one it is ("bike" of Fix the bike light, "paddle"
    of Paddle varnish); "" when an article or the like stands there (Varnish the paddle)."""
    words = re.findall(r"[a-z0-9]+", re.sub(r"[‘’'`]", "", str(title or "").lower()))
    head = _head(title)
    at = max((i for i, w in enumerate(words) if w == head), default=-1)
    return words[at - 1] if at > 0 and not _breaks(words[at - 1]) else ""
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


def refusal(pt: Any, title: str, phrases: List[str], items: List[str]) -> str:
    """Why the user's words don't point at this item when they name another item (done_reference.check)."""
    said = "; ".join(f'"{p}"' for p in phrases)
    why = (f"The user's words ({said}) name {', '.join(items)}, not {title}, so {title} isn't marked done. ")
    task = elsewhere(getattr(pt, "assistant", None), phrases)
    if task is not None:
        return why + f"{task.title} on their lists fits what they said: mark that one, with event {task.id}."
    return why + "If they finished that one, mark it; if it isn't clear which, ask in one short question."


__all__ = ["CONTAINER_NOUNS", "names_something_else", "named_instead", "also_said", "names_of", "reading",
           "fits_all", "fits_but_for_a_container", "only_said_which_kind", "elsewhere", "refusal"]
