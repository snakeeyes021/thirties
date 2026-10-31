"""How on track a period is: "how on track are we" at each horizon (ROADMAP C2).

For a week, a month or a year that has a plan (items dropped into it, horizons.py), Python counts what's planned,
what's done, what slipped and how much of the period is left, and says it in a line ("Behind, 1 of 4 done; slipped:
File the taxes; 3 days left."). show_period carries it for the period and for each week of a month or month of a
year; the week, month and year planning offers open with how the last one went; the morning proposal mentions the
week. The model reads the line; it never counts (AGENTS.md §3, anti-pattern 2).

**What slipped** (decided here; change ``slipped_items``): in a period that's over, everything planned that isn't
done. In one under way, what was meant for a day that has passed and isn't done: pinned to that day (a week plan's
day), or put on that day's schedule and not marked done. Items no longer on the user's lists are neither done nor
slipped (they may have been finished in Joplin by hand). **On track** means nothing has slipped; a period with
open items and no slips is on track however many are left, because the days left are the user's to spend, and
Python doesn't guess their pace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from thirties_core.horizons import HorizonPlanner, Period

# Titles named in a line before "and N more": a line, not a report.
MAX_NAMED = 3


@dataclass
class Track:
    planned: int
    done: int
    slipped: List[str] = field(default_factory=list)
    days_left: int = 0      # today included; 0 once the period is over
    starts_in: int = 0      # days until a future period starts

    @property
    def over(self) -> bool:
        return self.days_left == 0 and self.starts_in == 0

    @property
    def on_track(self) -> bool:
        return not self.slipped


def slipped_items(items: List[Dict], p: "Period", today: date, week_start: int = 0) -> List[Dict]:
    """The plan's items that slipped (see the module docstring)."""
    from thirties_core.horizons import slot_period
    out = []
    for r in items:
        if r["status"] in ("done", "gone"):
            continue
        if p.end < today:
            out.append(r)
            continue
        slot = slot_period(r, week_start) if r.get("slot") else None
        meant = slot.end if slot is not None else None
        if r["status"] == "scheduled" and r.get("on"):
            meant = r["on"] if meant is None else max(meant, r["on"])
        if meant is not None and meant < today:
            out.append(r)
    return out


def track(planner: "HorizonPlanner", p: "Period") -> Track:
    today = planner.today()
    items = planner.plan_items(p)
    done = sum(1 for r in items if r["status"] == "done")
    slipped = [r["title"] for r in slipped_items(items, p, today, planner.week_start)]
    left = (p.end - today).days + 1 if p.start <= today <= p.end else 0
    return Track(planned=sum(1 for r in items if r["status"] != "gone"), done=done, slipped=slipped,
                 days_left=left, starts_in=(p.start - today).days if p.start > today else 0)


def _left(days: int) -> str:
    """Days left in natural units: days up to two weeks, then weeks, then months."""
    if days == 1:
        return "last day"
    if days <= 13:
        return f"{days} days left"
    if days <= 62:
        return f"{round(days / 7)} weeks left"
    return f"about {round(days / 30.4)} months left"


def _names(titles: List[str]) -> str:
    named = ", ".join(titles[:MAX_NAMED])
    return named + (f" and {len(titles) - MAX_NAMED} more" if len(titles) > MAX_NAMED else "")


def summary(t: Track) -> str:
    """The facts in a few words, no capital or full stop. Empty when nothing
    is planned. ("on track, 2 of 5 done, 4 days left")"""
    if t.planned == 0:
        return ""
    if t.starts_in:
        return f"{t.planned} planned, starts in {t.starts_in} day{'s' if t.starts_in != 1 else ''}"
    counts = f"{t.done} of {t.planned} done"
    if t.done == t.planned:
        return f"all {t.planned} done" + ("" if t.over else f", {_left(t.days_left)}")
    if t.over:
        return counts + (f"; slipped: {_names(t.slipped)}" if t.slipped else "")
    if t.slipped:
        return f"behind, {counts}; slipped: {_names(t.slipped)}; {_left(t.days_left)}"
    return f"on track, {counts}, {_left(t.days_left)}"


def track_line(planner: "HorizonPlanner", p: "Period", label: str = "") -> str:
    """One line for a period: "This week: behind, 1 of 4 done; slipped: File the taxes; 3 days left." With no
    label, the summary alone, as a sentence. Empty when nothing is planned for it."""
    text = summary(track(planner, p))
    if not text:
        return ""
    if label:
        return f"{label[:1].upper()}{label[1:]}: {text}."
    return text[:1].upper() + text[1:] + "."


__all__ = ["Track", "track", "track_line", "summary", "slipped_items", "MAX_NAMED"]
