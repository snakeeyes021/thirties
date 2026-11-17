"""The role of each time a reply says beside an item: a start, or an end.

A line such as "11:15 AM Pottery class (until 12:40 PM)" gives the item's start and its end. The reply check
(FrontierBrain._wrong_times) judges each by its role: the start must be the item's start (or its block), the end
its end. Checking the times only as a set let a right end excuse a wrong start ("11:45 AM Pottery class (until
12:40 PM)" over a class at 11:15).

Ranges ("11:15 – 12:40", "from 2:05 to 3:10") are left to the caller, which checks them as a pair already.
"""

from __future__ import annotations

import re
from typing import Callable, List, Pattern, Tuple

# words that make the time right after them an end: "(until 12:40 PM)", "ends at 3:10", "wraps up around 3:10"
END_WORD_RE = re.compile(r"(?:\buntil|\btill|'til|\bend(?:s|ing)?|\bfinish(?:es|ing)?|\bwraps?\s+up)"
                         r"(?:\s+(?:at|by|around|about))?\s*$", re.IGNORECASE)


def start_and_end(text: str, clock_re: Pattern, range_re: Pattern,
                  said_clocks: Callable[[str], List[tuple]]) -> Tuple[List[tuple], List[tuple]]:
    """The times ``text`` says outside any range, split by role: (starts, ends). A time right after "until",
    "ends" and the like is an end; any other is a start. Only a part with both is worth judging by role; a time
    said alone may be either."""
    taken = [m.span() for m in range_re.finditer(text or "")]
    starts: List[tuple] = []
    ends: List[tuple] = []
    for m in clock_re.finditer(text or ""):
        if any(a <= m.start() < b for a, b in taken):
            continue
        key = said_clocks(m.group(0))
        if not key:
            continue
        lead = text[:m.start()]
        (ends if END_WORD_RE.search(lead[-30:]) else starts).append(key[0])
    return starts, ends
