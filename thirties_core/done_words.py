"""The everyday words people use for an item, and a done they already asked for.

A done goes through when the user's words point to the item (done_reference.py). People rarely repeat a title
word for word, so the words are read loosely here:

- **Word forms**: two words are the same when they share a stem of five letters or more ("varnishing" for
  Varnish) or, both six letters or longer, open with the same four ("surgical" for Surgeon).
- **Common household words**: a small map of everyday synonyms ("pup" for dog, "fed" for feed),
  each in any of its word forms ("puppy" for Dog agility class). It stays small and general.
- **Fed for feed**: "starter got fed" names Feed the sourdough starter.

Across turns:

- **A question about one item**: when the last message the user saw asked whether they meant one item, any reply
  that isn't a no confirms it.
- **A done already asked for**: an item the user asked to mark done in an earlier message, whose done was refused
  and which the reply then named to them ("Did you mean X?"), stays pointed to until it's done or the user says
  no. Their next words ("yes", "yes, the class", a complaint ending in "?") complete it, and the question
  rule (turn_guards.py) doesn't hold it back.

Anything the words never pointed to is still refused.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# synonyms for common household words, stemmed as item_facts._stem stems them
KIN_GROUPS = (
    {"cat", "kitty", "kitten"},
    {"dog", "puppy", "pup"},
    {"coffee", "caffeine"},
    {"med", "medication", "medicine", "pill", "vitamin"},
    {"shower", "wash"},
    {"teeth", "tooth", "brush"},
    {"trash", "garbage", "bin", "rubbish"},
    {"laundry", "wash"},
    {"feed", "fed"},
)
_KIN: Dict[str, set] = {}
for _group in KIN_GROUPS:
    for _word in _group:
        _KIN.setdefault(_word, set()).update(_group)

# words in a message that say how or when, never which item: no loose match on these
_NOT_NAMES = {"done", "finished", "finish", "complete", "completed", "already", "today", "tonight", "morning",
              "evening", "afternoon", "yesterday", "earlier", "before", "after", "again", "still", "really",
              "actually", "literally", "marked", "ticked", "checked", "crossed", "said", "told", "talked", "talking",
              "asked", "meant", "thing", "things", "something", "everything", "anything", "nothing", "minutes",
              "hours", "little", "while", "about", "thanks", "please", "should", "would", "could", "there", "where",
              "which", "these", "those", "think", "thought", "right", "wrong", "always", "never"}

# a no: the reply turns the question down, or calls the done off
NO_RE = re.compile(r"^\W*(?:no|nope|nah|not (?:that|it|yet|really)|wrong|neither|none|never ?mind|nvm|forget it|"
                   r"don'?t (?:mark|tick|bother)|cancel|the other)\b", re.IGNORECASE)


def says_no(message: Optional[str]) -> bool:
    """The reply is a no (``NO_RE``). A no that goes with a "not" about the talk isn't one ("no need to ask me twice",
    "no, not saying it a second time": started.about_the_talk)."""
    found = NO_RE.match(message or "")
    if not found:
        return False
    from thirties_core.started import about_the_talk
    return not (about_the_talk(message) or about_the_talk(message[found.end():]))


# a message asking for a done: said in so many words, or a past tense ("fed", "walked")
_DONE_ASK_RE = re.compile(r"\b(?:mark|tick|check|cross)\w*\b", re.IGNORECASE)
# the refusal done_reference.check gives, as it reads in the conversation
_REFUSED_RE = re.compile(r"The user's words don't point to (.+?), so it isn't marked done\.")
# how many of the user's messages back a done they asked for still counts
TURNS_BACK = 6


def _words(text: Any) -> List[tuple]:
    """(stem, word) for each word of the text that can name something."""
    from thirties_core.item_facts import _MATCH_SKIP, _STOP, _stem
    out = []
    for w in re.findall(r"[a-z0-9]+", re.sub(r"[‘’'`]", "", str(text or "").lower())):
        if len(w) > 1 and not w.isdigit() and (w not in _STOP or w == "feed"):
            stem = _stem(w)
            if stem not in _MATCH_SKIP:
                out.append((stem, w))
    return out


def _common(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def kin(title_word: str, said: tuple) -> bool:
    """Whether a word of the user's (stem, word) is this word of an item's title, read loosely."""
    stem, raw = said
    if stem == title_word or any(g == title_word or _common(g, title_word) >= 5 for g in _KIN.get(stem, ())):
        return True  # a synonym, in any of its forms
    if raw in _NOT_NAMES or stem in _NOT_NAMES:
        return False
    if _common(stem, title_word) >= 5:
        return True
    return len(stem) >= 6 and len(title_word) >= 6 and stem[:4] == title_word[:4]


def _hits(said: List[tuple], title: str) -> set:
    mine = {s for s, _ in _words(title)}
    return {w for w in mine if any(kin(w, s) for s in said)}


def named(text: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """Whether the text names this item in everyday words: all of its words, a word no other item has, or more of
    its words than any other item that shares them (as item_facts.referenced reads it, with words read loosely)."""
    from thirties_core.item_facts import item_name
    if not text:
        return False
    said = _words(text)
    mine = {s for s, _ in _words(title)}
    hits = _hits(said, title)
    if not mine or not hits:
        return False
    if hits == mine:
        return True
    rivals = [o for o in others if item_name(o) != item_name(title)]
    rival_words = [{s for s, _ in _words(o)} for o in rivals]
    if any(not any(kin(w, (r, r)) or kin(r, (w, w)) for words in rival_words for r in words) for w in hits):
        return True  # a word only this item has
    return all(len(hits) > len(_hits(said, o)) for o in rivals if _hits(said, o))


# --- a question about one item, and a done already asked for ---

# a question about which item it was, or whether it got done; an offer ("want me to move it?") is no such question
_WHICH_OR_DONE_RE = re.compile(r"\b(?:mean|meant|talking about|referring|which|did you|have you|are you done|"
                               r"is (?:it|that)|was (?:it|that)|done|finished|get to|got to)\b", re.IGNORECASE)


def _last_question(shown: str) -> str:
    asked = re.findall(r"[^.!?\n]+\?", shown or "")
    return asked[-1] if asked and _WHICH_OR_DONE_RE.search(asked[-1]) else ""


def asked_about(shown: str, pool: Sequence[str], titles: Sequence[str]) -> List[str]:
    """The items the last question in the message the user saw names ("Did you mean the puppy's class?")."""
    question = _last_question(shown)
    if not question:
        return []
    from thirties_core.done_reference import _listed
    return list(dict.fromkeys(t for t in pool if _listed(question, t) or named(question, t, titles)))


def pending_dones(messages: Sequence[dict]) -> List[str]:
    """The items the user asked to mark done in their recent messages whose done was refused, and that haven't been
    marked since or been called off with a no. The reply after the refusal has to name the item ("Did you mean
    X?"): what the user answers is then about it. Read from the conversation before this turn."""
    from thirties_core.started import FINISHED_RE, said_not_done

    def visible_user(m: dict) -> bool:
        text = str(m.get("content") or "")
        return (m.get("role") == "user" and bool(text.strip()) and not m.get("hidden")
                and not text.startswith("[System check"))
    starts = [i for i, m in enumerate(messages) if visible_user(m)]
    if not starts:
        return []
    window = messages[starts[-TURNS_BACK] if len(starts) >= TURNS_BACK else starts[0]:]
    from thirties_core.done_reference import _listed
    pending: Dict[str, str] = {}
    refused: Dict[str, str] = {}  # refused this turn: asked for only once the reply names it to the user
    said = ""
    for m in window:
        if visible_user(m):
            said = str(m.get("content") or "")
            refused = {}
            for title in list(pending):
                if says_no(said) or said_not_done(said, title):
                    pending.pop(title)
        elif m.get("role") == "tool" and m.get("name") == "log_outcome":
            content = str(m.get("content") or "")
            found = _REFUSED_RE.search(content)
            if found and (FINISHED_RE.search(said) or _DONE_ASK_RE.search(said)):
                refused[found.group(1)] = said
            elif not content.startswith("NOT_DONE"):
                for title in list(pending) + list(refused):
                    if title in content:
                        pending.pop(title, None)
                        refused.pop(title, None)
        elif m.get("role") == "assistant" and not m.get("hidden") and str(m.get("content") or "").strip():
            shown = str(m.get("content"))
            for title in list(refused):
                if _listed(shown, title) or named(shown, title):
                    pending[title] = refused.pop(title)
    return list(pending)


def earlier_request(pt: Any, args: Dict[str, Any]) -> bool:
    """Whether this log_outcome completes a done the user asked for in an earlier turn: the question rule doesn't
    hold it back."""
    pending = getattr(pt, "pending_dones", None) or ()
    if not pending or str(args.get("status") or "").strip().lower() != "done":
        return False
    from thirties_core.item_facts import item_name
    event = str(args.get("event") or "").strip()
    titles = {event}
    try:
        span = pt._find_span(event)
        if span is not None:
            titles.add(span.title)
    except Exception:
        pass
    return any(item_name(t) == item_name(p) for t in titles for p in pending)
# --- end a question about one item, and a done already asked for ---


__all__ = ["KIN_GROUPS", "NO_RE", "asked_about", "earlier_request", "kin", "named", "pending_dones", "says_no"]
