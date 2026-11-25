"""A record after the fact is a done like any other.

"I cleaned the gutters from 3 to 4", recorded with happened=true, follows the rules every done follows
(assistant_tools.log_outcome):

- **Which list item**: its id, or its exact list wording, or (while the user is talking) the one open list item
  their words name (done_on_lists.py); several match: the model picks the likeliest and says so.
- **A done the user said** (said_for_it.done_refused): which list item it is for is the model's reading; it stops,
  and nothing is recorded, only when nothing the user said is a done or their words name the item as not done.
- **Working on it isn't finishing it** (work_sessions.py): a session on something that isn't finished stays open
  on its list, its time on the record as partly done; a session on an ongoing item ticks only today's copy.
- **Done is done everywhere**: a finished list item is ticked in Joplin like any other done; something on no list
  ticks its line on the Today note, if it has one.

The app's own controls and the background (nobody talking) are never held or matched here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional, Tuple

from thirties_core.models import SpanStatus

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools


def _talking(pt: "PlanTools") -> bool:
    return getattr(pt, "user_message", None) is not None and not getattr(pt, "by_hand", False)


def resolve(pt: "PlanTools", title: str, task_id: Optional[str]) -> Tuple[Optional[str], Any, SpanStatus]:
    """Before anything is stored: (the task id to link, the list item it is or None, done or partly done). Raises
    ToolError when the user's words give no done for the item, or name it as not done, or several items match."""
    a = getattr(pt, "assistant", None)
    if a is None:
        return task_id, None, SpanStatus.DONE
    from thirties_core.plan_tools import ToolError
    task = None
    if task_id:
        task = a._exact_task(str(task_id))
    else:
        same = a._exact_task(title)  # worded exactly as a list item: it's that item
        if same is not None and not same.id.startswith("media:"):
            task = same
        elif _talking(pt):
            from thirties_core.done_on_lists import on_the_lists
            task = on_the_lists(a, title)  # several: the model picks the likeliest and says so
    if task is not None and _talking(pt):
        from thirties_core.said_for_it import done_refused
        held = done_refused(pt, task.title)
        if held:
            raise ToolError(held)
    status = SpanStatus.DONE
    if _talking(pt):
        from thirties_core.kinds import ONGOING
        from thirties_core.work_sessions import is_session
        probe = None if task is not None else SimpleNamespace(title=title, id="")
        if is_session(a, probe, task, True) and a.kind_of(task if task is not None else title) != ONGOING:
            status = SpanStatus.PARTIAL  # the time is on the record; the item stays open, nothing is ticked
    return (task.id if task is not None else task_id), task, status


def settle_lists(pt: "PlanTools", span: Any, task: Any) -> str:
    """After the record is stored: the lists, as for any done. Returns what to add to the tool's answer."""
    a = getattr(pt, "assistant", None)
    if a is None:
        db = getattr(pt, "_state_db", None)
        if span.task_id and db is not None and span.status == SpanStatus.DONE:
            db.record_task_completed(span.task_id, pt.day_plan.target_date)
        return ""
    if span.status == SpanStatus.PARTIAL:
        return " (a session on it: it stays open on its list)"
    if task is None:
        if span.task_id and a._db:
            a._db.record_task_completed(span.task_id, pt.day_plan.target_date)
        else:
            a._tick_on_today(span.title)
        return ""
    from thirties_core.kinds import ONGOING
    if a.kind_of(task) == ONGOING:
        return " " + a._worked_on(task)  # a session: the item itself goes on
    return " " + a._complete_backlog_task(task, False)


__all__ = ["resolve", "settle_lists"]
