"""Started is not finished, and what the user is doing now is what the plan says now.

- **Started, not done.** "The dishwasher's going", "I'm in the shower now", "heading out for errands": something has
  begun. It's done only when the user says it's finished. ``only_started`` reads the user's words about one item
  (the clause that names it), so log_outcome never marks done what was only started.
- **Doing now** (PlanTools.doing_now): the item goes in the current block, started now, and the flexible things
  planned in its time move to the first free stretch after it: the user isn't doing them now. Something that runs
  by itself (a dishwasher, a load of laundry) is only recorded as started; nothing moves for it.
- **A gap the user asks for** (rest, a break, a nap) keeps its time like a fixed item: nothing new is placed in it,
  a redraw goes around it, and what was planned there moves out (``is_rest``).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# a clause saying it has begun or is happening now
STARTED_RE = re.compile(
    r"\b(?:i'?m|i am|we'?re|we are)\s+(?:in|at|on|doing|making|having|taking|getting|starting|heading|off|out|"
    r"about|still)\b|\b(?:going|running|started|starting|begun|beginning|heading|on my way|omw|about to|"
    r"working on|in progress|underway|under way|kicked off|turned on|put on|now doing)\b|\b[a-z]{3,}ing now\b|"
    r"\bin the [a-z]+ now\b", re.IGNORECASE)
# a clause saying it's over
FINISHED_RE = re.compile(
    r"\b(?:done|finished|finish|complete|completed|did|ate|fed|over with|wrapped up|handled|taken care of|"
    r"took care of|got out|out of the|back from|back home|went|ran|took|had|made)\b|"
    r"\b(?!(?:started|scheduled|planned|needed|wanted|supposed|tired|bored|kicked|turned)\b)[a-z]{3,}ed\b",
    re.IGNORECASE)
# a clause saying it hasn't happened ("not cooking yet", "I haven't cooked", "it is NOT done")
NOT_YET_RE = re.compile(r"\b(?:not|never|no|yet to|haven'?t|hasn'?t|didn'?t|isn'?t|wasn'?t|aren'?t)\b|n't\b",
                        re.IGNORECASE)
_OVER_RE = re.compile(r"\b(?:done|finished|complete|completed|over with|wrapped up)\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[.;!?\n,]+|\b(?:and|then|but|so|while)\b", re.IGNORECASE)

REST_RE = re.compile(
    r"^(?:(?:take|have|get)\s+)?(?:an?\s+|some\s+)?(?:quick\s+|short\s+|long\s+)?(?:rest|resting|break|nap|breather|"
    r"downtime|down time|lie[- ]down|lying down|quiet time|me time|free time|time off|recovery|decompress\w*|"
    r"unwind\w*|relax\w*)(?:\s+(?:time|break|period))?(?:\s+(?:after|before|from|until)\b.*)?$", re.IGNORECASE)


def is_rest(title: str) -> bool:
    """A gap the user asked for: rest, a break, a nap, free time. It keeps its time."""
    return bool(REST_RE.match(str(title or "").strip()))


def _names(clause: str, title: str, others: list) -> bool:
    """The clause names the item: as item_facts.mentions does, or by a word of the item's own (one no other item has,
    four letters or more) that a word of the clause starts with ("the washer" for Wash the sheets, "showering" for
    Shower)."""
    from thirties_core.item_facts import mentions, name_words
    if mentions(clause, title, others):
        return True
    shared = set().union(*(name_words(o) for o in others if o != title)) if others else set()
    own = {w for w in name_words(title) - shared if len(w) >= 4}
    said = re.findall(r"[a-z]+", clause.lower())
    return any(word.startswith(w) for w in own for word in said)


# --- a "not" that isn't the user saying it didn't happen ---
# Words about the app's record, not about the thing: "still says not marked yet", "isn't ticked", "unmarked". The
# record is what the user is asking to fix, so the "not" never means the thing didn't happen. Chat words after it
# ("isnt marked lol", "still not marked tho") don't change that.
_RECORD_NOT_RE = re.compile(
    r"\b(?:not|isn'?t|aren'?t|wasn'?t|weren'?t)\s+(?:been\s+|yet\s+|even\s+)?(?:marked|ticked|checked off|"
    r"crossed off)(?:\s+(?:as\s+)?(?:yet|done|off|complete|completed|finished))*"
    r"(?=\s*(?:$|[^\w\s]|\b(?:but|and|even|though|although|when|yet|in|on|here|there|anymore|still|"
    r"tho|lol|lmao|haha|pls|plz|please|either|again|for some reason)\b))|"
    r"\b(?:says?|said|shows?|showing|shown|reads?|listed as|marked as)\s+(?:as\s+|it'?s\s+)?"
    r"[\"'“‘]?\s*(?:not|un)", re.IGNORECASE)
# a quoted stretch: "why does it say 'not marked yet'" quotes the app, it doesn't deny anything
_QUOTED_RE = re.compile(r"\"[^\"\n]{1,80}\"|“[^”\n]{1,80}”|‘[^’\n]{1,80}’|(?<![A-Za-z])'[^'\n]{1,80}'(?![A-Za-z])")
# a question: a wh-word opens it ("why does the walk still say not done"), or a yes/no opener with its question
# mark ("is the walk not marked?"). "didn't get to the walk" opens with no question word: a statement.
_WH_RE = re.compile(r"^\W*(?:why|how come|how|what|what's|whats|where|when|wait,? why)\b", re.IGNORECASE)
_YES_NO_RE = re.compile(r"^\W*(?:is|isn'?t|are|aren'?t|does|doesn'?t|do|don'?t|did|didn'?t|was|wasn'?t|can|could|"
                        r"should|shouldn'?t|will|would)\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")


def _without_record_words(message: str) -> str:
    """The message with the "not"s that quote or describe the app's record taken out."""
    text = _QUOTED_RE.sub(lambda m: NOT_YET_RE.sub(" ", m.group(0)), message)
    return _RECORD_NOT_RE.sub(lambda m: NOT_YET_RE.sub(" ", m.group(0)), text)


def _asks(sentence: str) -> bool:
    s = sentence.strip()
    return bool(_WH_RE.match(s) or (_YES_NO_RE.match(s) and s.endswith("?")))


def said_not_done(message: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """Whether the user says this item hasn't happened ("haven't walked the dog yet", "the walk didn't happen").
    A question isn't a denial ("why does the walk still say not marked yet? I did it at 8"), and neither is the
    app's own status text, quoted or described ("it shows as not done", "isn't ticked")."""
    if not message:
        return False
    others = list(others)
    for sentence in _SENTENCE_RE.findall(_without_record_words(message)):
        if _asks(sentence):
            continue
        if any(c.strip() and _names(c, title, others) and NOT_YET_RE.search(c) for c in _CLAUSE.split(sentence)):
            return True
    return False


def not_finished(message: Optional[str], title: str, others: Iterable[str] = ()) -> str:
    """How the user's words about this item put it, when not as finished: "begun" (under way, or happening
    now) or "not" (it hasn't happened: "not cooking yet", "I haven't cooked"). "" when they say it's finished,
    when nobody is talking, or when the words don't name it. Asking about the item, or quoting the app's status
    for it, isn't saying it hasn't happened (said_not_done)."""
    if not message:
        return ""
    others = list(others)
    about = [c for c in _CLAUSE.split(message) if c.strip() and _names(c, title, others)]
    if not about:
        return ""
    if said_not_done(message, title, others):
        return "not"
    if any(_OVER_RE.search(c) for c in about):  # said in so many words
        return ""
    if any(STARTED_RE.search(c) for c in about):  # under way beats other verbs about the item ("pulled the cover off and
        return "begun"                            # started the washer")
    return ""


def only_started(message: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """Whether the user's words about this item say it has begun (or is going on now) and none says it's over."""
    return not_finished(message, title, others) == "begun"


__all__ = ["STARTED_RE", "FINISHED_RE", "NOT_YET_RE", "REST_RE", "is_rest", "not_finished", "only_started", "said_not_done"]
