"""Seasons: an item that only makes sense in some months ("only in summer", "a winter thing", "May to September").

The user says so (set_item_facts with ``season``) or the item's own text marks it ("(summer)", "(May–Sep)"); it's
never guessed. The item keeps its months. Named seasons are meteorological and follow the hemisphere of the
configured latitude: in the north, summer is June to August and spring is March to May; in the south, six months
on.

The planners only place it within those months. Out of season, it waits for its next window: it isn't someday, it
isn't missed, and it's never offered day after day. A deadline outside the window is pulled back to the last
in-season day before it. Daylight composes on its own: an outdoor item in summer gets that day's (summer) daylight.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Iterable, List, Optional, Set

_NORTH = {"spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11), "fall": (9, 10, 11), "winter": (12, 1, 2)}
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9
_SEASONS_ALT = "spring|summer|autumn|fall|winter"
_MONTHS_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASON_WORD = rf"({_SEASONS_ALT})"
_MONTH_WORD = rf"({_MONTHS_ALT})\.?"
# an item's own mark: "(summer)", "(only in winter)", "(May–Sep)", "(June)"
MARK_RE = re.compile(rf"\((?:only\s+(?:in\s+)?)?({_SEASONS_ALT}|{_MONTHS_ALT})\.?"
                     rf"(?:\s*(?:-|–|to|through)\s*({_MONTHS_ALT})\.?)?\)", re.IGNORECASE)


def season_months(name: str, latitude: float = 0.0) -> Set[int]:
    """A named season's months, for the hemisphere of ``latitude`` (meteorological seasons)."""
    months = _NORTH[name.lower()]
    if latitude < 0:
        months = tuple((m + 5) % 12 + 1 for m in months)
    return set(months)


def month_span(first: int, last: int) -> Set[int]:
    """Months from ``first`` to ``last``, round the year end if needed (November to February)."""
    out, m = set(), first
    while True:
        out.add(m)
        if m == last:
            return out
        m = m % 12 + 1


def parse(text: str, latitude: float = 0.0) -> Optional[Set[int]]:
    """The months a user's words name: a season ("summer", "a winter thing"), a range ("May to September"), or
    months ("June and July"). An empty set means all year ("never", "all year"); None if it names none."""
    raw = " ".join(str(text or "").lower().split())
    if raw in ("never", "all year", "year round", "year-round", "any time", "anytime", "none", "no", "false", ""):
        return set()
    m = re.search(_MONTH_WORD + r"\s*(?:-|–|to|through|until)\s*" + _MONTH_WORD, raw)
    if m:
        return month_span(_MONTHS[m.group(1)], _MONTHS[m.group(2)])
    seasons = re.findall(r"\b" + _SEASON_WORD + r"\b", raw)
    if seasons:
        return set().union(*(season_months(s, latitude) for s in seasons))
    months = {_MONTHS[w] for w in re.findall(r"\b" + _MONTH_WORD, raw)}
    return months or None


def marked(title: str, latitude: float = 0.0) -> Set[int]:
    """Months an item's own text marks ("(summer)", "(May–Sep)"); empty if none."""
    m = MARK_RE.search(title or "")
    if not m:
        return set()
    first = m.group(1).lower().rstrip(".")
    if first in _NORTH:
        return season_months(first, latitude)
    if m.group(2):
        return month_span(_MONTHS[first], _MONTHS[m.group(2).lower().rstrip(".")])
    return {_MONTHS[first]}


def to_text(months: Iterable[int]) -> str:
    """For storing: "6,7,8"."""
    return ",".join(str(m) for m in sorted(set(months)))


def from_text(text: str) -> Set[int]:
    return {int(x) for x in str(text or "").split(",") if x.strip().isdigit() and 1 <= int(x) <= 12}


def in_season(months: Set[int], d: date) -> bool:
    return not months or d.month in months


def overlaps(months: Set[int], start: date, end: date) -> bool:
    """Any day of start..end falls in those months (always, with none)."""
    if not months:
        return True
    d = start.replace(day=1)
    while d <= end:
        if d.month in months:
            return True
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return False


def pulled_due(months: Set[int], due: Optional[date]) -> Optional[date]:
    """A deadline outside the season, pulled back to the last in-season day before it."""
    if due is None or in_season(months, due):
        return due
    d = due
    for _ in range(400):
        d -= timedelta(days=1)
        if d.month in months:
            return d
    return due


def describe(months: Set[int]) -> str:
    """"June to August", or the months named one by one when they aren't one stretch."""
    if not months:
        return "all year"
    ms = sorted(months)
    for first in ms:  # a single stretch, possibly round the year end
        if month_span(first, (first + len(ms) - 2) % 12 + 1) == set(ms):
            last = (first + len(ms) - 2) % 12 + 1
            return calendar.month_name[first] if first == last else \
                f"{calendar.month_name[first]} to {calendar.month_name[last]}"
    return ", ".join(calendar.month_name[m] for m in ms)


__all__ = ["season_months", "month_span", "parse", "marked", "to_text", "from_text", "in_season", "overlaps",
           "pulled_due", "describe"]
