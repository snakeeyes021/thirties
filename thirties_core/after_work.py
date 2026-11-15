"""When work ends: how much of the day is left, in daylight and dark thirties, and a few things that fit.

"Since it's the middle of winter you have two daylight thirties and 8 dark thirties; here's like... 3 tasks." When
the user says work is over (log_day_times, or the app's button), the reply counts the free thirties left before bed,
split by daylight (a short winter afternoon has few), and offers up to three things from what's pressing that fit
them: sorted to the user's energy (energy.py), something outdoors only if a daylight stretch can hold it. The offer
is made once a day; what's offered isn't put on the plan until the user picks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from thirties_core.clock import format_duration, thirty_ceil

MAX_OFFERED = 3
OFFERED_KEY = "after_work_offer:{day}"


def thirties_left(tools, plan, start: datetime) -> Tuple[int, int, List[Tuple[datetime, datetime]]]:
    """(free daylight thirties, free dark thirties, the free stretches) from ``start`` until bed, leaving the half
    hour after work clear (breathing.py)."""
    from thirties_core.breathing import clip
    from thirties_core.day_times import night_sleep
    bed = night_sleep(plan)
    end = bed.start_dt if bed is not None else plan.day_end
    saved, tools.day_plan = tools.day_plan, plan
    try:
        windows = clip(plan, tools.open_windows(after=start, before=end))
    finally:
        tools.day_plan = saved
    windows = [(max(a, thirty_ceil(start)), b) for a, b in windows if b - max(a, thirty_ceil(start)) >= timedelta(minutes=30)]
    light = dark = 0
    for a, b in windows:
        t = a
        while t + timedelta(minutes=30) <= b:
            block = plan.block_at(t)
            if block is not None and block.is_sunlight:
                light += 1
            else:
                dark += 1
            t += timedelta(minutes=30)
    return light, dark, windows


def _fits(plan, minutes: int, windows, outdoors: bool) -> bool:
    need = timedelta(minutes=minutes)
    for a, b in windows:
        if outdoors:
            a, b = max(a, plan.sunrise), min(b, plan.sunset)
        if b - a >= need:
            return True
    return False


def offer(tools, ended: datetime, once: bool = True) -> str:
    """The reply's lines for the end of work: the thirties left, and up to three things that fit. '' when it was
    already offered today, or the day has no free time left."""
    from thirties_core.replan import outdoors
    plan = tools.day_plan
    db = tools._state_db
    key = OFFERED_KEY.format(day=plan.target_date.isoformat())
    if once and db is not None and db.get_sync_meta(key):
        return ""
    light, dark, windows = thirties_left(tools, plan, max(ended, tools._now()))
    if not windows:
        return ""
    picks: List[str] = []
    if tools.assistant is not None:
        try:
            cands = tools.assistant.proposals.candidates(plan)
        except Exception:
            cands = []
        for c in cands:
            if _fits(plan, c["minutes"], windows, outdoors(c["title"], tools)):
                picks.append(f"{c['title']} ({format_duration(c['minutes'])})")
            if len(picks) >= MAX_OFFERED:
                break
    if db is not None and once:
        db.set_sync_meta(key, tools._now().isoformat())
    count = (f"{light} daylight thirt{'y' if light == 1 else 'ies'} and " if light else "no daylight left, and ") \
        + f"{dark} dark thirt{'y' if dark == 1 else 'ies'} free before bed."
    if not picks:
        return f"After work: {count}"
    return f"After work: {count} A few that fit:\n" + "\n".join(picks)


__all__ = ["offer", "thirties_left", "MAX_OFFERED"]
