"""The morning's reply stands on its own.

The reply to the user's first message of the day builds the day and lists it (morning.py, day_lists.py). The
what's-next slice ("Now: …", "Next: …") under it would list the same items again, so that reply never gets one;
the replies after it do, as usual (whats_next.py). The user has seen the day, so a notice soon after needn't
repeat it either. A new week's plan is said in this reply too, in one line, never in a message of its own.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def with_week(manager: Any, reply: str) -> str:
    """A new week's plan, made while the day was built (morning.week_line), is part of the morning reply: when the
    reply doesn't name what went in, its line goes under it. Never a message of its own."""
    try:
        from thirties_core import morning
        db = manager.scheduler.state_db if manager.scheduler is not None else None
        line, names = morning.week_notice(db, manager.day_plan.target_date)
        if not line:
            return reply
        low = (reply or "").lower()
        if line.lower() in low or (names and all(n.split(" › ")[-1].strip().lower() in low for n in names)):
            return reply
        from thirties_core.messages import join_sections
        return join_sections((reply or "").rstrip(), line)
    except Exception as e:  # never costs the user the reply
        logger.debug("Couldn't add the week's line to the morning: %s", e)
        return reply


def without_slice(manager: Any, reply: str) -> str:
    """The morning reply as it is (with a new week's line: ``with_week``), with the day counted as shown
    (whats_next.SentSummaries)."""
    reply = with_week(manager, reply)
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


__all__ = ["with_week", "without_slice"]
