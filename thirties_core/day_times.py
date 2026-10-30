"""Actual wake, sleep and work times (ROADMAP B6): what really happened to the day's envelopes.

Sleep and Work are planned from the user's settings every day; what really
happened was never recorded, so a late start or a long workday looked like a
plan the user didn't follow. Thirties now asks (with the morning, "when did you
wake up / start work?", and at the end of work, "done with work?") and records
the answer on the Sleep and Work spans themselves, the same way an outcome is
recorded on an activity:

- ``actual_start`` / ``actual_end`` hold what's known (either may be missing:
  a wake time says nothing about bedtime), ``time_source`` says how reliable,
  and ``planned_start`` / ``planned_end`` keep the plan.
- The span moves to the real times, so everything that reads the plan (open
  time, the rollover's placement, the day's stretches, messages) follows what
  happened without knowing about this module.
- Statistics read the actual times (``day_time_facts``).

A fitness tracker can supply sleep later through ``record_day_times``; the
model's tool is a thin wrapper around it. Sleep and wake are asked for now.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.clock import ClockError, format_clock, resolve_clock
from thirties_core.models import DayPlan, EventSpan, SpanCategory

# When the questions go out (decided 2026-10-30 for B6; change here). The morning one comes an hour after the
# planned wake, or half an hour into work on a work day that starts within five hours of waking, so "when did you
# start work?" has an answer; the end-of-work one at the planned end of work. Each is asked once a day, at most
# ASK_WINDOW late, and never when the answer is already on the record.
MORNING_ASK_AFTER_WAKE = timedelta(minutes=60)
MORNING_ASK_AFTER_WORK_START = timedelta(minutes=30)
WORK_START_NEAR_WAKE = timedelta(hours=5)
ASK_WINDOW = timedelta(hours=2)
# The message levels that get these questions: they're record-keeping, like the check-ins (light is only heads-ups
# before calendar events).
ASK_AT_LEVELS = ("heavy",)  # at medium they read as nagging (10-30 sim: "pointless pings… I'd already mapped my morning")

RELIABLE = ("reported", "checkin", "tracker")


class DayTimesError(ValueError):
    """The times can't be recorded as given; nothing was changed."""


# ----------------------------------------------------------------------------
# Finding the day's envelopes
# ----------------------------------------------------------------------------

def _sleeps(plan: DayPlan, extra: Optional[List[EventSpan]] = None) -> List[EventSpan]:
    pool = {s.id: s for s in (extra or [])}
    pool.update({s.id: s for s in plan.spans})
    return sorted((s for s in pool.values() if s.category == SpanCategory.SLEEP), key=lambda s: s.start_dt.timestamp())


def morning_sleep(plan: DayPlan, extra: Optional[List[EventSpan]] = None) -> Optional[EventSpan]:
    """The night the user woke from on this day: the sleep that ends latest between six hours before the canvas
    starts (an early riser wakes before sunrise) and twelve hours after."""
    lo, hi = plan.day_start - timedelta(hours=6), plan.day_start + timedelta(hours=12)
    ends = [s for s in _sleeps(plan, extra) if lo.timestamp() <= s.end_dt.timestamp() <= hi.timestamp()]
    return max(ends, key=lambda s: s.end_dt.timestamp(), default=None)


def night_sleep(plan: DayPlan, extra: Optional[List[EventSpan]] = None) -> Optional[EventSpan]:
    """Tonight's sleep: the first that starts after the morning's wake."""
    first = morning_sleep(plan, extra)
    after = first.end_dt if first else plan.day_start
    later = [s for s in _sleeps(plan, extra) if s.start_dt.timestamp() > after.timestamp()]
    return later[0] if later else None


def work_span(plan: DayPlan) -> Optional[EventSpan]:
    works = sorted((s for s in plan.spans if s.category == SpanCategory.WORK), key=lambda s: s.start_dt.timestamp())
    return works[0] if works else None


def wake_time(plan: DayPlan, extra: Optional[List[EventSpan]] = None) -> Optional[datetime]:
    """When the user woke (or is planned to) on this day: the actual wake if it's recorded."""
    sleep = morning_sleep(plan, extra)
    if sleep is None:
        return None
    return sleep.actual_end or sleep.end_dt


# ----------------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------------

def _set(span: EventSpan, start: Optional[datetime], end: Optional[datetime], source: str) -> None:
    """Record actual times on an envelope and move it there, keeping the plan."""
    new_start = start or span.start_dt
    new_end = end or span.end_dt
    if new_start.timestamp() >= new_end.timestamp():
        raise DayTimesError(f"{span.title} would end before it starts ({format_clock(new_start)} – "
                            f"{format_clock(new_end)}).")
    if span.planned_start is None:
        span.planned_start, span.planned_end = span.start_dt, span.end_dt
    if start is not None:
        span.actual_start = start
    if end is not None:
        span.actual_end = end
    span.start_dt, span.end_dt = new_start, new_end
    if span.time_source not in RELIABLE or source in RELIABLE:
        span.time_source = source


def _clock(text: Any, plan: DayPlan, now: datetime, after: Optional[datetime] = None) -> datetime:
    raw = str(text).strip().lower()
    if raw in ("now", "just now", "right now"):
        return now
    try:
        return resolve_clock(raw, plan.day_start, after=after)
    except ClockError as e:
        raise DayTimesError(str(e))


def record_day_times(plan: DayPlan, now: datetime, *, woke: Any = None, slept: Any = None, work_started: Any = None,
                     work_ended: Any = None, source: str = "reported",
                     history: Optional[List[EventSpan]] = None) -> Tuple[List[EventSpan], List[str]]:
    """Record what's given on this day's envelopes. Returns (changed spans, sentences). ``history``: stored spans
    around the day, for a night that ended before the canvas starts. Raises DayTimesError (nothing changed)."""
    changes: Dict[str, Tuple[EventSpan, Optional[datetime], Optional[datetime]]] = {}
    said: List[str] = []

    def stage(span: EventSpan, start: Optional[datetime] = None, end: Optional[datetime] = None) -> None:
        _, s0, e0 = changes.get(span.id, (span, None, None))
        changes[span.id] = (span, start or s0, end or e0)

    if woke not in (None, ""):
        sleep = morning_sleep(plan, history)
        if sleep is None:
            raise DayTimesError("There's no night's sleep before this day to put a wake-up time on.")
        t = _clock(woke, plan, now)
        if t.timestamp() > (now + timedelta(minutes=5)).timestamp():
            t -= timedelta(days=1)  # "woke at 6" said at 9 AM, on a canvas that starts at 7
        stage(sleep, end=t)
        said.append(f"woke {format_clock(t)}")
    if slept not in (None, ""):
        t = _clock(slept, plan, now)
        if t.timestamp() > (now + timedelta(minutes=15)).timestamp():
            # "went to bed at 1" said in the morning: later than now on this day's clock, so it was last night
            t -= timedelta(days=1)
            night = morning_sleep(plan, history)
        else:
            night = night_sleep(plan, history)
        if night is None:
            raise DayTimesError("There's no sleep on the plan to put a bedtime on.")
        stage(night, start=t)
        said.append(f"went to sleep {format_clock(t)}")
    if work_started not in (None, "") or work_ended not in (None, ""):
        work = work_span(plan)
        if work is None:
            raise DayTimesError("There's no Work on this day's plan. If they worked, add it with add_event first.")
        start = _clock(work_started, plan, now) if work_started not in (None, "") else None
        end = _clock(work_ended, plan, now, after=start or work.start_dt) if work_ended not in (None, "") else None
        stage(work, start=start, end=end)
        if start:
            said.append(f"started work {format_clock(start)}")
        if end:
            said.append(f"finished work {format_clock(end)}")
    if not changes:
        raise DayTimesError("Say which times: woke, slept, work_started or work_ended.")
    # check everything first, so a bad time changes nothing
    for span, start, end in changes.values():
        if (start or span.start_dt).timestamp() >= (end or span.end_dt).timestamp():
            raise DayTimesError(f"{span.title} would end before it starts.")
    for span, start, end in changes.values():
        _set(span, start, end, source)
    return [c[0] for c in changes.values()], said


# ----------------------------------------------------------------------------
# Asking
# ----------------------------------------------------------------------------

def _known(span: Optional[EventSpan], end: bool) -> bool:
    if span is None:
        return True  # nothing to ask about
    value = span.actual_end if end else span.actual_start
    return value is not None and span.time_source in RELIABLE


def due_question(plan: DayPlan, now: datetime, asked: Dict[str, str],
                 history: Optional[List[EventSpan]] = None) -> Optional[Tuple[str, str]]:
    """The question about the day's actual times that's due now, as (key, text), or None. ``asked`` holds the keys
    already asked (they're per day)."""
    day = plan.target_date.isoformat()
    sleep, work = morning_sleep(plan, history), work_span(plan)
    wake = (sleep.planned_end or sleep.end_dt) if sleep else plan.day_start
    work_start = (work.planned_start or work.start_dt) if work else None

    key = f"morning:{day}"
    ask_at = wake + MORNING_ASK_AFTER_WAKE
    if work_start and work_start - wake <= WORK_START_NEAR_WAKE:
        ask_at = max(ask_at, work_start + MORNING_ASK_AFTER_WORK_START)
    need_wake = sleep is not None and not _known(sleep, end=True)
    need_start = work is not None and work_start is not None and not _known(work, end=False) \
        and work_start <= ask_at
    if key not in asked and (need_wake or need_start) and ask_at <= now < ask_at + ASK_WINDOW:
        if need_wake and need_start:
            text = "☀️ For the record: when did you wake up, and when did you start work?"
        elif need_wake:
            text = "☀️ For the record: when did you wake up?"
        else:
            text = "💼 For the record: when did you start work?"
        return key, text

    key = f"work_end:{day}"
    if work is not None and key not in asked and not _known(work, end=True):
        end = work.planned_end or work.end_dt
        if end <= now < end + ASK_WINDOW and work.start_dt < now:
            return key, f"💼 Done with work? If so, when did you finish? (It was planned until {format_clock(end)}.)"
    return None


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

def _round15(minutes: float) -> int:
    return int(round(minutes / 15.0)) * 15


def _clock_of(minutes_after_noon: int, ref: datetime) -> str:
    base = ref.replace(hour=12, minute=0, second=0, microsecond=0)
    return format_clock(base + timedelta(minutes=minutes_after_noon))


def _after_noon(t: datetime) -> int:
    """Minutes after noon (0..1439), so bedtimes either side of midnight average sensibly."""
    return ((t.hour - 12) % 24) * 60 + t.minute


def _after_midnight(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _late(minutes: int) -> str:
    if abs(minutes) < 15:
        return "about on time"
    h, m = divmod(abs(minutes), 60)
    size = (f"{h}h {m:02d}m" if m else f"{h}h") if h else f"{m} minutes"
    return f"about {size} {'later' if minutes > 0 else 'earlier'} than planned"


def day_time_facts(spans: List[EventSpan], min_samples: int = 3) -> List[str]:
    """Plain facts from recorded wake, bed and work times (reliable ones only); silent where data is thin. Times
    are rounded to the quarter hour: a typical time isn't a minute-exact fact."""
    facts: List[str] = []
    sleeps = [s for s in spans if s.category == SpanCategory.SLEEP and s.time_source in RELIABLE]
    wakes = [s for s in sleeps if s.actual_end]
    if len(wakes) >= min_samples:
        median = _round15(statistics.median(_after_midnight(s.actual_end) for s in wakes))
        drift = _round15(statistics.median((s.actual_end - (s.planned_end or s.actual_end)).total_seconds() / 60
                                           for s in wakes))
        ref = wakes[-1].actual_end
        facts.append(f"You usually wake around {format_clock(ref.replace(hour=0, minute=0) + timedelta(minutes=median))}"
                     f", {_late(drift)} ({len(wakes)} mornings recorded).")
    beds = [s for s in sleeps if s.actual_start]
    if len(beds) >= min_samples:
        median = _round15(statistics.median(_after_noon(s.actual_start) for s in beds))
        facts.append(f"You usually go to sleep around {_clock_of(median, beds[-1].actual_start)} "
                     f"({len(beds)} nights recorded).")
    works = [s for s in spans if s.category == SpanCategory.WORK and s.time_source in RELIABLE]
    starts = [s for s in works if s.actual_start]
    if len(starts) >= min_samples:
        drift = _round15(statistics.median((s.actual_start - (s.planned_start or s.actual_start)).total_seconds() / 60
                                           for s in starts))
        facts.append(f"Work usually starts {_late(drift)} ({len(starts)} days recorded).")
    whole = [s for s in works if s.actual_start and s.actual_end]
    if len(whole) >= min_samples:
        actual = _round15(statistics.median((s.actual_end - s.actual_start).total_seconds() / 60 for s in whole))
        planned = _round15(statistics.median(((s.planned_end or s.end_dt) - (s.planned_start or s.start_dt))
                                             .total_seconds() / 60 for s in whole))
        h, m = divmod(actual, 60)
        took = f"{h}h {m:02d}m" if m else f"{h}h"
        ph, pm = divmod(planned, 60)
        plan_text = f"{ph}h {pm:02d}m" if pm else f"{ph}h"
        facts.append(f"A workday usually runs about {took} (planned {plan_text}; {len(whole)} days recorded).")
    return facts


__all__ = ["record_day_times", "due_question", "day_time_facts", "wake_time", "morning_sleep", "night_sleep",
           "work_span", "DayTimesError", "ASK_AT_LEVELS"]
