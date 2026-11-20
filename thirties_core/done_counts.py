"""A count is never a name.

"Check those two off", "finished both", "the three of them are finished": the number counts items the user named
before. An item with the same word in its title (Fire two test tiles) isn't named by it. So when the done checks
read which item the user's words name, number words and bare small numbers are left out; the pointer and the count
still come from the words as said ("those two" points back to two items), and so does a time ("the 11am one").

A count that points back ("all three finished", "those three are done", "three of them") covers the items the
message the user was just shown lists, when it lists exactly that many (``pointed_count``).
"""

from __future__ import annotations

import re
from typing import Optional

COUNT_RE = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both|couple|pair|dozen)\b|"
    r"(?<![\d:.])\b\d{1,2}\b(?!\s*(?::\d|am\b|pm\b|a\.m|p\.m|h\b|min|minutes?\b|hours?\b|o'?clock))",
    re.IGNORECASE)


def without_counts(text: Optional[str]) -> Optional[str]:
    """The words with the counts taken out, for reading which item they name."""
    if text is None:
        return None
    return COUNT_RE.sub(" ", text)


def counted_only(message: Optional[str], title: str) -> bool:
    """The words reach this item only through a count ("check those two off" for Fire two test tiles)."""
    if not message or not COUNT_RE.search(message):
        return False
    from thirties_core.write_guard import _CLAUSE_RE, _about
    clauses = [c for c in _CLAUSE_RE.split(message) if c and c.strip()]
    return (any(_about(c, title, []) for c in clauses)
            and not any(_about(without_counts(c), title, []) for c in clauses))


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12}
_N = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
# a count with a word pointing back at items just shown, or a count "of them"
_POINTED_RE = re.compile(
    rf"\b(?:all|those|these|the)\s+{_N}\b(?!\s*(?::\d|am\b|pm\b|min|minutes?\b|hours?\b|o'?clock))|"
    rf"\b{_N}\s+of\s+(?:them|those|these|em|'em)\b", re.IGNORECASE)


def pointed_count(message: Optional[str]) -> Optional[int]:
    """How many items a count in the words points back at ("all three finished" → 3); None when no count does,
    or two counts differ."""
    found = set()
    for m in _POINTED_RE.finditer(message or ""):
        said = (m.group(1) or m.group(2)).lower()
        found.add(int(said) if said.isdigit() else _NUMBERS[said])
    return found.pop() if len(found) == 1 else None


__all__ = ["COUNT_RE", "counted_only", "pointed_count", "without_counts"]
