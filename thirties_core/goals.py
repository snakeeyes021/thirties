"""Goals in the profile carry a date, and stale ones get checked before they're offered (A7).

10-19 10:16: "did we not finish the hiding of the Ethernet cable last night? Why
are you suggesting it?" The profile's "Near-term goals" were weeks old and fed
the suggestions. Now each goal line carries the day it was last known to be
current, as a plain "(as of 2026-10-30)" at its end, which the user sees and can
edit like the rest of the profile. Python works out which are stale; the model
only asks the user about them, once, and records the answer with check_goal.
Undated lines (from before this) count as stale: nobody knows how old they are.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

# How long a goal stays current without a check (decided 2026-10-30; change here). Near-term goals are weeks-long
# things ("hide the Ethernet cable", "merge Redfox"); long-term ones change slowly.
STALE_AFTER_DAYS = {"Near-term goals": 14, "Long-term goals": 90}
GOAL_SECTIONS = tuple(STALE_AFTER_DAYS)
CHECK_STATES = ("current", "done", "dropped")

_AS_OF_RE = re.compile(r"\s*\(as of (\d{4}-\d{2}-\d{2})\)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def split_line(line: str) -> Tuple[str, Optional[date]]:
    """A goal line's text (no bullet, no date) and its as-of date, if any."""
    m = _AS_OF_RE.search(line)
    text = _BULLET_RE.sub("", _AS_OF_RE.sub("", line)).strip()
    try:
        return text, (date.fromisoformat(m.group(1)) if m else None)
    except ValueError:
        return text, None


def dated(text: str, day: date) -> str:
    return f"- {split_line(text)[0]} (as of {day.isoformat()})"


def _goal_lines(content: str) -> List[str]:
    return [line for line in (content or "").splitlines() if line.strip() and split_line(line)[0]]


def stamp_new_lines(old: str, new: str, today: date) -> str:
    """The section's new text, with each line that wasn't there before dated today. Lines already there keep their
    date (or stay undated): rewriting a section mustn't make old goals look freshly checked."""
    known = {split_line(line)[0].lower(): line for line in _goal_lines(old)}
    out = []
    for line in (new or "").splitlines():
        text, when = split_line(line)
        if not text:
            out.append(line)
        elif when is not None:
            out.append(line)
        elif text.lower() in known:
            out.append(known[text.lower()])
        else:
            out.append(dated(line, today))
    return "\n".join(out).strip()


def stale_goals(sections: Dict[str, str], today: date) -> List[Tuple[str, str, Optional[date]]]:
    """(section, goal text, as-of date or None) for each goal older than its section allows, oldest first."""
    out = []
    for section, days in STALE_AFTER_DAYS.items():
        for line in _goal_lines(sections.get(section, "")):
            text, when = split_line(line)
            if when is None or (today - when).days > days:
                out.append((section, text, when))
    return sorted(out, key=lambda g: g[2] or date.min)


def prompt_lines(sections: Dict[str, str], today: date) -> List[str]:
    """For the brain: the goals to check with the user before offering anything for them."""
    return [f"- {text} ({section.split()[0].lower()}; " + (f"last checked {when.strftime('%b %d')}" if when else
                                                            "never checked") + ")"
            for section, text, when in stale_goals(sections, today)]


def check_goal(sections: Dict[str, str], goal: str, state: str, today: date) -> Tuple[str, str, str]:
    """Apply the user's answer about a goal. Returns (section, its new content, a sentence). "current" dates it
    today; "done" and "dropped" take it out. Raises ValueError if the goal isn't found or is ambiguous."""
    state = (state or "").strip().lower()
    if state not in CHECK_STATES:
        raise ValueError(f"state must be one of {', '.join(CHECK_STATES)}.")
    want = split_line(goal)[0].lower()
    if not want:
        raise ValueError("Say which goal.")
    every = [(section, line, split_line(line)[0]) for section in GOAL_SECTIONS
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
        lines[i] = dated(line, today)
        said = f"'{text}' is still a goal (checked today)."
    else:
        del lines[i]
        said = f"Took '{text}' out of {section}" + (" (done)." if state == "done" else ".")
    return section, "\n".join(lines).strip(), said


__all__ = ["STALE_AFTER_DAYS", "GOAL_SECTIONS", "split_line", "stamp_new_lines", "stale_goals", "prompt_lines",
           "check_goal"]
