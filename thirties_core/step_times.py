"""A clock time typed at the start of a step's title is the step's time, not part of its name.

People often write a routine as "6:15 Water the plants", "7pm Lay out clothes" or "21:00 Lights out". Kept in the
title, that time shows up twice next to the plan's own time ("7:00 AM 6:15 Water the plants"), the user keeps
asking to fix it, and the reply checks read the title's time as a claim about the plan. So wherever Thirties keeps
a step's title (a routine placed on the plan, its learned placement, a daily anchor, a step written to today's
list), the leading time is taken off the title and kept as the step's own time.

The routine note itself is the user's: it is never rewritten. Only Thirties' own copies of the titles change.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Optional, Tuple

# "6:15 ", "06:15 - ", "6:15am ", "7 pm: ", "21:00 "; a bare number ("15-min stretch", "3 sets", "10.5 km") is no
# time: it needs its minutes after a colon, or am/pm
_LEADING_RE = re.compile(
    r"^\s*(?:@\s*)?"
    r"(?:(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*(?P<ap>[ap])\.?\s*m\b\.?)?"
    r"|(?P<h2>1[0-2]|0?[1-9])\s*(?P<ap2>[ap])\.?\s*m\b\.?)"
    r"\s*(?:[-–—:,|>]+\s*|\s+)(?=\S)",
    re.IGNORECASE)


def split_leading_time(title: str) -> Tuple[str, Optional[time], bool]:
    """(the title without its leading time, the time, whether the time is certain). A 24-hour time ("17:30"),
    one with a leading zero ("06:30") or one with am/pm is certain; "7:30" alone could be morning or evening."""
    text = str(title or "")
    m = _LEADING_RE.match(text)
    if not m:
        return text.strip(), None, True
    rest = text[m.end():].strip()
    if not rest or not re.search(r"[A-Za-z]", rest):
        return text.strip(), None, True  # the whole title is a time: nothing to name the step by
    if m.group("h2"):
        hour, minute, ap = int(m.group("h2")), 0, m.group("ap2")
    else:
        hour, minute, ap = int(m.group("h")), int(m.group("m")), m.group("ap")
    if ap:
        if hour > 12 or hour == 0:
            return text.strip(), None, True  # "17:30pm" isn't a time anyone means
        hour = hour % 12 + (12 if ap.lower() == "p" else 0)
        return rest, time(hour, minute), True
    certain = hour >= 13 or hour == 0 or (m.group("h") or "").startswith("0")
    return rest, time(hour, minute), certain


def clean_title(title: str) -> str:
    """The title without a leading clock time ("6:15 Water the plants" -> "Water the plants")."""
    return split_leading_time(title)[0]


def has_leading_time(title: str) -> bool:
    return split_leading_time(title)[1] is not None


def key(title: str) -> str:
    """The title as Thirties compares step names: without its leading time, lowercased."""
    return clean_title(title).strip().lower()


def step_start(title: str, near: datetime) -> Optional[datetime]:
    """When the title's leading time falls, on the day of ``near``: an uncertain "7:30" is whichever of 7:30 AM and
    7:30 PM is nearer ``near`` (the part it was placed in). None when the title starts with no time."""
    _, t, certain = split_leading_time(title)
    if t is None:
        return None
    base = near.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if certain:
        return base
    options = [base, base + timedelta(hours=12) if t.hour < 12 else base - timedelta(hours=12)]
    return min(options, key=lambda o: abs((o - near).total_seconds()))
