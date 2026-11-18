"""The user's work week follows what they say, not a guess.

Work goes on a day only when the user's week says so (week.py: their work days and hours, set in Preferences or
with set_week). With nothing said, no day gets a Work block: a guessed one put work on days the user was off and at
hours they don't keep, and they had to take it off or move it every morning.

A correction for one day changes that day only: "no work today" (set_days_off for one day, or Work taken off the
day), a later shift that day (that day's Work moved). Each is kept here for a few weeks. Said the same way on the
same weekday a week later, it's the user's week now: that weekday's usual work changes (no work on Thursdays; work
11:00 AM to 6:00 PM on Wednesdays), the coming days follow, and the reply says so. Saying the new pattern outright
("no work on Thursdays any more", "my shifts start at eleven from now on") goes through set_week as before.

A stretch of days off ("off all next week") is a vacation, not a pattern, and never counts.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

KEY = "work_corrections"  # in shared_settings: [{"date": iso, "work": null | [start_thirty, end_thirty]}]
KEEP = timedelta(days=42)

Hours = Optional[Tuple[int, int]]  # (first work thirty, last work thirty) as in config; None: no work


def corrections(db) -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        data = json.loads(db.get_shared_setting(KEY) or "[]")
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict) and r.get("date")]


def usual(gen, day: date) -> Hours:
    """The work the user's week gives this date (None: no work)."""
    from thirties_core.week import day_hours
    h = day_hours(gen, day)
    return (h.work_start % 48, h.work_end % 48) if h.works else None


def _hours_of(span: "EventSpan") -> Hours:
    from thirties_core.week import thirty_index
    return thirty_index(span.start_dt.time()), (thirty_index(span.end_dt.time(), round_up=True) - 1) % 48


def record(tools: "PlanTools", day: date, work: Hours) -> str:
    """Keep the user's correction of one day's work. Returns a sentence when it changed their usual week, else ""."""
    db, sched = getattr(tools, "_state_db", None), getattr(tools, "scheduler", None)
    if db is None or sched is None:
        return ""
    gen = sched.config.general
    keep = [r for r in corrections(db) if r["date"] != day.isoformat()
            and abs((date.fromisoformat(r["date"]) - day).days) <= KEEP.days]
    mine = {"date": day.isoformat(), "work": list(work) if work else None}
    db.set_shared_setting(KEY, json.dumps(sorted(keep + [mine], key=lambda r: r["date"])),
                          datetime.now(timezone.utc).isoformat())
    if work == usual(gen, day):
        return ""  # back to the usual: nothing to learn
    week_before = (day - timedelta(days=7)).isoformat()
    before = next((r for r in keep if r["date"] == week_before), None)
    if before is None or (tuple(before["work"]) if before["work"] else None) != work:
        return ""
    return _learn(tools, day, work)


def _learn(tools: "PlanTools", day: date, work: Hours) -> str:
    """That weekday's usual work becomes ``work``; the coming days follow (except what was changed by hand)."""
    from thirties_core.clock import format_clock
    from thirties_core.settings import Settings
    from thirties_core.week import DAYS, weekday_key, work_days
    gen = tools.scheduler.config.general
    key = weekday_key(day)
    days = [d for d in DAYS if d in work_days(gen)]
    changes: Dict[str, Any] = {}
    if work is None:
        changes["general.work_days"] = [d for d in days if d != key]
    else:
        if key not in days:
            changes["general.work_days"] = [d for d in DAYS if d in days or d == key]
        week = {k: dict(v) for k, v in (getattr(gen, "week", None) or {}).items()}
        for name, value in (("work_start_thirty", work[0]), ("work_end_thirty", work[1])):
            if value == int(getattr(gen, name)) % 48:
                week.get(key, {}).pop(name, None)  # the usual hours: only the day's differences are kept
            else:
                week.setdefault(key, {})[name] = value
        week = {k: v for k, v in week.items() if v}
        if week != (getattr(gen, "week", None) or {}):
            changes["general.week"] = week
    settings = Settings(tools._state_db)
    for name, value in changes.items():
        settings.set(name, value)
        setattr(gen, name.split(".", 1)[1], value)  # live, for everything holding this config
    assistant = getattr(tools, "assistant", None)
    if assistant is not None:
        try:
            assistant.routines._refresh_envelopes(kinds=("work",))
        except Exception:
            pass
    plural = day.strftime("%A") + "s"
    if work is None:
        return f" Two {plural} running without work: {plural} are now days off in the usual week."

    def clock(thirty: int) -> str:
        return format_clock(datetime.combine(day, datetime.min.time()) + timedelta(minutes=30 * thirty))
    return (f" Two {plural} running with work {clock(work[0])} – {clock(work[1] + 1)}: that's the usual work on "
            f"{plural} now.")


def after_work_edit(tools: "PlanTools", span: "EventSpan", removed: bool = False) -> str:
    """After the user moved or took off a day's Work: kept as that day's correction."""
    from thirties_core.models import SpanCategory
    from thirties_core.plan_log import outer_tool
    if span.category != SpanCategory.WORK:
        return ""
    if not getattr(tools, "by_hand", False) and outer_tool() not in ("update_event", "remove_event"):
        return ""  # Thirties' own changes (a late start recorded, a replan) aren't the user's week
    try:
        return record(tools, tools.day_plan.target_date, None if removed else _hours_of(span))
    except Exception:
        return ""


def after_day_off(tools: "PlanTools", first: date, last: date) -> str:
    """After set_days_off: a single day off on a work day is that day's correction; a stretch is a vacation."""
    gen = getattr(getattr(getattr(tools, "scheduler", None), "config", None), "general", None)
    if first != last or gen is None or usual(gen, first) is None:
        return ""
    return record(tools, first, None)


__all__ = ["after_day_off", "after_work_edit", "corrections", "record", "usual"]
