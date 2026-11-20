"""A done said ahead of time is no report.

"I'll ping you as soon as the soup bowls are finished", "once the paddles are waxed I'm off to the lake", "the
bowls will be done in a bit": the words are about the item and carry a done word, but the done is promised, hoped
for, or waiting on a condition. It hasn't happened, so nothing is ticked, on the plan or in Joplin.

- **A condition or a moment to come**: "once", "when", "as soon as", "the moment", "after", "if", "until" before
  the item's words, said in the present ("are glazed", "I finish"). Said in the past ("when I finished the bowls",
  "after the paddles were waxed") it's a report, and counts.
- **The future, or a hope**: "will", "I'll", "going to", "should", "hopefully", "in a bit", "tomorrow", "need to"
  in the clause about the item, with its done word.

Read for the clause that names the item; when none does (a reply to a question about it), for the clauses that name
no other item. Questions say nothing.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# a condition, or a moment still to come, that a done waits on
_LEAD_RE = re.compile(r"\b(?:once|when|whenever|as soon as|soon as|the (?:moment|minute|second|instant)|after|if|"
                      r"until|till|by the time|before)\b", re.IGNORECASE)
# said in the present after such a word: still to come ("once they are glazed", "when I finish them")
_PRESENT_RE = re.compile(r"\b(?:is|are|am|be|get|gets|getting|finish|finishes|wrap|wraps|complete|completes|have|has)\b|"
                         r"\w'(?:s|re|ve)\b", re.IGNORECASE)
# said in the past: a report ("after the paddles were waxed", "when I finished them")
_PAST_RE = re.compile(r"\b(?:was|were|had|did|got|went|came)\b|"
                      r"\b(?:i|we|you|they)\s+(?:\w+ly\s+)?(?:[a-z]{3,}ed|wrapped)\b", re.IGNORECASE)
# the future, a hope, or something still to do
_FUTURE_RE = re.compile(r"\b(?:will|shall|going to|gonna|should|ought to|might|hope|hoping|hopefully|about to|"
                        r"soon(?!\s+after)|in a (?:bit|sec|second|minute|moment|while|few)|tomorrow|eventually|"
                        r"plan to|planning to|need to|needs to|have to|has to|gotta)\b|\w'll\b",
                        re.IGNORECASE)
_STRETCH_END_RE = re.compile(r"[,;:.!?\n]")


def _plain(text: str) -> str:
    return re.sub(r"[‘’`]", "'", text or "")


def said_ahead(message: Optional[str], title: str, others: Iterable[str] = ()) -> str:
    """Why the user's words put this item's done in the future or behind a condition ("I'll tell you once the bowls
    are glazed"); "" when they don't."""
    if not message:
        return ""
    from thirties_core.started import _SENTENCE_RE
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import REPORTED_RE, _CLAUSE_RE, _about, _done_in
    others = [o for o in others if o and o != title]
    sentences = [s for s in _SENTENCE_RE.findall(_plain(message)) if s.strip() and not s.rstrip().endswith("?")]

    def names_other(text: str) -> bool:
        return any(_about(text, o, [x for x in others if x != o] + [title]) for o in others)

    named_anywhere = any(_about(c, title, others) for s in sentences for c in _CLAUSE_RE.split(s) if c and c.strip())

    def about(text: str) -> bool:
        return _about(text, title, others) if named_anywhere else not names_other(text)

    def done_in(text: str) -> bool:  # a done word goes with it: "should be fine" promises nothing
        return _done_in(text, title) or bool(FINISHED_RE.search(text)) or bool(REPORTED_RE.search(text))

    for sentence in sentences:
        for lead in _LEAD_RE.finditer(sentence):
            stretch = _STRETCH_END_RE.split(sentence[lead.end():], maxsplit=1)[0]
            if (stretch.strip() and about(stretch) and done_in(stretch) and _PRESENT_RE.search(stretch)
                    and not _PAST_RE.search(stretch)):
                return _why(title)
        for clause in _CLAUSE_RE.split(sentence):
            if clause and clause.strip() and about(clause) and done_in(clause) and _FUTURE_RE.search(clause):
                return _why(title)
    return ""


def _why(title: str) -> str:
    return (f"The user said {title} will be done, or is done once something happens: that's still to come, so it "
            "isn't marked done. Leave it open; it's done when they say it is.")


__all__ = ["said_ahead"]
