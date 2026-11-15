"""Progress reports say what was pushed back to the backlog, as well as what got done.

What counts, from the records Thirties already keeps (nothing new is stored):

- a list item taken off the plan without being done (removed from a day it was planned on): it's back on its
  list, unplanned (the plan's change log, plan_log.py, and the removed item itself);
- a leftover kept for later instead of today (this week, by its deadline, someday), or dropped (it stays on its
  list, it just stops coming back): the leftovers pool (carry.py).

The reports are review_day (a day) and week_overview (a week): each ends with "Pushed back to the backlog:" and
one line per item, or nothing when nothing was. extras.py wraps both tools, so their own code is untouched.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.carry import DROPPED, POOL_KEY, SAID, TODAY, _load

HEADING = "Pushed back to the backlog:"


def _removed_list_items(db, first: date, last: date) -> List[Tuple[str, str]]:
    """(title, how) for list items taken off a plan between ``first`` and ``last`` without being done."""
    from thirties_core import plan_log
    out: List[Tuple[str, str]] = []
    with db._get_connection() as conn:
        plan_log._ensure(db, conn)
        rows = conn.execute("SELECT span_id, title, plan_date, before FROM plan_changes WHERE change = 'removed' "
                            "AND plan_date >= ? AND plan_date <= ? ORDER BY id",
                            (first.isoformat(), last.isoformat())).fetchall()
        for r in rows:
            data_row = conn.execute("SELECT data FROM spans WHERE id = ?", (r["span_id"],)).fetchone()
            try:
                data = json.loads(data_row["data"]) if data_row else {}
            except ValueError:
                data = {}
            if not data.get("task_id") or data.get("status") == "done" or data.get("source") == "calendar":
                continue
            again = conn.execute(  # put back on the same day (a redraw gives it a new id): not pushed back
                "SELECT 1 FROM spans WHERE deleted = 0 AND id != ? AND substr(start_utc, 1, 10) BETWEEN ? AND ? "
                "AND json_extract(data, '$.task_id') = ?",
                (r["span_id"], r["plan_date"], (date.fromisoformat(r["plan_date"]) + timedelta(days=1)).isoformat(),
                 data["task_id"])).fetchone()
            if again:
                continue
            out.append((r["title"] or data.get("title", ""), "taken off the plan, still on its list"))
    return out


def _pool_items(db, first: date, last: date) -> List[Tuple[str, str]]:
    out = []
    for rec in _load(db, POOL_KEY).values():
        horizon = rec.get("horizon")
        if horizon == DROPPED:
            when = rec.get("dropped_on") or rec.get("since", "")
            how = "dropped: it stays on its list, it just stops coming back"
        elif horizon in (None, "", TODAY, "gone"):
            continue
        else:
            when = rec.get("since", "")
            how = "kept for " + (rec.get("when") or SAID.get(horizon, horizon))
        if first.isoformat() <= when[:10] <= last.isoformat():
            out.append((rec.get("title", ""), how))
    return out


def pushed_back(db, first: date, last: Optional[date] = None) -> List[Tuple[str, str]]:
    """(title, what happened) for everything pushed back to the backlog from ``first`` to ``last``."""
    if db is None:
        return []
    last = last or first
    seen, out = set(), []
    for title, how in _removed_list_items(db, first, last) + _pool_items(db, first, last):
        if title and title.strip().lower() not in seen:
            seen.add(title.strip().lower())
            out.append((title, how))
    return out


def section(db, first: date, last: Optional[date] = None) -> str:
    items = pushed_back(db, first, last)
    if not items:
        return ""
    return HEADING + "\n" + "\n".join(f"{t} ({how})" for t, how in items)


def with_pushed_back(text: str, db, first: date, last: Optional[date] = None) -> str:
    part = section(db, first, last)
    return f"{text}\n\n{part}" if part else text


# --- the wrapped reports (extras.py) ---

def review_day(assistant, **kwargs: Any) -> str:
    out = assistant.review_day(**kwargs)
    return with_pushed_back(out, assistant._db, assistant.pt.day_plan.target_date)


def week_overview(assistant, start_day: Any = None, days: Any = 7, **kwargs: Any) -> str:
    from thirties_core.clock import parse_day
    out = assistant.week_overview(start_day=start_day, days=days, **kwargs)
    if out.startswith("NOT_DONE"):
        return out
    today = assistant._today()
    first = parse_day(start_day, today) if start_day else today
    try:
        count = max(1, min(14, int(str(days))))
    except ValueError:
        count = 7
    last = first + timedelta(days=count - 1)
    if first >= today:  # a week from today looks ahead: what was pushed back is from the week just gone
        first, last = today - timedelta(days=count - 1), today
    return with_pushed_back(out, assistant._db, first, last)


__all__ = ["HEADING", "pushed_back", "section", "with_pushed_back", "review_day", "week_overview"]
