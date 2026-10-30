"""Weekly patterns: work hours and sleep that differ by weekday.

The defaults in ``config.general`` (work_start_thirty ... sleep_end_thirty, work_days) are the usual week; the
setting ``general.week`` holds what's different on some weekdays, e.g. ``{"sat": {"sleep_end_thirty": 18}}`` (sleep
in until 9 on Saturdays). Which days are work days stays in ``general.work_days`` (shown in Preferences), so the two
never disagree. The day builder (``DeterministicScheduler.routine_spans``) reads ``day_hours`` for each date, so the
default Work and Sleep spans follow the pattern ("I work 7–3 on weekdays, off weekends", After 0.1 item 7).

Bedtime belongs to the evening's weekday and waking to the morning's: Friday night's sleep starts at Friday's
bedtime and ends at Saturday's wake time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Dict, Iterable, List, Optional

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HOUR_KEYS = ("work_start_thirty", "work_end_thirty", "sleep_start_thirty", "sleep_end_thirty")


@dataclass(frozen=True)
class DayHours:
    """One weekday's hours as config thirties (0 = midnight, 17 = 8:30 AM). work_end is the last work thirty."""
    works: bool
    work_start: int
    work_end: int
    sleep_start: int
    sleep_end: int


def weekday_key(d: date) -> str:
    return DAYS[d.weekday()]


def work_days(gen) -> List[str]:
    return [str(d).strip().lower()[:3] for d in (getattr(gen, "work_days", None) or [])]


def day_hours(gen, d: date) -> DayHours:
    """The hours for a date: the usual ones, with that weekday's differences applied."""
    key = weekday_key(d)
    over: Dict[str, Any] = (getattr(gen, "week", None) or {}).get(key) or {}

    def pick(name: str) -> int:
        value = over.get(name)
        return int(value) if value is not None else int(getattr(gen, name))
    return DayHours(key in work_days(gen), pick("work_start_thirty"), pick("work_end_thirty"),
                    pick("sleep_start_thirty"), pick("sleep_end_thirty"))


# --- reading what the user says ---

_DAY_WORDS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
              "saturday": "sat", "sunday": "sun"}


def parse_days(text: Any, gen=None) -> List[str]:
    """'weekdays', 'weekends', 'mon-fri', 'Monday and Thursday', 'every day', 'work days', 'days off' -> day keys."""
    if isinstance(text, (list, tuple)):
        return sorted({d for part in text for d in parse_days(part, gen)}, key=DAYS.index)
    raw = str(text or "").strip().lower()
    if raw in ("", "every day", "everyday", "daily", "all", "all days", "each day", "all week", "the week"):
        return list(DAYS)
    if re.fullmatch(r"(on )?(the )?week ?days?|monday (to|through|-) friday|mon ?- ?fri", raw):
        return list(DAYS[:5])
    if re.fullmatch(r"(on )?(the )?week ?ends?|sat(urday)?s? and sun(day)?s?", raw):
        return ["sat", "sun"]
    if re.fullmatch(r"(my )?work ?days?|working days|days i work", raw):
        return work_days(gen) if gen is not None else list(DAYS[:5])
    if re.fullmatch(r"(my )?(days? off|off days?|free days|non-?work(ing)? days)", raw):
        mine = work_days(gen) if gen is not None else list(DAYS[:5])
        return [d for d in DAYS if d not in mine]
    m = re.fullmatch(r"([a-z]+)\s*(?:-|to|through|thru)\s*([a-z]+)", raw)
    if m and _day(m.group(1)) and _day(m.group(2)):
        a, b = DAYS.index(_day(m.group(1))), DAYS.index(_day(m.group(2)))
        return [DAYS[i % 7] for i in range(a, a + ((b - a) % 7) + 1)]
    words = [w for w in re.split(r"[\s,/&+]+", raw) if w.strip() and w not in ("and", "on", "the", "every", "each")]
    found = [_day(w) for w in words]
    if found and all(found):
        return sorted(set(found), key=DAYS.index)
    raise ValueError(f"Could not read the days '{text}'. Say e.g. 'weekdays', 'weekends', 'Mon-Fri', 'Monday and "
                     "Thursday', 'work days' or 'every day'.")


def _day(word: str) -> Optional[str]:
    word = word.strip().rstrip("s").rstrip(".")
    if len(word) < 2:
        return None
    for name, key in _DAY_WORDS.items():
        if name.startswith(word) and len(word) >= 3 or word == key:
            return key
    return {"tu": "tue", "th": "thu", "sa": "sat", "su": "sun", "mo": "mon", "we": "wed", "fr": "fri"}.get(word)


def thirty_index(t: time, round_up: bool = False) -> int:
    """A clock time as a config thirty (rounded down, or up for an end)."""
    minutes = t.hour * 60 + t.minute
    return (minutes // 30 + (1 if round_up and minutes % 30 else 0)) % 48


def describe_days(days: Iterable[str]) -> str:
    days = [d for d in DAYS if d in set(days)]
    if days == list(DAYS):
        return "every day"
    if days == list(DAYS[:5]):
        return "weekdays"
    if days == ["sat", "sun"]:
        return "weekends"
    return ", ".join(d.capitalize() for d in days)


def describe_week(gen) -> str:
    """The week in a few lines, grouping weekdays with the same hours (for the model)."""
    from datetime import timedelta
    from thirties_core.clock import format_clock
    from datetime import datetime
    monday = date(2026, 1, 5)  # any Monday: only the weekday matters

    def clock(thirty: int) -> str:
        return format_clock(datetime.combine(monday, time((thirty % 48) // 2, 30 * (thirty % 2))))
    groups: Dict[str, List[str]] = {}
    for i, key in enumerate(DAYS):
        d = monday + timedelta(days=i)
        h, after = day_hours(gen, d), day_hours(gen, d + timedelta(days=1))
        work = f"work {clock(h.work_start)} – {clock(h.work_end + 1)}" if h.works else "no work"
        groups.setdefault(f"{work}; bed {clock(h.sleep_start)}, up {clock(after.sleep_end)} the next morning",
                          []).append(key)
    return "\n".join(f"- {describe_days(days)}: {text}" for text, days in groups.items())


__all__ = ["DAYS", "DayHours", "day_hours", "parse_days", "describe_days", "describe_week", "thirty_index",
           "weekday_key", "work_days"]
