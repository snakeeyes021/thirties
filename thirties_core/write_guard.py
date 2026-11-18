"""The last check before Joplin is written during a conversation: the user's own words have to say it.

Two kinds of change can't be undone by just taking the plan back, so the user's message for the turn has to ask
for them in plain words:

- **Filed as dropped, or gone from its own list.** Filing an item in the month's Dropped note, or removing its line
  from one of the user's lists (any note but Today), needs a drop word: "drop it", "I'm not doing it", "forget it",
  "cross it off for good", "delete it", "never going to". Taking something off today ("take that off", "skip it",
  "not today") only changes today's plan and the Today note; the item stays to do on its list.
- **Ticked done.** A tick needs the user saying it happened: a done word ("done", "finished", "paid", "sent",
  "all set"), the item's own verb in the past ("renewed" for Renew the kiln permit), or a plain yes to a question.
  Words that say something is under way or merely all right ("the kiln is firing", "it's running", "the glaze is
  fine") are never a done, for the item they're said about, however loosely the words point to it.

Changes made outside a turn (the app's own controls, the background, this morning's housekeeping) aren't judged
here: they have their own rules.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# the user asking for an item to be let go for good
DROP_WORDS_RE = re.compile(
    r"\bdrop(?:s|ped|ping)?\b(?!\s+(?:off|by|in|round|over)\b)|"
    r"\b(?:i'?m|i am|we'?re|we are)\s+not\s+(?:doing|gonna do|going to do)\s+(?:it|that|this|them|those|the\b[^.!?\n]{0,30})"
    r"(?![^.!?\n]{0,20}\b(?:today|tonight|now|yet|this (?:morning|afternoon|evening)))|"
    r"\bforget (?:about )?(?:it|that|this|them|those)\b(?![^.!?\n]{0,12}\b(?:for )?(?:today|tonight|now)\b)|"
    r"\b(?:for good|for ever|forever|permanently|altogether|never (?:going to |gonna )?(?:do|doing)\b)\b|"
    r"\b(?:delete|archive|scrap|ditch|get rid of|let (?:it|that|them) go|no longer (?:need|want|doing))\b",
    re.IGNORECASE)

# done in so many words
DONE_WORDS_RE = re.compile(
    r"\b(?:done|finished|finish(?:ed)? (?:it|that|them)|complete[ds]?|paid|sent|submitted|mailed|posted|returned|"
    r"bought|handed in|dropped off|picked up|signed|renewed|booked|fixed|repaired|cleaned|ticked|checked off|"
    r"crossed off|all set|taken care of|took care of|sorted|wrapped up|over with|handled|did (?:it|that|them)|"
    r"got (?:it|that|them) done)\b", re.IGNORECASE)
# a plain yes, answering a question the user was asked
YES_RE = re.compile(r"^\W*(?:yes|yep|yeah|yup|yea|correct|right|exactly|that one|that'?s (?:it|the one)|sure)\b",
                    re.IGNORECASE)
# under way, or merely all right: never a done
STATE_RE = re.compile(
    r"\b(?:running|going|firing|started|starting|underway|under way|in progress|on its way|heating|warming|"
    r"cooling|soaking|drying|loading|working|fine|ok|okay|alright|all right|good)\b|"
    r"\bon\s*$", re.IGNORECASE)  # "the kiln is on", said at the end; "worked on the railing" is something else
_NOT_DONE_RE = re.compile(r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|isn'?t|wasn'?t|yet to)\b|n't\b", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[.;!?\n,]+|\b(?:and|but|so|then|while)\b", re.IGNORECASE)
# past tenses that aren't the thing happening
_NOT_PAST = {"started", "scheduled", "planned", "needed", "wanted", "supposed", "tired", "bored", "kicked", "turned",
             "asked", "wondered", "guessed", "worried", "listed", "added", "moved", "pushed", "postponed"}
_IRREGULAR = {"buy": "bought", "make": "made", "write": "wrote", "take": "took", "get": "got", "do": "did",
              "go": "went", "send": "sent", "pay": "paid", "bring": "brought", "throw": "threw", "find": "found",
              "sell": "sold", "tell": "told", "sweep": "swept", "build": "built", "lend": "lent", "spend": "spent", "hang": "hung", "wind": "wound", "drive": "drove", "give": "gave", "ring": "rang", "throw": "threw", "pick": "picked", "put": "put", "set": "set", "run": "ran"}


def _said(turn: str) -> str:
    """The user's words from the turn's record ("[app] words / more words")."""
    return re.sub(r"^\[[^\]]*\]\s*", "", str(turn or "")).strip()


def asks_to_drop(message: Optional[str]) -> bool:
    """The user asks for something to be let go for good, not just taken off today."""
    return bool(message and DROP_WORDS_RE.search(message))


def _past_of_title(message: str, title: str) -> bool:
    """A word of the message is one of the item's own words in the past ("renewed" for Renew the kiln permit)."""
    from thirties_core.done_words import kin
    from thirties_core.item_facts import _stem
    words = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", title.lower()))
    said = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", message.lower()))
    for w in words:
        if _IRREGULAR.get(w) in said:
            return True
    stems = {_stem(w) for w in words if len(w) > 2}
    for s in said:
        if len(s) >= 5 and s.endswith("ed") and s not in _NOT_PAST:
            if any(kin(t, (_stem(s), s)) for t in stems):
                return True
    return False


def _about(clause: str, title: str, others: list) -> bool:
    """The clause names the item: as started reads it, or loosely (done_words.named). Loose words only point."""
    from thirties_core.done_words import named
    from thirties_core.started import _names
    return _names(clause, title, others) or named(clause, title, others)


def _done_in(clause: str, title: str) -> bool:
    return bool(DONE_WORDS_RE.search(clause) or _past_of_title(clause, title))


def state_not_done(message: Optional[str], title: str, others=()) -> str:
    """Why the user's words say this item isn't finished: what they say about it is that it's under way, merely all
    right, or not done ("the kiln is firing", "the glaze is fine", "kiln's on"). When no clause names it (a reply
    like "it's running" to a question about it), the clauses that name no other item are about it. "" when no
    such thing is said. However loosely the words point to the item, they never make a done of these."""
    if not message:
        return ""
    from thirties_core.started import _SENTENCE_RE, _asks, _without_record_words
    others = [o for o in others if o and o != title]
    # a question, or the app's own status quoted ("why does it still say 'not marked'?"), says nothing about it
    told = " ".join(t for t in _SENTENCE_RE.findall(_without_record_words(message)) if not _asks(t))
    clauses = [c for c in _CLAUSE_RE.split(told) if c and c.strip()]
    about = [c for c in clauses if _about(c, title, others)]
    if not about:
        about = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)]
    for c in about:
        if not _done_in(c, title) and (STATE_RE.search(c) or _NOT_DONE_RE.search(c)):
            return (f"The user said {title} is going or all right, not that it's finished, so it isn't marked done. "
                    "Leave it open; it's done when they say it is.")
    return ""


def says_done(message: Optional[str], title: str, others=(), strict: bool = False) -> bool:
    """Whether the user's words say this item happened: a done word or its verb in the past, said about it (or in a
    clause naming no other item), or a plain yes; and nothing about it says it's only under way, merely all right,
    or not done. ``strict``: only done words and the item's own verb count (any past tense about the item counts
    otherwise), and a question says nothing ("how come the rent got moved?")."""
    if not message:
        return False
    from thirties_core.started import FINISHED_RE, _SENTENCE_RE, _asks, not_finished
    others = [o for o in others if o and o != title]
    if not_finished(message, title, others) in ("begun", "not") or state_not_done(message, title, others):
        return False
    if strict:
        message = " ".join(t for t in _SENTENCE_RE.findall(message) if not _asks(t))
    clauses = [c for c in _CLAUSE_RE.split(message) if c and c.strip()]
    if any(_about(c, title, others) and (_done_in(c, title) or (not strict and FINISHED_RE.search(c)))
           for c in clauses):
        return True
    free = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)]
    return any(_done_in(c, title) for c in free) or bool(YES_RE.match(message))


def _is_today(db: Any, note_id: str) -> bool:
    try:
        from thirties_core.lists import ListCatalog
        return ListCatalog(db).role(note_id) == "today"
    except Exception:
        return False


def refused(op: Dict[str, Any], turn: str, db: Any) -> str:
    """Why this change can't be written on the user's words this turn; "" when it can (or isn't judged here)."""
    if not turn or op.get("valid_until") or op.get("_twin"):
        return ""  # outside a turn, this morning's housekeeping, or the twin of a tick that went through
    message = _said(turn)
    kind = op.get("op")
    title = str(op.get("title") or "")
    if kind == "file_dropped" and not op.get("ticked") and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title} for good, so it isn't filed as dropped. Taking "
                "it off today leaves it on its own list.")
    if kind == "remove" and op.get("note_id") and not _is_today(db, str(op["note_id"])) \
            and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title}, so its line stays on its list. Taking it off "
                "today changes only today's plan and the Today note.")
    if kind == "complete" and not says_done(message, title):
        return (f"not written: the user's words don't say {title} is finished, so it isn't ticked. Something "
                "running, started or fine isn't done; it's done when they say it is.")
    return ""


__all__ = ["DROP_WORDS_RE", "DONE_WORDS_RE", "STATE_RE", "asks_to_drop", "says_done", "state_not_done", "refused"]
