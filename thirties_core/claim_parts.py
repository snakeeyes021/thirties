"""The parts of a sentence a claim covers, so a claim about two items is checked item by item.

"I moved the bench to 11 and the van to 6" used to pass when only the van moved, because the check looked at the
sentence as a whole and one of the two had changed; "the bench and the van are marked done" likewise. A claim covers
the part of the sentence that says it ("I moved the bench to 11") and the parts that are only names, times and small
words ("the van to 6", "the bench"); a part with words of its own ("the bench stays at 10") is another statement.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, List

_SPLIT_RE = re.compile(r"[,;]|:(?!\d)|\band\b|\bthen\b|\bplus\b", re.IGNORECASE)
# words a bare part may have besides the names: articles, pointers, times
_SMALL = {"the", "a", "an", "my", "your", "to", "at", "for", "from", "both", "also", "too", "now", "am", "pm", "noon",
          "i", "i've", "ive", "it", "on", "in", "of", "s", "all", "as", "well"}


def claimed_parts(sentence: str, claim_re: "re.Pattern", named: Callable[[str], Iterable[str]]) -> List[str]:
    """The parts of a sentence the claim in it covers. ``named(part)`` gives the titles a part names."""
    from thirties_core.item_facts import name_words
    # each part: "claim" (says the claim), "other" (a statement of its own: "I've skipped the bench") or "bare"
    # (only names, times and small words). A bare part belongs to the statement before it ("I've skipped the bench
    # and the van, moved the kiln" skips the van, it doesn't move it); bare parts that open the sentence, to the
    # first statement after them ("the bench and the van are marked done").
    parts = []
    for part in _SPLIT_RE.split(sentence or ""):
        if not part.strip():
            continue
        if claim_re.search(part):
            parts.append((part, "claim"))
            continue
        titles = list(named(part))
        if not titles:
            continue  # names no item: no statement about one
        rest = name_words(part) - set().union(*(name_words(t) for t in titles))
        bare = all(w in _SMALL or w.isdigit() or re.fullmatch(r"\d{1,2}(?::\d{2})?(?:am|pm)?", w)
                              for w in rest)
        parts.append((part, "bare" if bare else "other"))
    out, leading, current = [], [], None
    for part, kind in parts:
        if kind == "bare":
            if current is None:
                leading.append(part)
            elif current == "claim":
                out.append(part)
            continue
        if current is None and kind == "claim":
            out += leading
        current = kind
        if kind == "claim":
            out.append(part)
    return out


__all__ = ["claimed_parts"]
