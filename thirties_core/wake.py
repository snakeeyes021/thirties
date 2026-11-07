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
from typing import List, Optional

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


MAX_HELLO_ITEMS = 12
RECENT_KEY = "morning_hello_recent"  # the last few first messages, so the model doesn't repeat itself
PLATE_HEADING = "On the plate today:"  # copy-madlibs T3-003
FALLBACK = {  # when no model can write it (copy-madlibs T3-001, T3-002): the time and the offer, nothing more
    "sunrise": "☀️ {clock} sunrise. Tell me when you're up and I'll put your schedule together.",
    "wake": "{clock}, your wake-up time. Tell me when you're up and I'll put your schedule together.",
}


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


def hello_prompt(mode: str, clock: str, recent: List[str]) -> str:
    """For the model: the first message of the day, fresh each morning (11-05: never the same canned line)."""
    framing = (f"say it's {clock} and frame that as today's sunrise (the sun is just coming up), and include the "
               "☀️ emoji" if mode == "sunrise" else
               f"say it's {clock} and frame that as the wake-up time the user set; no emoji needed")
    avoid = ("\nDon't reuse the wording of these earlier ones:\n" + "\n".join(recent)) if recent else ""
    return ("Write the first message of the user's day for Thirties, their planning assistant: one or two short "
            f"sentences, warm and plain. In it: greet them; {framing}; and offer to put their schedule together once "
            "they're up or ready (you'll plan the day after they reply). The greeting and the time can be one "
            "phrase. No list, no questions about tasks, no quotation marks. Write it fresh, in your own words."
            f"{avoid}\nReply with only the message.")


def check_line(text: str, mode: str, at: datetime) -> Optional[str]:
    """The model's line, if it has what it must (the time; in sunrise mode the ☀️) and is short; else None."""
    import re
    line = " ".join(str(text or "").strip().strip('"').split())
    if not line or len(line) > 320 or line.lower().startswith(("error", "the antigravity", "i cannot", "i can't")):
        return None
    hour = at.hour % 12 or 12
    minute = f"{at.minute:02d}"
    pattern = rf"\b{hour}:{minute}\b" if at.minute else rf"\b{hour}(?::00)?\s*(?:a\.?m\.?|o'?clock)|\b{hour}:00\b"
    if not re.search(pattern, line, re.I):
        return None
    if mode == "sunrise" and "☀" not in line:
        line = "☀️ " + line
    return line


def write_hello(engine, mode: str, at: datetime, recent: List[str]) -> str:
    """The first message's line: written by the model, or the plain fallback."""
    from thirties_core.clock import format_clock
    clock = format_clock(at)
    if engine is not None:
        try:
            reply = engine.chat([{"role": "user", "content": hello_prompt(mode, clock, recent)}])
            line = check_line((reply or {}).get("content", ""), mode, at)
            if line:
                return line
        except Exception:
            pass
    return FALLBACK.get(mode, FALLBACK["wake"]).format(clock=clock)


def hello_text(line: str, titles) -> str:
    """The one message before the user is up: the line, and (if the setting is on) the day's items, one per line,
    no bullets, no times (11-05): what's on the plate, not a schedule yet."""
    items = []
    for t in titles:
        if t and t.lower() not in {i.lower() for i in items}:
            items.append(t)
    if not items:
        return line
    shown = items[:MAX_HELLO_ITEMS]
    more = len(items) - len(shown)
    return (line + f"\n\n{PLATE_HEADING}\n" + "\n".join(shown) + (f"\n…and {more} more" if more else ""))
