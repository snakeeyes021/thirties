"""A reply is never only an announcement that something is being looked up.

Asked why an item sat where it did, a reply once went out as nothing but a line saying it was being checked, with
the schedule under it, and the answer came only with the next message: the model had written that line next to its
lookup, and the rewrite stopped at the lookup instead of answering from its result. A chat message can't be
followed up by itself, so "checking…", "let me look…" or "one moment…" promises a message that never comes.

- A sentence that only announces a lookup ("Checking why…", "Let me look into that.", "One moment.") is a holding
  line. A sentence that says when it'll check later ("I'll look at it tomorrow") or a check-in ("Checking in:")
  isn't one.
- A reply that is nothing but holding lines (with at most a schedule's rows under it) goes back to the model once:
  make the lookup now and answer from it, in this reply. If it still only announces, the user hears plainly that
  it couldn't be looked up, never the announcement.
- A reply that answers and also has a holding line loses that line, whole.
"""

from __future__ import annotations

import re
from typing import List

_LEAD_RE = re.compile(r"^\W*(?:(?:ok(?:ay)?|sure|right|got it|alright)\b\W*)?", re.IGNORECASE)
_HOLD_RE = re.compile(
    r"^(?:"
    r"(?:let me|lemme|I'?ll|I will|I'?m going to|I am going to)\s+(?:just\s+|quickly\s+|go\s+)?(?:check|look|see|"
    r"find out|pull up|dig|take a look|have a look|verify|double-check)"
    r"|(?:I'?m|I am)\s+(?:just\s+)?(?:checking|looking (?:into|at|up|through)|pulling|digging|searching)"
    r"|(?:checking|looking (?:into|at|up|through)|pulling up|digging (?:into|up)|searching|verifying|finding out)"
    r"|(?:one|just a|give me a|hang on a|hold on a|wait a)\s+(?:moment|sec(?:ond)?|minute|tick)"
    r"|hold on|hang on|bear with me"
    r")\b", re.IGNORECASE)
_GERUND_RE = re.compile(r"^(?:checking|looking|pulling|digging|searching|verifying|finding)\b", re.IGNORECASE)
# a check-in or a promise to check on something: not a holding line
_NOT_HOLD_RE = re.compile(r"\bcheck(?:ing)? (?:in|on|back)\b|\blooking forward\b", re.IGNORECASE)
# a lookup put off to a later time ("I'll look at it tomorrow"): not a holding line either
_FUTURE = r"(?:I'?ll|I will|I'?m going to|I am going to)"
_DAY_PARTS = ("morning", "afternoon", "evening")
_LATER = ["later", "next", "tonight", "tomorrow", r"at \d", "when (?:you|it)", "after (?:you|it|that)",
          "this (?:" + "|".join(_DAY_PARTS) + ")"]
_LATER_WORDS = "(?:" + "|".join(_LATER) + ")"
_LATER_RE = re.compile(rf"^{_FUTURE}\b.*\b{_LATER_WORDS}", re.IGNORECASE)
# "Looking at the afternoon, the canoe fits at 4:40.": an opening, then the answer
_THEN_ANSWER_RE = re.compile(r",\s+(?:\S+\s+){2,}\S")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_MAX_WORDS = 16

CHECK = ("Your draft announces a lookup and stops there; no second message follows it on its own."
         " Make the lookup now if you need it, then answer from what it shows, in this reply. "
         "If it doesn't say, say you don't know.")
COULDNT = "I couldn't look that up just now."


def is_holding(sentence: str) -> bool:
    """A sentence that only announces a lookup."""
    s = (sentence or "").strip()
    if not s or s.endswith("?") or len(s.split()) > _MAX_WORDS:
        return False
    rest = _LEAD_RE.sub("", s, count=1)
    if not _HOLD_RE.search(rest) or _NOT_HOLD_RE.search(rest) or _LATER_RE.search(rest):
        return False
    return not (_GERUND_RE.search(rest) and _THEN_ANSWER_RE.search(rest))


def _prose(reply: str) -> List[str]:
    """The reply's sentences, leaving out a schedule's rows (lines that don't end like a sentence)."""
    out = []
    for line in (reply or "").split("\n"):
        line = line.strip()
        if not line or not (line.endswith((".", "!", "?", "…", ":")) or len(re.split(r"(?<=[.!?…])\s+", line)) > 1):
            continue
        out += [s for s in _SENTENCE_RE.split(line) if s.strip()]
    return out


def holds(reply: str) -> bool:
    """The reply has a holding line somewhere."""
    return any(is_holding(s) for s in _prose(reply))


def only_holds(reply: str) -> bool:
    """The reply says nothing but that it's looking something up."""
    said = _prose(reply)
    return bool(said) and all(is_holding(s) for s in said)


def without(reply: str) -> str:
    """The reply without its holding lines (whole sentences, with any list one introduces)."""
    from thirties_core.clean_prose import cut
    return cut(reply, is_holding)


__all__ = ["is_holding", "holds", "only_holds", "without", "CHECK", "COULDNT"]
