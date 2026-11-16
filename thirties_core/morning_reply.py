"""The morning's reply stands on its own.

The reply to the user's first message of the day builds the day and lists it (morning.py, day_lists.py). The
what's-next slice ("Now: …", "Next: …") under it would list the same items again, so that reply never gets one;
the replies after it do, as usual (whats_next.py). The user has seen the day, so a notice soon after needn't
repeat it either.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def without_slice(manager: Any, reply: str) -> str:
    """The morning reply as it is, with the day counted as shown (whats_next.SentSummaries)."""
    try:
        from thirties_core import whats_next
        from thirties_core.astronomy import get_current_time
        plan = manager.day_plan
        if manager.tools.is_current(plan):
            db = manager.scheduler.state_db if manager.scheduler is not None else None
            whats_next.SentSummaries(db).mark(plan, get_current_time(plan.day_start.tzinfo), whole_day=True)
    except Exception as e:  # never costs the user the reply
        logger.debug("Couldn't count the morning's day as shown: %s", e)
    return reply


__all__ = ["without_slice"]
