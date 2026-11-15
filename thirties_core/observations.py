"""Observations from the statistics, written into the profile: the assistant uses the statistics to keep notes
there now and then.

Once a week, Python takes the statistics that describe lasting patterns (how long things really take, the part
of the day plans get done in, energy, what keeps getting put off, when the user wakes and sleeps) and writes them
into the profile's Observations section, each ending "(from the statistics, <day>)". The next week's lines
replace those; anything else in the section (the user's own words, or the assistant's) is never touched. The
user sees and edits them like the rest of the profile; deleting a line is fine, it comes back only if it's still
true at the next refresh.

Counts about one day (how many items have no outcome yet, today's heads-up) aren't observations and stay out.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

SECTION = "Observations"
EVERY_DAYS = 7
MAX_LINES = 6
KEY = "observations_written"   # sync meta: the day they were last written
TAG = "(from the statistics, {day})"
_TAG_RE = re.compile(r"\s*\(from the statistics, \d{4}-\d{2}-\d{2}\)\s*$")
# what a lasting pattern sounds like, from stats.fact_lines and day_times.day_time_facts
_PATTERN_RE = re.compile(r"usually takes|get done \d+% of the time|^Completion is|has been put off|"
                         r"usually wake|usually go to sleep|Work usually starts|workday usually runs")


def lasting(facts: List[str]) -> List[str]:
    return [f for f in facts if _PATTERN_RE.search(f)][:MAX_LINES]


def merged(current: str, facts: List[str], today: date) -> str:
    """The section with this refresh's lines in place of the last one's; the rest kept as it was."""
    kept = [line for line in (current or "").splitlines() if line.strip() and not _TAG_RE.search(line)]
    tag = TAG.format(day=today.isoformat())
    return "\n".join(kept + [f"- {f} {tag}" for f in facts]).strip()


def refresh(profile, facts: List[str], today: date) -> bool:
    """Write the observations into the profile. True when the section changed."""
    current = profile.sections().get(SECTION, "")
    new = merged(current, lasting(facts), today)
    if new == (current or "").strip():
        return False
    profile.set_section(SECTION, new)
    return True


def refresh_if_due(assistant, today: Optional[date] = None) -> bool:
    """Once a week, from the assistant's own statistics. Never raises: a failed refresh waits for the next one."""
    try:
        db = assistant._db
        if db is None:
            return False
        today = today or assistant._today()
        last = db.get_sync_meta(KEY)
        if last and (today - date.fromisoformat(last)).days < EVERY_DAYS:
            return False
        facts = lasting(assistant.statistics_facts())
        db.set_sync_meta(KEY, today.isoformat())
        if not facts:
            return False
        return refresh(assistant.s.profile, facts, today)
    except Exception:  # pragma: no cover - best effort
        import logging
        logging.getLogger(__name__).warning("Writing observations into the profile failed", exc_info=True)
        return False


__all__ = ["SECTION", "lasting", "merged", "refresh", "refresh_if_due", "EVERY_DAYS"]
