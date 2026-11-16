"""A nested list item named with where it sits: "Thank-you notes › Rivera family".

A list note can hold headings and lines indented under other lines. An item deep in such a list means little
by its own line ("Rivera family", "address missing"), so wherever Thirties names one to the user (the schedule,
what's next, check-ins and start notices, reminders, the morning's list, the plans for a week, month or year,
Before today, and the item lists the model reads), it's named with its parents: its heading or parent line, then
the item, at most two levels of parents (the nearest ones).

Display only. The item's stored title, its plan entry and its line in the list never change, and anything that
looks an item up (ticking it done, moving it) still goes by its id or its own line; a label said back
("Invitations › Lee household › address missing") finds the same item (``resolve``).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Sequence

SEP = " › "
MAX_PARENTS = 2
# Lists whose headings aren't context for an item: a routine's steps are named by the routine already, and the
# Today note's headings sort the day ("If there's time"), they don't say what an item is about.
NO_HEADING_ROLES = ("today", "routine")
NO_PATH_ROLES = ("routine",)


def _short(text: Any) -> str:
    from thirties_core.titles import title_of
    return title_of(str(text or "").strip())


def _by_id(tasks: Optional[Iterable[Any]]) -> Dict[str, Any]:
    if isinstance(tasks, dict):
        return tasks
    return {t.id: t for t in (tasks or []) if getattr(t, "id", None)}


def parents(task: Any, tasks: Optional[Iterable[Any]]) -> List[str]:
    """The item's heading and the lines it's indented under, top first, the nearest MAX_PARENTS of them; [] for an
    item with neither."""
    if task is None or getattr(task, "list_role", "") in NO_PATH_ROLES:
        return []
    by_id = _by_id(tasks)
    chain: List[str] = []
    seen = {task.id}
    cur = task
    while getattr(cur, "parent_task_id", None) and cur.parent_task_id in by_id and cur.parent_task_id not in seen:
        cur = by_id[cur.parent_task_id]
        seen.add(cur.id)
        chain.insert(0, _short(cur.title))
    section = str(getattr(cur, "section", "") or getattr(task, "section", "") or "").strip()
    if section and getattr(task, "list_role", "") not in NO_HEADING_ROLES:
        chain.insert(0, _short(section))
    chain = [c for c in chain if c]
    return chain[-MAX_PARENTS:]


def label(task: Any, tasks: Optional[Iterable[Any]], leaf: Optional[str] = None) -> str:
    """"Heading › Parent › Item" for a nested item; the item's short title (or ``leaf``) for any other."""
    own = str(leaf).strip() if leaf else _short(getattr(task, "title", ""))
    if task is None or SEP.strip() in own:
        return own
    return SEP.join(parents(task, tasks) + [own])


def _stands_for(title: str, task: Any) -> bool:
    """A plan entry's title is this item's own words (as written, in brief, or as "Item: piece")."""
    said = title.strip().lower()
    if not said:
        return False
    own = str(task.title or "").strip().lower()
    short = _short(task.title).lower()
    return said in (own, short) or said.endswith(": " + own) or said.endswith(": " + short)


def span_label(span: Any, tasks: Optional[Iterable[Any]]) -> str:
    """How a plan entry is named to the user: with its item's parents when it stands for a nested list item, else
    its own title, unchanged."""
    title = str(getattr(span, "title", "") or "")
    task = _by_id(tasks).get(getattr(span, "task_id", None) or "")
    if task is None or SEP.strip() in title or not _stands_for(title, task):
        return title
    up = parents(task, tasks)
    if not up:
        return title
    return SEP.join(up + [_short(task.title)])


def labelled(spans: Sequence[Any], tasks: Optional[Iterable[Any]]) -> List[Any]:
    """Copies of ``spans`` named for the user (span_label); a span that needs no label is the span itself. For
    rendering only: never stored."""
    by_id = _by_id(tasks)
    if not by_id:
        return list(spans)
    out = []
    for s in spans:
        name = span_label(s, by_id)
        if name == s.title:
            out.append(s)
            continue
        named = copy.copy(s)  # a shallow copy: the same item, by another name, for this message only
        named.title = name
        out.append(named)
    return out


def labelled_plan(plan: Any, tasks: Optional[Iterable[Any]]) -> Any:
    """A copy of the day's plan with nested items named for the user, for rendering a message; the plan itself
    when nothing needs a label."""
    if plan is None:
        return plan
    spans = labelled(plan.spans, tasks)
    if all(a is b for a, b in zip(spans, plan.spans)):
        return plan
    named = copy.copy(plan)
    named.spans = spans
    return named


def named(tasks: Optional[Iterable[Any]], title: Any, task_id: Any = "", limit: int = 80) -> str:
    """An item known by its words (and its id, when there is one) as the user reads it: a nested list item with
    its parents, else its short title. Without an id, a title only one item has."""
    from thirties_core.titles import title_of
    by_id = _by_id(tasks)
    task = by_id.get(str(task_id or ""))
    if task is None and title:
        want = str(title).strip().lower()
        same = [t for t in by_id.values() if str(t.title or "").strip().lower() == want]
        task = same[0] if len(same) == 1 else None
    if task is not None and parents(task, by_id):
        return label(task, by_id)
    return title_of(str(title or "").strip(), limit)


def tasks_of(tools: Any) -> List[Any]:
    """The list items a PlanTools (or an AssistantTools) knows about."""
    for holder in (getattr(tools, "services", None), getattr(getattr(tools, "assistant", None), "s", None),
                   getattr(tools, "s", None)):
        found = getattr(holder, "tasks", None)
        if found:
            return list(found)
    return []


def leaf(text: Any) -> str:
    """The item's own words in a label said back ("Invitations › Lee household › address missing" → "address
    missing"); the text itself when it isn't one."""
    raw = str(text or "")
    return raw.rsplit(SEP.strip(), 1)[-1].strip() if SEP.strip() in raw else raw.strip()


def resolve(ref: Any, tasks: Optional[Iterable[Any]]) -> Optional[Any]:
    """The one item a label names (its parents and its words, case aside); None when it isn't a label or doesn't
    name exactly one."""
    raw = str(ref or "").strip()
    if SEP.strip() not in raw:
        return None
    by_id = _by_id(tasks)
    want = " ".join(raw.lower().split())
    found = [t for t in by_id.values() if " ".join(label(t, by_id).lower().split()) == want]
    if len(found) > 1:  # a Today copy and its home item: the home item
        found = [t for t in found if getattr(t, "list_role", "") != "today"] or found
    return found[0] if len(found) == 1 else None


__all__ = ["SEP", "MAX_PARENTS", "parents", "label", "span_label", "labelled", "labelled_plan", "named", "tasks_of",
           "leaf", "resolve"]
