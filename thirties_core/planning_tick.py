"""One check for planning, run with each settle of moot items (the background service's tick, and each
turn's prompt): what's in progress keeps going or finishes by itself (in_progress.py), the next meal is spaced from
the last one eaten (meals.py), and the leave-by and follow-up items the user always wants go on (travel.py)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


def tick(tools, now: Optional[datetime] = None) -> List[str]:
    """Run each part on the day being lived; one failing never stops the others. Returns what changed."""
    from thirties_core import in_progress, meals, travel
    plan = tools.day_plan
    now = now or tools._now()
    if tools._state_db is None or not (plan.day_start <= now < plan.day_end):
        return []
    out: List[str] = []
    for name, part in (("in progress", lambda: in_progress.tick(tools, plan, now)),
                       ("meal spacing", lambda: meals.respace(tools, plan, now)),
                       ("planned around", lambda: travel.plan_around(tools, plan, now))):
        try:
            out += part()
        except Exception as e:  # never in the way of the check itself
            logger.warning("Planning check (%s) failed: %s", name, e)
    if out:
        logger.info("Planning check: %s", "; ".join(out))
    return out


__all__ = ["tick"]
