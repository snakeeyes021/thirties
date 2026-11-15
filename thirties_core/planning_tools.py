"""Tools for the brains, in one place: what's in progress (in_progress.py), how items sit in a day
(traits.py), days off (days_off.py), leave-by and follow-up items (travel.py), and the week ahead
pencilled in (week_draft.py). Registered with the assistant's
tools (assistant_tools.py)."""

from __future__ import annotations

from typing import Any, Dict, List

from thirties_core.days_off import DAYS_OFF_TOOL_SPECS, set_days_off
from thirties_core.in_progress import IN_PROGRESS_TOOL_SPECS, still_doing
from thirties_core.traits import TRAIT_TOOL_SPECS, set_item_traits
from thirties_core.travel import TRAVEL_TOOL_SPECS, follow_up, leave_by
from thirties_core.week_draft import WEEK_DRAFT_TOOL_SPECS, week_ahead

PLANNING_TOOL_SPECS: List[Dict[str, Any]] = (IN_PROGRESS_TOOL_SPECS + TRAIT_TOOL_SPECS + DAYS_OFF_TOOL_SPECS
                                             + TRAVEL_TOOL_SPECS + WEEK_DRAFT_TOOL_SPECS)
PLANNING_READ_ONLY = {"week_ahead"}


def handlers(pt) -> Dict[str, Any]:
    def doing(**kw: Any) -> str:
        out = still_doing(pt, **kw)
        from thirties_core.meals import meal_word, respace
        if meal_word(pt._find_span(kw.get("event")).title):  # still eating: the next meal waits for it
            spaced = respace(pt)
            out += ("\n" + "\n".join(spaced)) if spaced else ""
        return out
    return {
        "still_doing": doing,
        "set_item_traits": lambda **kw: set_item_traits(pt, **kw),
        "set_days_off": lambda **kw: set_days_off(pt, **kw),
        "leave_by": lambda **kw: leave_by(pt, **kw),
        "follow_up": lambda **kw: follow_up(pt, **kw),
        "week_ahead": lambda **kw: week_ahead(pt, **kw),
    }


__all__ = ["PLANNING_TOOL_SPECS", "PLANNING_READ_ONLY", "handlers"]
