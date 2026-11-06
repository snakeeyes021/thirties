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
    """The user has started this day: they've written since it began (sunrise, rounded down, or their planned
    wake if that's earlier) and haven't slept since, or their waking is on the record (log_day_times)."""
    from thirties_core.day_times import morning_sleep
    history = []
    if db is not None:
        try:
            history = db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
        except Exception:
            history = []
    sleep = morning_sleep(plan, history)
    said = _last_message(db)
    if said is not None:
        starts = min(plan.day_start, sleep.end_dt) if sleep is not None else plan.day_start  # up before sunrise
        slept_since = any(  # a night's sleep ended since they last wrote: not up yet (11-04 replay: 6:30, before
            s.category.value == "sleep" and said < s.end_dt <= now and not s.tentative  # sunrise, still counted
            for s in list(plan.spans) + list(history))                                 # as yesterday)
        if starts <= said <= now + timedelta(minutes=1) and not slept_since:
            return True
    woke = sleep.actual_end if sleep is not None else None
    return woke is not None and woke >= plan.day_start - timedelta(hours=6) and woke <= now


HELLO = "☀️ Good morning! Let me know when you're up and I'll put your schedule together."
MAX_HELLO_ITEMS = 12


def hello_time(config, plan, history=None) -> Optional[datetime]:
    """When the first message of the day goes out (assistant.morning_message_at): at sunrise, or at the
    scheduled wake-up, the end of the night's planned Sleep (default). Both are worked out offline: sunrise from
    the location in the settings (NOAA equations, astronomy.py)."""
    when = getattr(config.assistant, "morning_message_at", "wake") or "wake"
    if when == "sunrise":
        return getattr(plan, "sunrise", None)
    from thirties_core.day_times import morning_sleep
    sleep = morning_sleep(plan, history or [])
    if sleep is None:
        return plan.day_start
    return sleep.planned_end or sleep.end_dt  # planned, even if a wake was recorded since


def hello_text(titles) -> str:
    """The one message before the user is up: the wake-up line, and (if the setting is on) the day's items as a
    plain list with no times: what's on the plate, not a schedule yet."""
    items = []
    for t in titles:
        if t and t.lower() not in {i.lower() for i in items}:
            items.append(t)
    if not items:
        return HELLO
    shown = items[:MAX_HELLO_ITEMS]
    more = len(items) - len(shown)
    return (HELLO + "\n\nOn the plate today:\n" + "\n".join(f"- {t}" for t in shown)
            + (f"\n…and {more} more" if more else ""))
