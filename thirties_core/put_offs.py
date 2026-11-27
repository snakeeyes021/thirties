"""What counts as putting an item off, and how it's counted.

A put-off is the user (or the model, on the user's words) skipping an item that was on a day's plan, or moving it to
a later day. The count is worked out from those records (``StateDatabase.put_off_records``, kept per item and day in
a window), never from a running counter, so a number that was once too high corrects itself and two devices with
different histories add up to the union of what happened on each.

What isn't a put-off, however the item ended up skipped or moved:
- a day proposal the user left out (skipped before its time, with nothing else done to it since the app put it on);
- a leftover the app rolled on its own, an item taken off a full day by the app's own overflow rule, a repeating
  deadline the app moved on, and a past item that was left unmarked;
- an item that wasn't on any plan.
Only a change the user made counts: the app's own controls, or the model's own call of the tool for the user's message
(not the same tool run by the app, such as the rollover's skips).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHOW_FROM = 2  # a count is only said to the model (and the user) from this many put-offs: one is no pattern

PROPOSAL_TOOLS = ("propose_day", "apply_day_proposal")  # what puts the morning's proposal on the plan


def by_the_user(pt: Any, tool: str) -> bool:
    """The change in progress is the user's: a tap or drag in the app, or the model's own call of ``tool`` for the
    user's message. The app's own work (the rollover, the morning set-up, the overflow rule) is never that, even when
    it runs the same tool from inside a turn."""
    if getattr(pt, "by_hand", False):
        return True
    if getattr(pt, "user_message", None) is None:
        return False
    from thirties_core.plan_log import is_model_call, outer_tool
    return is_model_call() and outer_tool() == tool


def untouched_proposal(db: Any, span: Any, now: datetime) -> bool:
    """The span is still just what the day proposal put on the plan: nothing the user did to it since, and its time
    hasn't come. Skipping it then is saying no to a proposal, not putting off something they planned."""
    if db is None or now >= span.start_dt:
        return False
    try:
        from thirties_core.plan_log import changes_for
        rows = changes_for(db, span_id=span.id)
    except Exception:  # the log must never cost a count
        logger.debug("plan log unreadable", exc_info=True)
        return False
    first = next((r for r in rows if r["change"] == "added"), None)
    if first is None or (first.get("tool") or "").split(" > ")[0] not in PROPOSAL_TOOLS:
        return False
    return all(r is first or r["change"] == "marked skipped" for r in rows)


def record_skip(pt: Any, db: Any, span: Any, task_id: Optional[str], kind_counts: bool) -> Optional[int]:
    """Store a real skip made by the user. Gives the new count, or None when nothing was stored."""
    if db is None or not task_id or not kind_counts or span is None or span.is_envelope:
        return None
    if not by_the_user(pt, "log_outcome") or untouched_proposal(db, span, pt._now()):
        return None
    return db.record_put_off(task_id, "skipped", span_id=span.id, from_day=span.start_dt.date())


def record_move(pt: Any, db: Any, span: Any, task_id: Optional[str], kind_counts: bool, to_date: Any) -> Optional[int]:
    """Store a real move to a later day made by the user. Gives the new count, or None when nothing was stored."""
    if db is None or not task_id or not kind_counts or span is None or span.is_envelope or span.source == "calendar":
        return None
    if to_date <= span.start_dt.date() or not by_the_user(pt, "update_event"):
        return None
    if untouched_proposal(db, span, pt._now()):
        return None
    return db.record_put_off(task_id, "moved", span_id=span.id, from_day=span.start_dt.date())


def count_words(count: int) -> str:
    """What to say of a count: nothing below SHOW_FROM."""
    return f"put off {count} times" if count >= SHOW_FROM else ""


__all__ = ["SHOW_FROM", "by_the_user", "untouched_proposal", "record_skip", "record_move", "count_words"]
