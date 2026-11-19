"""A count is never a name.

"Check those two off", "finished both", "the three of them are finished": the number counts items the user named
before. An item with the same word in its title (Fire two test tiles) isn't named by it. So when the done checks
read which item the user's words name, number words and bare small numbers are left out; the pointer and the count
still come from the words as said ("those two" points back to two items), and so does a time ("the 11am one").
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


__all__ = ["COUNT_RE", "counted_only", "without_counts"]
