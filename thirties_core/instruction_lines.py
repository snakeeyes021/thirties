"""A line that says what to do with the items under it is never on a plan: only the items under it are.

A list can hold a line such as "Decide between these medallions" with items under it. It isn't something to do;
the items under it are (list_reading.says_what_to_do). Like any line with open items under it, it's never among
the day's options, the week's or month's, or what the morning proposes. A tool call that would put it on a plan
by itself is held back here, whoever asked, with the items under it named so one of those can go on instead. And
once a plan holds it, it simply ends when it comes off, instead of waiting for another day (KindTools.carries).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PLACING = {"add_event", "add_to_today", "propose_day", "pin_to_period"}


def held(pt, name: str, args: Dict[str, Any]) -> Optional[str]:
    """A NOT_DONE sentence when ``name`` would put such a line on a plan, else None. Only for the outermost call
    (a tool's own steps were checked when it was called)."""
    if name not in PLACING or pt.assistant is None or getattr(pt, "_home_plan", None) is not None:
        return None
    from thirties_core.backlog_tickets import placed_refs
    from thirties_core.list_reading import ENTRY, name_of, says_what_to_do
    tasks = list(pt.assistant.s.tasks)
    lines: List[Any] = []
    for ref in placed_refs(name, args or {}):
        ref = (ref or "").strip()
        try:
            task = pt.assistant._exact_task(ref) if ref else None
        except Exception:
            task = None
        if task is not None and says_what_to_do(task) and task not in lines:
            lines.append(task)
    if not lines:
        return None
    from thirties_core.plan_tools import NOT_DONE
    out = []
    for line in lines:
        under = [name_of(t) for t in tasks if t.parent_task_id == line.id and (t.line_kind or ENTRY) == ENTRY]
        out.append(f"{name_of(line)} only says what to do with the lines under it, so it never goes on a plan itself"
                   + (f"; the items under it: {'; '.join(under)}" if under else ""))
    return f"{NOT_DONE}: " + ". ".join(out) + ". Plan one of the items under it instead, as the user wants."


__all__ = ["PLACING", "held"]
