"""How on track a period is: "how on track are we" at each horizon (ROADMAP C2).

For a week, a month or a year that has a plan (items dropped into it, horizons.py), Python counts what's planned,
what's done, what slipped and how much of the period is left, and says it in a line of plain words that names the
items ("Planned for this week: Bisque the bowls, Wedge the stoneware and Label the glazes; done: Label the glazes;
not done by the day it was meant for: Bisque the bowls; two days to go"). It never gives a bare tally ("1 of 3 done") or a label
("behind", "on track"): the model repeats such words, and the user can't tell what they count. show_period carries it for the period and for each week of a month or month of a
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


@dataclass
class Track:
    planned: int
    done: int
    slipped: List[str] = field(default_factory=list)
    planned_titles: List[str] = field(default_factory=list)  # everything planned (not gone), in plan order
    done_titles: List[str] = field(default_factory=list)
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
    kept = [r for r in items if r["status"] != "gone"]
    done = [r["title"] for r in kept if r["status"] == "done"]
    slipped = [r["title"] for r in slipped_items(items, p, today, planner.week_start)]
    left = (p.end - today).days + 1 if p.start <= today <= p.end else 0
    return Track(planned=len(kept), done=len(done), slipped=slipped, days_left=left,
                 starts_in=(p.start - today).days if p.start > today else 0,
                 planned_titles=[r["title"] for r in kept], done_titles=done)


def _left(days: int) -> str:
    """Days left in natural units: days up to two weeks, then weeks, then months."""
    if days == 1:
        return "today is the last day"
    if days <= 13:
        return f"{days} days left"
    if days <= 62:
        return f"{round(days / 7)} weeks left"
    return f"about {round(days / 30.4)} months left"


def _names(titles: List[str]) -> str:
    """Every item by name, however many (a count in place of names leaves the user to find them): "A, B and C"."""
    titles = [t for t in titles if t]
    return titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + f" and {titles[-1]}" if titles else ""


def summary(t: Track, plan: bool = False) -> str:
    """The facts in plain words, naming the items, no capital or full stop; ``plan``: start with what's planned.
    Empty when nothing is planned. (e.g. "none done yet", then the days to go; or "done: Label the glazes", then
    the days to go)"""
    if t.planned == 0:
        return ""
    parts = [f"{_names(t.planned_titles)} planned"] if plan and t.planned_titles else []
    if t.starts_in:
        return "; ".join(parts + [f"starts in {t.starts_in} day{'s' if t.starts_in != 1 else ''}"])
    if t.done >= t.planned:
        parts.append("all done")
    else:
        parts.append(f"done: {_names(t.done_titles)}" if t.done and t.done_titles else
                     "some done" if t.done else "none done yet")
        if t.over:
            open_ = [x for x in t.planned_titles if x not in t.done_titles]
            if open_:
                parts.append(f"not done: {_names(open_)}")
        elif t.slipped:
            parts.append(f"not done by the day {'it was' if len(t.slipped) == 1 else 'they were'} meant for: "
                         f"{_names(t.slipped)}")
    if not t.over:
        parts.append(_left(t.days_left))
    return "; ".join(parts)


def track_line(planner: "HorizonPlanner", p: "Period", label: str = "") -> str:
    """One line for a period: "Planned for this week: Wedge the stoneware and Label the glazes; done: Label the
    glazes", then what's not done by its day and the days to go. With no label (the items are listed beside it), the
    summary alone, as a sentence. Empty when nothing is planned for it."""
    t = track(planner, p)
    text = summary(t)
    if not text:
        return ""
    if label:
        return f"Planned for {label}: {_names(t.planned_titles)}; {text}."
    return text[:1].upper() + text[1:] + "."


__all__ = ["Track", "track", "track_line", "summary", "slipped_items"]
