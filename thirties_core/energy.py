"""What the user says about their energy, used to choose what to offer.

"I've got two hours and I'm really focused, what should I do?": the free time comes
from the plan (or the hours they give), the energy from what they say. Python reads the level (1 drained to 5
great) from their words or a number, keeps it in the energy log, and sorts what's offered to fit:

- low (1-2): light fare first: short, easy things; fewer of them, with room between;
- high (4-5): the bigger things first: time on a project, long or effortful items;
- in between, or not said: the usual order (what's pressing first).

What's due soon keeps its place whatever the energy: a due date doesn't wait for a good day.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

LOW, HIGH = 2, 4           # at or below LOW: lighter fare; at or above HIGH: the bigger things
LIGHT_MINUTES = 30         # light: this long or less, and not time on a project
BIG_MINUTES = 60           # bigger: this long or more, or time on a project

_WORDS = (  # (pattern, level): the first that matches wins
    (r"\b(exhausted|drained|wiped( out)?|dead tired|burn(ed|t) out|running on empty|no energy|zero energy)\b", 1),
    (r"\b(tired|sleepy|meh|sluggish|low( energy)?|worn( out)?|groggy|blah|not great|rough|foggy)\b", 2),
    (r"\b(very|really|super|so|extremely)\s+(motivated|energetic|energized|pumped|productive|focused)\b|"
     r"\b(great|fantastic|amazing|on fire)\b", 5),
    (r"\b(motivated|energetic|energized|pumped|productive|focused|good|rested|fresh|sharp)\b", 4),
    (r"\b(ok|okay|fine|alright|all right|so-so|normal|average)\b", 3),
)


def level_from(value: Any) -> Optional[int]:
    """A level from 1 to 5: a number ("4", "4/5"), or read from the user's words; None if it doesn't say."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(1, min(5, int(value)))
    text = str(value).strip().lower()
    m = re.match(r"^(\d)(?:\s*/\s*5)?$", text)
    if m:
        return max(1, min(5, int(m.group(1))))
    for pattern, level in _WORDS:
        if re.search(pattern, text):
            return level
    return None


def today(assistant, plan) -> Optional[int]:
    """The latest energy the user gave today, or None."""
    try:
        entries = assistant.s.energy.entries(since=plan.day_start)
    except Exception:
        return None
    entries = [e for e in entries if e["at"] < plan.day_end]
    return int(entries[-1]["level"]) if entries else None


def is_light(candidate: Dict[str, Any]) -> bool:
    title = str(candidate.get("title") or "")
    return int(candidate.get("minutes") or 0) <= LIGHT_MINUTES and not title.lower().startswith("time on ") \
        and not candidate.get("session")


def is_big(candidate: Dict[str, Any]) -> bool:
    title = str(candidate.get("title") or "")
    return int(candidate.get("minutes") or 0) >= BIG_MINUTES or title.lower().startswith("time on ") \
        or bool(candidate.get("session"))


def weight(candidate: Dict[str, Any]) -> str:
    """'light', 'bigger' or '' (in between), for the options the model reads."""
    return "light" if is_light(candidate) else ("bigger" if is_big(candidate) else "")


DUE_WITHIN = 2  # days: due this soon, it comes first whatever the energy


def is_due(candidate: Dict[str, Any], day=None) -> bool:
    """Due soon, or said can't wait: it keeps its place whatever the energy."""
    why = str(candidate.get("why") or "").lower()
    if "can't wait" in why or why.startswith("now") or "overdue" in why:
        return True
    task = candidate.get("task")
    due = getattr(task, "due", "") if task is not None else ""
    if due and day is not None:
        from datetime import date
        try:
            return (date.fromisoformat(due[:10]) - day).days <= DUE_WITHIN
        except ValueError:
            return False
    return bool(re.search(r"\bdue (today|tomorrow)\b", why))


def fit_order(cands, level: Optional[int], day=None):
    """The candidates sorted to fit the energy (stable: within each group, the pressing order holds). What's due
    soon stays first."""
    if level is None or LOW < level < HIGH:
        return list(cands)
    fits = is_light if level <= LOW else is_big
    return sorted(cands, key=lambda c: (not is_due(c, day) and not fits(c), not is_due(c, day)))


def line(level: Optional[int]) -> str:
    """The options' first line about energy, or ''."""
    if level is None:
        return ""
    if level <= LOW:
        return (f"Energy is low today ({level}/5): lighter fare. Pick fewer, short, easy things (listed first), with "
                "room between; leave heavy work for another day unless it's due.")
    if level >= HIGH:
        return (f"Energy is high today ({level}/5): a good time for the bigger things (listed first), while it "
                "lasts; what's due still comes first.")
    return ""


__all__ = ["level_from", "today", "is_light", "is_big", "weight", "fit_order", "line", "LOW", "HIGH"]
