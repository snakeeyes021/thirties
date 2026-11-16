"""The parts of a sentence a claim covers, so a claim about two items is checked item by item.

"I moved the bench to 11 and the van to 6" used to pass when only the van moved, because the check looked at the
sentence as a whole and one of the two had changed; "the bench and the van are marked done" likewise. A claim covers
the part of the sentence that says it ("I moved the bench to 11") and the parts that are only names, times and small
words ("the van to 6", "the bench"); a part with words of its own ("the bench stays at 10") is another statement.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, List

_SPLIT_RE = re.compile(r"[,;]|\band\b|\bthen\b|\bplus\b", re.IGNORECASE)
# words a bare part may have besides the names: articles, pointers, times
_SMALL = {"the", "a", "an", "my", "your", "to", "at", "for", "from", "both", "also", "too", "now", "am", "pm", "noon",
          "i", "i've", "ive", "it", "on", "in", "of", "s", "all", "as", "well"}


def claimed_parts(sentence: str, claim_re: "re.Pattern", named: Callable[[str], Iterable[str]]) -> List[str]:
    """The parts of a sentence the claim in it covers. ``named(part)`` gives the titles a part names."""
    from thirties_core.item_facts import name_words
    out = []
    for part in _SPLIT_RE.split(sentence or ""):
        if not part.strip():
            continue
        if claim_re.search(part):
            out.append(part)
            continue
        titles = list(named(part))
        if not titles:
            continue
        rest = name_words(part) - set().union(*(name_words(t) for t in titles))
        if all(w in _SMALL or w.isdigit() or re.fullmatch(r"\d{1,2}(?::\d{2})?(?:am|pm)?", w) for w in rest):
            out.append(part)
    return out


__all__ = ["claimed_parts"]
