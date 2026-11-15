"""The user's own effort estimates, in the unit they choose (minutes, thirties, hours, days or points).

An estimate is stored per item (project_facts, kind "estimate", keyed by the item's id) as what the user said,
the unit, and the minutes it comes to. The unit they usually estimate in is a shared setting
(``lists.effort_unit``: minutes, thirties, hours, days or points); for days and points, how long one is
(``lists.effort_unit_minutes``; a day is 6 hours of work and a point an hour until they say). A bare number is in
their unit.

Where it's used: an item whose estimate fits one sitting (at most SITTING_MINUTES) is planned for that long when
the user hasn't said how long it usually takes; a bigger one is planned in pieces, and the day's options say how
much there is in all (``apply_to_candidates``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

UNIT_KEY = "lists.effort_unit"
UNIT_MINUTES_KEY = "lists.effort_unit_minutes"
UNITS = {"minutes": 1, "thirties": 30, "hours": 60, "days": 360, "points": 60}
SITTING_MINUTES = 120
_ALIASES = {"m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
            "thirty": "thirties", "thirties": "thirties", "block": "thirties", "blocks": "thirties",
            "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
            "d": "days", "day": "days", "days": "days",
            "pt": "points", "pts": "points", "point": "points", "points": "points"}
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "half an": 0.5, "half a": 0.5}
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
                     r"([a-z]+)?", re.I)


def _settings(db):
    from thirties_core.settings import Settings
    return Settings(db)


def unit(db) -> str:
    value = _settings(db).get(UNIT_KEY) if db is not None else None
    return value if value in UNITS else "hours"


def unit_minutes(db, name: str) -> int:
    """How many minutes one of a unit is: the user's own length for days or points, else the fixed one."""
    if name in ("days", "points") and db is not None:
        mine = _settings(db).get(UNIT_MINUTES_KEY) or {}
        if isinstance(mine, dict) and int(mine.get(name) or 0) > 0:
            return int(mine[name])
    return UNITS[name]


def set_unit(db, name: str, length: Any = None) -> str:
    from thirties_core.clock import format_duration, parse_duration_minutes
    name = _ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())
    if name not in UNITS:
        raise ValueError("unit must be minutes, thirties, hours, days or points.")
    _settings(db).set(UNIT_KEY, name)
    out = f"Estimates are in {name}"
    if length not in (None, ""):
        if name not in ("days", "points"):
            raise ValueError(f"{name} have a fixed length; only days and points take one.")
        minutes = parse_duration_minutes(length)
        mine = dict(_settings(db).get(UNIT_MINUTES_KEY) or {})
        mine[name] = minutes
        _settings(db).set(UNIT_MINUTES_KEY, mine)
        out += f", one {name[:-1]} being {format_duration(minutes)}"
    return out + "."


def parse_estimate(db, text: str) -> Dict[str, Any]:
    """{"said", "amount", "unit", "minutes"} from the user's words; ValueError when it says no amount."""
    said = str(text or "").strip()
    low = said.lower()
    total, first_unit, amount = 0.0, "", 0.0
    for m in _AMOUNT.finditer(low):
        raw, word = m.group(1).lower(), (m.group(2) or "").lower()
        if not raw[0].isdigit() and not word:
            continue
        value = float(raw) if raw[0].isdigit() else _WORDS.get(raw, 0)
        name = _ALIASES.get(word, "")
        if not name:
            if word:
                continue  # a number with some other word ("3 people"): not an amount of work
            name = unit(db)
        total += value * unit_minutes(db, name)
        amount += value if not first_unit or name == first_unit else 0
        first_unit = first_unit or name
    if total <= 0:
        raise ValueError(f"'{said}' doesn't say how much work: give an amount, e.g. '3 hours' or '2 thirties'.")
    return {"said": said, "amount": amount, "unit": first_unit, "minutes": int(round(total))}


def describe(db, rec: Dict[str, Any]) -> str:
    """An estimate as the user said it, with what it comes to when that's not obvious."""
    from thirties_core.clock import format_duration
    said = re.sub(r"^(?:about|around|roughly|maybe|probably|approximately|like)\s+", "", rec.get("said") or "",
                  flags=re.I)
    minutes = int(rec.get("minutes") or 0)
    if rec.get("unit") in ("points", "days", "thirties") or not said:
        return f"{said} ({format_duration(minutes)})" if said else format_duration(minutes)
    return said


def estimate_for(db, task_id: str) -> Optional[Dict[str, Any]]:
    if db is None or not task_id:
        return None
    from thirties_core.project_facts import ProjectFacts
    return ProjectFacts(db).get("estimate", task_id)


def apply_to_candidates(assistant, cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The day's candidates with the user's estimates: one that fits a sitting is planned that long, unless they
    said how long it usually takes; a bigger one keeps its piece's length, and says how much there is in all."""
    from thirties_core.clock import format_duration
    db = assistant._db
    if db is None:
        return cands
    out = []
    for c in cands:
        t = c.get("task")
        rec = estimate_for(db, t.id) if t is not None else None
        if rec is None:
            out.append(c)
            continue
        minutes = int(rec.get("minutes") or 0)
        said_length = assistant.lasting_facts(t.title).minutes if hasattr(assistant, "lasting_facts") else 0
        if 0 < minutes <= SITTING_MINUTES and not said_length and not c.get("session") and not c.get("momentum"):
            c = {**c, "minutes": minutes}
        elif minutes > SITTING_MINUTES:
            c = {**c, "why": f"{c.get('why') or ''}; about {format_duration(minutes)} in all (your estimate)".lstrip("; ")}
        out.append(c)
    return out


__all__ = ["SITTING_MINUTES", "UNITS", "apply_to_candidates", "describe", "estimate_for", "parse_estimate",
           "set_unit", "unit", "unit_minutes"]
