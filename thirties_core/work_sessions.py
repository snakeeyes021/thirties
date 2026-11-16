"""Working on something isn't finishing it.

A done reported with its start and end ("I was on the shelf build from 3 to 6:30") on an item that goes on (a
project, an item with pieces still open, something worked on over time), or on anything the user says they worked
on, spent time on or did some of, records a session: the time is credited, the item stays open on its list, and
nothing is ticked. Only the user's own words that it's finished ("finished", "done with", "completed") make it
done. The model's choice of status or of finished_for_good doesn't change that: which it is, is in the user's words.

The words are read for the item only: the parts of the message that name it (and what follows them until another
item is named), so "worked on the shelves and finished the laundry" finishes the laundry, not the shelves.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

# time on it, not the end of it
WORKED_RE = re.compile(
    r"\b(?:work(?:ed|ing)?\s+(?:on|at)|spen[dt]\s+(?:(?:some|a\s+bit\s+of|a\s+little|an?\s+\w+|\w+\s+hours?|"
    r"the\s+\w+)\s+)?(?:time\s+)?on|did\s+(?:some|a\s+bit|a\s+little|part|a\s+chunk)\s+of|"
    r"put\s+(?:some\s+|a\s+few\s+|\w+\s+)?(?:time|hours?|work)\s+(?:in(?:to)?|on)|chipp(?:ed|ing)\s+away\s+at|"
    r"made\s+(?:some\s+|good\s+)?progress\s+on|kept\s+at|plugg(?:ed|ing)\s+away\s+at|a\s+session\s+on)\b",
    re.IGNORECASE)
# the end of it
FINISHED_RE = re.compile(r"\b(?:finish(?:ed|es)?|done\s+with|complet(?:ed|es|e)|wrapped\s+up|all\s+done|"
                         r"got\s+(?:it|that|them)\s+done|(?:is|it'?s|that'?s)\s+done)\b", re.IGNORECASE)
# "finished at 6:30", "finished for the day": when the session stopped, not that the thing is finished
_STOPPED_RE = re.compile(r"\b(?:finish(?:ed)?|wrapped\s+up|done)\s+(?:at|around|about|by|for\s+(?:the\s+day|today|"
                         r"now|tonight|the\s+night|the\s+evening))\b|\bdone\s+for\s+(?:the\s+day|today|now|tonight)\b",
                         re.IGNORECASE)
# "haven't finished", "not done with it", "not quite complete"
_NEGATED_RE = re.compile(r"(?:\b(?:not|never|yet\s+to|far\s+from|nowhere\s+near)|n['’]?t)\s+(?:quite\s+|yet\s+|"
                         r"really\s+|been\s+|got\s+)?(?:finish|done|complet|wrapped)\w*", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+|,\s*|\s+(?:and|but|then|plus|also)\s+", re.IGNORECASE)
PROJECT_WORD_RE = re.compile(r"\bproject\b", re.IGNORECASE)


def _clauses_about(message: str, title: str, others: List[str]) -> List[str]:
    """The parts of the message about this item: each clause that names it, and the clauses after it that name
    nothing ("... and finished it"), until another item is named. The whole message when none names it."""
    from thirties_core.started import _names
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(message or "") if p and p.strip()]
    out, on = [], False
    for part in parts:
        if _names(part, title, others):
            on = True
        elif any(_names(part, o, [title] + [x for x in others if x != o]) for o in others):
            on = False
        if on:
            out.append(part)
    return out or [message or ""]


def _finished(text: str) -> bool:
    cleaned = _STOPPED_RE.sub(" ", _NEGATED_RE.sub(" ", text))
    return bool(FINISHED_RE.search(cleaned))


def reading(message: Optional[str], title: str, others: Iterable[str] = ()) -> Optional[str]:
    """What the user's words say about the item: "finished", "session" (worked on it, spent time on it), or None
    when they say neither."""
    if not message or not title:
        return None
    others = [o for o in others if o and o.strip().lower() != title.strip().lower()]
    about = " . ".join(_clauses_about(message, title, others))
    if _finished(about):
        return "finished"
    if WORKED_RE.search(about):
        return "session"
    return None


def goes_on(assistant, task: Any, span: Any) -> bool:
    """An item that goes on past one sitting: an ongoing kind, an item with pieces still open, a part of a project,
    or one called a project."""
    from thirties_core.kinds import ONGOING
    title = getattr(span, "title", None) or getattr(task, "title", "") or ""
    try:
        if assistant.kind_of(span if span is not None else task) == ONGOING:
            return True
    except Exception:
        pass
    if PROJECT_WORD_RE.search(title) or (task is not None and PROJECT_WORD_RE.search(task.title or "")):
        return True
    if task is not None:
        from thirties_core.project_pieces import open_children
        if open_children(assistant.s.tasks, task):
            return True
    try:
        from thirties_core.project_pieces import part_of
        return part_of(assistant, title) is not None
    except Exception:
        return False


def is_session(assistant, span: Any, task: Any, said_start_and_end: bool) -> bool:
    """A done that's a session on the item, by the user's words this turn (never when nobody is talking: a tick in
    the app is a tick)."""
    message = getattr(assistant.pt, "user_message", None)
    if message is None or (span is None and task is None):
        return False
    title = getattr(span, "title", None) or getattr(task, "title", "")
    others = [s.title for s in assistant.pt.day_plan.spans if s.title and (span is None or s.id != span.id)]
    said = reading(message, title, others)
    if said == "finished":
        return False
    if said == "session":
        return True
    if task is None and span is not None:  # named by its place on the plan: the list item it stands for
        try:
            from thirties_core.project_pieces import task_of
            task = task_of(assistant, span)
        except Exception:
            task = None
    return said_start_and_end and goes_on(assistant, task, span)


__all__ = ["is_session", "reading", "goes_on", "WORKED_RE", "FINISHED_RE"]
