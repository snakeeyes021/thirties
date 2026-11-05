"""When the user's day has started: the one trigger for the morning.

Thirties isn't an alarm clock, so it can't know the user is up just because their planned wake time has passed
(11-04, 11-05: four messages between 7:00 and 7:40, and the routine laid out for a 6:30 wake, on mornings the user
woke at 9). Until the user is up, nothing is set up and nothing is sent, apart from reminders they asked for and at
most one heads-up about a fixed calendar event. The user's first message of the day starts it: the day's routines
go on from then, and the morning proposal follows the reply.

To change what counts as "up" (a fixed time, a sunrise option), change ``user_is_up``; everything that waits for
the user calls it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

LAST_MESSAGE_KEY = "last_user_message_at"  # set by ConversationManager at the start of each user turn


def _last_message(db) -> Optional[datetime]:
    raw = db.get_sync_meta(LAST_MESSAGE_KEY) if db is not None else None
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def user_is_up(db, plan, now: datetime) -> bool:
    """The user has started this day: they've written since the day began (sunrise, rounded down), or their
    waking is on the record (log_day_times)."""
    said = _last_message(db)
    if said is not None and plan.day_start <= said <= now + timedelta(minutes=1):
        return True
    from thirties_core.day_times import morning_sleep
    history = []
    if db is not None:
        try:
            history = db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
        except Exception:
            history = []
    sleep = morning_sleep(plan, history)
    woke = sleep.actual_end if sleep is not None else None
    return woke is not None and woke >= plan.day_start - timedelta(hours=6) and woke <= now
