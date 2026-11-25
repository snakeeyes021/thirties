"""Words beside an item's name that say nothing about the item.

- **"now" about what the app shows.** "lunch at 10:40, the time it shows now", "as the plan says now": the "now" is
  about the schedule on the screen, not a word that the item is under way or comes next. It's left out when the
  done checks read the words (``without_display_now``).
- **When something else ends.** "sanding paddles until the kiln check": the clause is about the sanding; the kiln
  check is only when it stops. What a clause says (going, working, on) belongs to what comes before "until",
  "till" or "before", so it isn't held against an item named only after it, nor is the clause a done for that item
  (``head``, ``only_as_an_end``).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# a "now" right after a word for what the app or the plan shows
_DISPLAY_NOW_RE = re.compile(
    r"\b((?:says?|said|shows?|showing|shown|reads?|lists?|listed|displays?|displayed|"
    r"(?:has|have|had|got|puts?)\s+(?:it|them|that))(?:\s+(?:it|there|on (?:the )?(?:plan|schedule|screen|list)))?)"
    r"\s+(?:right\s+)?now\b", re.IGNORECASE)
# the start of a clause's tail that says when the clause's activity stops
_END_RE = re.compile(r"\b(?:until|till|til|up until|up to|right up to|before)\b", re.IGNORECASE)


def without_display_now(text: Optional[str]) -> Optional[str]:
    """The words with each "now" that is about what the app shows taken out ("like it says now" → "like it says")."""
    if not text:
        return text
    return _DISPLAY_NOW_RE.sub(lambda m: m.group(1), text)


def head(clause: str) -> str:
    """The clause up to where it says when its activity stops ("working " for "working until the kiln check")."""
    m = _END_RE.search(clause or "")
    return clause[:m.start()] if m else clause


def only_as_an_end(clauses: Iterable[str], title: str, others: Iterable[str]) -> bool:
    """Every clause that names the item names it only as when something else stops ("until the kiln check"),
    with no done for it there."""
    from thirties_core.write_guard import _about, _done_in
    others = [o for o in others if o and o != title]
    naming = [c for c in clauses if c and c.strip() and _about(c, title, others)]
    if not naming:
        return False
    for c in naming:
        m = _END_RE.search(c)
        if m is None or _about(c[:m.start()], title, others) or _done_in(c[m.end():], title):
            return False
    return True


def end_refusal(title: str) -> str:
    return (f"The user named {title} only as when something else stops, not as done, so it isn't marked done. "
            "Leave it open.")


__all__ = ["end_refusal", "head", "only_as_an_end", "without_display_now"]
