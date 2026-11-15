"""The plain line added under a reply that didn't match the plan, in the app's own words and times.

When a reply gives an item a time the plan doesn't have, and the rewrite still does, Python adds one line with the
plan's facts. That line reads like the rest of the app:

- **Times as the app says them**: 12-hour ("10:00 PM"), never a 24-hour "22:00" next to a "10:00", which leaves the
  user asking which one is meant.
- **Items by their name**: a title that opens with a time of its own ("21:30 Lock the back door", as typed into a
  routine) is named without it, so the line shows one time per item: the plan's.
- **One plain sentence**: "On the plan, Lock the back door is at 9:30 PM and Water the ferns is at 6:00 PM."
"""

from __future__ import annotations

import re
from typing import Iterable, List

# a time a title opens with ("21:30 Lock the back door", "6:30am - Walk")
_LEADING_CLOCK_RE = re.compile(r"^\s*(?:[01]?\d|2[0-3])[:.][0-5]\d\s*(?:[ap]\.?m\.?)?\s*[-–—:,]?\s+(?=\S)",
                               re.IGNORECASE)
# a clock with no AM or PM after it
_CLOCK_RE = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])(?!\s*(?:[ap]\.?m\b))", re.IGNORECASE)


def plain_name(title: str) -> str:
    """The item's name without a time it opens with."""
    name = _LEADING_CLOCK_RE.sub("", str(title or ""), count=1)
    return name if name.strip() else str(title or "")


def twelve_hour(text: str) -> str:
    """24-hour clocks ("22:00", "06:30") said as the app says them ("10:00 PM", "6:30 AM"); a 12-hour clock with
    no AM or PM ("10:00") is left as it is."""
    def say(m: re.Match) -> str:
        hour, minute = int(m.group(1)), m.group(2)
        if not (hour >= 13 or hour == 0 or m.group(1).startswith("0")):
            return m.group(0)
        return f"{hour % 12 or 12}:{minute} {'AM' if hour < 12 else 'PM'}"
    return _CLOCK_RE.sub(say, text)


def on_the_plan(facts: List[str]) -> str:
    """The plan's times for the items a reply got wrong, as one sentence."""
    facts = [f for f in facts if f]
    said = facts[0] if len(facts) == 1 else ", ".join(facts[:-1]) + " and " + facts[-1]
    return f"(On the plan, {said}.)"


def in_app_words(lines: List[str], titles: Iterable[str]) -> List[str]:
    """The added lines with every item named as the app names it and every time in 12-hour form."""
    renamed = sorted({t for t in titles if t and plain_name(t) != t}, key=len, reverse=True)
    out = []
    for line in lines:
        for title in renamed:
            line = line.replace(title, plain_name(title))
        out.append(twelve_hour(line))
    return out


__all__ = ["plain_name", "twelve_hour", "on_the_plan", "in_app_words"]
