"""A rolling week of thirties, pencilled in: "always have like... a week's worth of thirties planned out, but
things would only fully solidify on the day".

Each morning (after the day's own proposal), and whenever the user asks about the days ahead, Python drafts the next
six days the way it drafts a day (day_proposal.default_items): what's pinned to each day in the week plan, what's
due, what's pressing, each at a time in that day's free thirties, every item on one day only (the earliest that has
room). The draft is kept apart from the plan, so it takes no time, sends no reminders and never nags: it's
pencilled in. On the day itself it firms up: that morning's proposal puts the drafted items first, at their drafted
times while those are still free, and they go on the plan for real. Everything after that day is drafted afresh.

The draft is a shared setting, so every device shows the same week.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from thirties_core.clock import format_clock

logger = logging.getLogger(__name__)

KEY = "week_draft"
DAYS_AHEAD = 6

WEEK_DRAFT_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "week_ahead",
    "description": "The coming days pencilled in, in thirties: for each, the free daylight and dark thirties and what "
                   "Python has pencilled in for it (what's pinned to the day, due or pressing), at times. Nothing in "
                   "it is on the plan: each day firms up on its morning. Read-only (it redrafts the days ahead).",
    "parameters": {},
    "required": [],
}]


def _load(db) -> Dict[str, Any]:
    try:
        data = json.loads(db.get_shared_setting(KEY) or "{}") if db is not None else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def draft_for(db, day: date) -> List[Dict[str, Any]]:
    """What's pencilled in for a day: [{"title", "task_id", "start" (ISO), "minutes"}], earliest first."""
    return list(_load(db).get("days", {}).get(day.isoformat(), []))


def drafted_starts(db, day: date) -> Dict[str, datetime]:
    return {i["title"].strip().lower(): datetime.fromisoformat(i["start"]) for i in draft_for(db, day)}


def refresh(tools, days: int = DAYS_AHEAD) -> Dict[str, List[Dict[str, Any]]]:
    """Draft the days after today afresh (today is the morning proposal's) and keep it. Returns it by ISO date."""
    db = tools._state_db
    if db is None or tools.assistant is None or tools.scheduler is None:
        return {}
    from thirties_core.models import SpanStatus
    today = tools.today()
    dp = tools.assistant.proposals
    taken = {s.title.strip().lower() for s in tools.plan_for(today).spans if s.status != SpanStatus.SKIPPED}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for k in range(1, days + 1):
        d = today + timedelta(days=k)
        try:
            plan = tools.plan_for(d)
            items = dp.default_items(plan, skip=taken, ahead=True)
        except Exception as e:  # one day that can't be drafted never stops the rest
            logger.warning("Couldn't pencil in %s: %s", d, e)
            continue
        taken |= {i["title"].strip().lower() for i in items}
        out[d.isoformat()] = items
    db.set_shared_setting(KEY, json.dumps({"made": today.isoformat(), "days": out}),
                          datetime.now(timezone.utc).isoformat())
    return out


def week_ahead(tools, **_: Any) -> str:
    """The days ahead, pencilled in, one block per day in plain lines."""
    from thirties_core.after_work import thirties_left
    from thirties_core.assistant_tools import ToolError
    if tools._state_db is None or tools.assistant is None:
        raise ToolError("The days ahead can't be drafted here.")
    drafted = refresh(tools)
    blocks = []
    for iso, items in drafted.items():
        d = date.fromisoformat(iso)
        plan = tools.plan_for(d)
        light, dark, _ = thirties_left(tools, plan, plan.day_start)
        head = f"{d.strftime('%A %b %-d')}: {light} daylight and {dark} dark thirties free"
        lines = [f"{format_clock(datetime.fromisoformat(i['start']))} {i['title']}" for i in items]
        blocks.append("\n".join([head + (", pencilled in:" if lines else ", nothing pencilled in yet.")] + lines))
    if not blocks:
        return "Nothing to pencil in for the days ahead."
    return ("Pencilled in (not on the plan; each day firms up on its morning):\n\n" + "\n\n".join(blocks))


__all__ = ["WEEK_DRAFT_TOOL_SPECS", "draft_for", "drafted_starts", "refresh", "week_ahead", "DAYS_AHEAD"]
