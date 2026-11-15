"""Goals in the profile keep the day they were set, a real deadline, and when they were last checked.

Without the day a goal was added, its deadline can't be known either. A goal
"in 2–4 months" means a date, counted from the day it was set. 10-19: a goal finished the night before was still
suggested, because the profile's "Near-term goals" were weeks old.

So each goal line ends with plain dates the user can see and edit like the rest of the profile:

    - Learn three sea knots in 2–4 months (set 2026-10-16; by 2027-02-16; as of 2026-10-30)

- **set**: the day it was first written down. It never changes, whatever is checked or rewritten later.
- **by**: the deadline, worked out by Python from the goal's own words, counted from the day it was set ("in 2–4
  months" is the far end, 4 months; "by March 1"; "this year"). None when the words give no time.
- **as of**: the day it was last confirmed current (left out while that's the day it was set).

Python works out which are stale and how long each has left; the model only asks the user about stale ones,
once, and records the answer with check_goal (done and dropped ones come out, so they're never suggested
again). Lines from before this carry only "(as of …)": that day is the best guess for when they were set.
Undated lines count as stale: nobody knows how old they are.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

# How long a goal stays current without a check (decided 2026-10-30; change here). Near-term goals are weeks-long
# things ("fix the squeaky door", "tidy the garage"); long-term ones change slowly.
STALE_AFTER_DAYS = {"Near-term goals": 14, "Long-term goals": 90}
GOAL_SECTIONS = tuple(STALE_AFTER_DAYS)
CHECK_STATES = ("current", "done", "dropped")

_ISO = r"\d{4}-\d{2}-\d{2}"
_TAIL_RE = re.compile(r"\s*\(((?:(?:set|by|as of) " + _ISO + r"\s*[;,]?\s*)+)\)\s*$")
_FIELD_RE = re.compile(r"(set|by|as of) (" + _ISO + r")")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")

_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "a couple": 2,
            "a few": 3, "few": 3, "several": 4}
_NUM = r"(\d+|" + "|".join(sorted((re.escape(k) for k in _NUMBERS), key=len, reverse=True)) + r")"
_SPAN_RE = re.compile(r"\b(?:in|within|over|over the next|in the next|in about|in around)\s+(?:about\s+|around\s+|"
                      r"the next\s+)?" + _NUM + r"(?:\s*(?:-|–|to|or)\s*" + _NUM + r")?\s+(day|week|month|year)s?\b",
                      re.IGNORECASE)
_THIS_RE = re.compile(r"\b(?:by the end of |before the end of |by end of |)(this|next) (week|month|year)\b",
                      re.IGNORECASE)
_BY_DATE_RE = re.compile(r"\b(?:by|before)\s+((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
                         r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{4})?)\b", re.IGNORECASE)


@dataclass
class Goal:
    text: str
    set: Optional[date] = None
    by: Optional[date] = None
    as_of: Optional[date] = None

    @property
    def checked(self) -> Optional[date]:
        """The last day it was known current."""
        return self.as_of or self.set

    def line(self) -> str:
        parts = []
        if self.set:
            parts.append(f"set {self.set.isoformat()}")
        if self.by:
            parts.append(f"by {self.by.isoformat()}")
        if self.as_of and self.as_of != self.set:
            parts.append(f"as of {self.as_of.isoformat()}")
        return f"- {self.text}" + (f" ({'; '.join(parts)})" if parts else "")


def _num(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBERS.get(word.lower(), 1)


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def deadline_from(text: str, set_on: date) -> Optional[date]:
    """The date a goal's own words set, counted from the day it was set; None when they give no time."""
    m = _SPAN_RE.search(text or "")
    if m:
        n = max(_num(m.group(1)), _num(m.group(2)) if m.group(2) else 0)  # "3–5 months": the far end
        unit = m.group(3).lower()
        if unit == "day":
            return set_on + timedelta(days=n)
        if unit == "week":
            return set_on + timedelta(weeks=n)
        return add_months(set_on, n * (12 if unit == "year" else 1))
    m = _THIS_RE.search(text or "")
    if m:
        which, unit = m.group(1).lower(), m.group(2).lower()
        if unit == "week":
            end = set_on + timedelta(days=6 - set_on.weekday())
            return end + timedelta(weeks=1) if which == "next" else end
        if unit == "month":
            first = add_months(set_on.replace(day=1), 2 if which == "next" else 1)
            return first - timedelta(days=1)
        return date(set_on.year + (1 if which == "next" else 0), 12, 31)
    m = _BY_DATE_RE.search(text or "")
    if m:
        from thirties_core.clock import ClockError, parse_day
        said = m.group(1).strip().rstrip(".")
        if not re.search(r"\d", said):  # "by March": the start of that month
            month = next((i for i, name in enumerate(calendar.month_abbr) if name
                          and said.lower().startswith(name.lower())), None)
            if month is None:
                return None
            day = date(set_on.year, month, 1)
            return day if day >= set_on else day.replace(year=day.year + 1)
        try:
            day = parse_day(said, set_on)
        except (ClockError, ValueError):
            return None
        if day < set_on and not re.search(r"\d{4}", m.group(1)):  # "by March" said in November: next March
            day = day.replace(year=day.year + 1)
        return day
    return None


def parse(line: str) -> Goal:
    """A goal line: its text (no bullet, no dates) and its dates."""
    m = _TAIL_RE.search(line or "")
    fields: Dict[str, date] = {}
    if m:
        for key, value in _FIELD_RE.findall(m.group(1)):
            try:
                fields[key] = date.fromisoformat(value)
            except ValueError:
                pass
    text = _BULLET_RE.sub("", _TAIL_RE.sub("", line or "")).strip()
    as_of = fields.get("as of")
    set_on = fields.get("set") or as_of  # from before "set": the old as-of is the best guess
    return Goal(text, set_on, fields.get("by"), as_of if as_of != set_on else None)


def split_line(line: str) -> Tuple[str, Optional[date]]:
    """A goal line's text and the day it was last known current, if any."""
    g = parse(line)
    return g.text, g.checked


def dated(text: str, day: date) -> str:
    """A new goal, set ``day``, with the deadline its words give."""
    g = parse(text)
    if g.set is None:
        g.set = day
    if g.by is None:
        g.by = deadline_from(g.text, g.set)
    return g.line()


def _goal_lines(content: str) -> List[str]:
    return [line for line in (content or "").splitlines() if line.strip() and parse(line).text]


def stamp_new_lines(old: str, new: str, today: date) -> str:
    """The section's new text, with each line that wasn't there before set today. Lines already there keep their
    dates (or stay undated): rewriting a section mustn't make old goals look freshly set or checked."""
    known = {parse(line).text.lower(): line for line in _goal_lines(old)}
    out = []
    for line in (new or "").splitlines():
        g = parse(line)
        if not g.text:
            out.append(line)
        elif g.set is not None:
            out.append(line)
        elif g.text.lower() in known:
            out.append(known[g.text.lower()])
        else:
            out.append(dated(line, today))
    return "\n".join(out).strip()


def stale_goals(sections: Dict[str, str], today: date) -> List[Tuple[str, str, Optional[date]]]:
    """(section, goal text, last checked or None) for each goal older than its section allows, oldest first."""
    out = []
    for section, days in STALE_AFTER_DAYS.items():
        for line in _goal_lines(sections.get(section, "")):
            g = parse(line)
            if g.checked is None or (today - g.checked).days > days:
                out.append((section, g.text, g.checked))
    return sorted(out, key=lambda g: g[2] or date.min)


def prompt_lines(sections: Dict[str, str], today: date) -> List[str]:
    """For the brain: the goals to check with the user before offering anything for them."""
    return [f"- {text} ({section.split()[0].lower()}; " + (f"last checked {when.strftime('%b %d')}" if when else
                                                            "never checked") + ")"
            for section, text, when in stale_goals(sections, today)]


def _left(by: date, today: date) -> str:
    days = (by - today).days
    if days < 0:
        return f"{-days} day{'s' if days != -1 else ''} past it"
    if days == 0:
        return "today"
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} left"
    if days < 70:
        return f"about {round(days / 7)} weeks left"
    return f"about {round(days / 30.4)} months left"


def deadline_lines(sections: Dict[str, str], today: date) -> List[str]:
    """For the brain: each goal with a deadline, when it was set and how long is left (Python's math)."""
    out = []
    for section in GOAL_SECTIONS:
        for line in _goal_lines(sections.get(section, "")):
            g = parse(line)
            if g.by:
                said = f"set {g.set.strftime('%b %d, %Y')}; " if g.set else ""
                out.append(f"- {g.text}: by {g.by.strftime('%b %d, %Y')} ({said}{_left(g.by, today)})")
    return out


def prompt_section(sections: Dict[str, str], today: date) -> str:
    lines = deadline_lines(sections, today)
    if not lines:
        return ""
    return ("GOAL DEADLINES (worked out from the day each goal was set; favour what's nearest, and say when one is "
            "slipping)\n" + "\n".join(lines))


def check_goal(sections: Dict[str, str], goal: str, state: str, today: date) -> Tuple[str, str, str]:
    """Apply the user's answer about a goal. Returns (section, its new content, a sentence). "current" marks it
    checked today (its set date and deadline stay); "done" and "dropped" take it out. Raises ValueError if the
    goal isn't found or is ambiguous."""
    state = (state or "").strip().lower()
    if state not in CHECK_STATES:
        raise ValueError(f"state must be one of {', '.join(CHECK_STATES)}.")
    want = parse(goal).text.lower()
    if not want:
        raise ValueError("Say which goal.")
    every = [(section, line, parse(line).text) for section in GOAL_SECTIONS
             for line in _goal_lines(sections.get(section, ""))]
    hits = [h for h in every if h[2].lower() == want] or [h for h in every if want in h[2].lower()]
    if not hits:
        raise ValueError(f"No goal like '{goal}' in the profile.")
    if len(hits) > 1:
        raise ValueError(f"'{goal}' matches several goals: " + "; ".join(h[2] for h in hits) + ". Quote one.")
    section, line, text = hits[0]
    lines = (sections.get(section, "") or "").splitlines()
    i = lines.index(line)
    if state == "current":
        g = parse(line)
        if g.set is None:
            g.set = today  # never dated: today is the first day anyone knows of it
        g.as_of = today
        lines[i] = g.line()
        said = f"'{text}' is still a goal (checked today)."
    else:
        del lines[i]
        said = f"Took '{text}' out of {section}" + (" (done)." if state == "done" else ".")
    return section, "\n".join(lines).strip(), said


__all__ = ["STALE_AFTER_DAYS", "GOAL_SECTIONS", "Goal", "parse", "split_line", "dated", "deadline_from",
           "stamp_new_lines", "stale_goals", "prompt_lines", "deadline_lines", "prompt_section", "check_goal",
           "add_months"]
