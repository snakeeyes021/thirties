"""What put_routine says is only what happened to each step.

put_routine moves a routine's part by taking its steps off and placing each one again at the part's new time. It
said "Moved <routine>: <time>: <four steps>" from the times it asked for, not from where the steps landed: asked to
move a later part, it put one step at the new time while the other three went back to where they were (a time the
user had only quoted was read as a time they gave), and the reply still said all four had moved. A step whose
placing was refused stopped the whole call, after the steps before it had been taken off, so they were off the plan
with no word about it.

Now:

- each step's line is written from where it actually landed on the plan, grouped by thirty;
- a step that can't be placed goes back where it was, and the result names it, says it stayed, and why;
- with nothing placed at all, the call is refused with the reasons, and everything is where it was; a call that
  stops on an error puts every step it took off and hadn't placed again back where it was.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple

from thirties_core.clock import format_clock, thirty_of

if TYPE_CHECKING:
    from thirties_core.models import DayPlan, EventSpan
    from thirties_core.plan_tools import PlanTools


def _key(title: str) -> str:
    from thirties_core.step_times import key
    return key(title)


def landed(plan: "DayPlan", before: Iterable[str], title: str) -> Optional["EventSpan"]:
    """The span a step was just placed as: a new one with its name, else the planned one with its name."""
    from thirties_core.models import SpanStatus
    before, want = set(before), _key(title)
    named = [s for s in plan.spans if _key(s.title) == want and s.status == SpanStatus.PLANNED]
    fresh = [s for s in named if s.id not in before]
    return (fresh or named or [None])[-1]


def put_back(pt: "PlanTools", plan: "DayPlan", gone: List["EventSpan"], title: str) -> Optional[datetime]:
    """A step taken off for the move that couldn't be placed again goes back where it was. Its old start, or None
    when it wasn't on the plan before."""
    want = _key(title)
    back = [s for s in gone if _key(s.title) == want]
    for s in back:
        if all(o.id != s.id for o in plan.spans):
            plan.spans.append(s)
            pt._persist(s)
        gone.remove(s)
    plan.spans.sort(key=lambda s: s.start_dt)
    return back[0].start_dt if back else None


def settled(gone: List["EventSpan"], title: str) -> None:
    """A step placed at its new time: its old place isn't going back."""
    want = _key(title)
    gone[:] = [s for s in gone if _key(s.title) != want]


def put_all_back(pt: "PlanTools", plan: "DayPlan", gone: List["EventSpan"]) -> None:
    """Every step taken off and not placed again goes back where it was (the move stopped part way)."""
    for s in list(gone):
        if s in gone:
            put_back(pt, plan, gone, s.title)


def lines(where: List[Tuple[str, datetime, datetime]]) -> List[str]:
    """"8:30 PM: Check the glue pots, Sort the offcuts" for each part, in time order, from (step, where it landed,
    the part's start): a step that landed in its part's half hour is said with its part, any other by the thirty
    it's in."""
    groups: Dict[datetime, List[str]] = {}
    for title, start, first in where:
        mine = first <= start <= first + timedelta(minutes=30)
        groups.setdefault(thirty_of(first) if mine else thirty_of(start), []).append(title)
    return [f"{format_clock(t)}: {', '.join(names)}" for t, names in sorted(groups.items())]


def not_moved(refused: List[Tuple[str, Optional[datetime], str]]) -> str:
    """What the result says about the steps that couldn't be placed: each by name, where it is, and why."""
    out = []
    for title, was, why in refused:
        where = f"stays at {format_clock(was)}" if was is not None else "isn't on the plan"
        out.append(f"{title} {where} ({why.rstrip('.')})")
    return "Not moved: " + "; ".join(out) + "."


def refusal(refused: List[Tuple[str, Optional[datetime], str]]) -> str:
    """Nothing could be placed: the reasons, once each."""
    whys = list(dict.fromkeys(why.rstrip(".") for _, _, why in refused))
    return "; ".join(whys) + "."


__all__ = ["landed", "lines", "not_moved", "put_all_back", "put_back", "refusal", "settled"]
