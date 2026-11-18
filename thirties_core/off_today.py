"""Taking something off today, when the user asks: off the schedule and off today's list, never ticked as done.

Unscheduling an item used to leave its Today line behind, which then reappeared untimed, and no tool
could remove that line: list tools work on home lists and schedule tools on spans. A request to
clear an item from the list was also read as the item being finished. Now:

- a clear ask is the user asking for an item to go: to take it off, drop, remove, clear, cancel or get rid of it,
  or saying it isn't happening. It is no done, so a done for it is refused (clearing isn't finishing);
- an item taken off on such words leaves today's list too. An item that has a home list keeps it there, untouched
  (a drop, as leftover_action means it: only its copy on Today goes); an item that lives only on Today is
  filed in the month's Dropped note, the record of what was let go, where an undrop can bring it back;
- an item on today's list with no time can be taken off the same way (remove_event by its title).

A move said with such words (dropping it to a later time instead) isn't a clear ask; neither is a title that is
itself the verb (an item whose own name starts with clear or drop).

Taking something off today isn't dropping it: without a drop word (write_guard.asks_to_drop) the item stays to do,
on its own list or, when Today is its only place, in Before today's pool. And when the user says it happened as
well ("the rent's paid, take it off"), taking it off is the done: it is ticked done, never filed as dropped, and
its line is never taken away before the tick (done_instead).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# the user asking for something to go
CLEAR_ASK_RE = re.compile(
    r"\b(?:remove|delete|drop|clear|scrap|ditch|nix|cancel|get rid of|take\b[^.!?\n]{0,40}?\boff|"
    r"(?:isn'?t|is not|not|won'?t be|aren'?t|are not) happening)\b", re.IGNORECASE)
# a clear ask that is really a move or a swap
_MOVE_RE = re.compile(r"\b(?:instead|rather|replac\w*|move|push|shift|to (?:\d|noon|later|tomorrow|after|before))\b",
                      re.IGNORECASE)
# a word that says it's finished, beside a clear ask ("done, clear it off")
_DONE_RE = re.compile(r"\b(?:done|finished|did it|did that|completed|got (?:it|that) done|ticked)\b", re.IGNORECASE)
_VERB_STEM = {"remove": "remov", "delete": "delet", "drop": "drop", "clear": "clear", "scrap": "scrap",
              "ditch": "ditch", "nix": "nix", "cancel": "cancel"}


def asks_to_clear(message: Optional[str], title: str = "") -> bool:
    """The message asks for something to go (not to move, and not that it's finished). A verb that is part of the
    item's own name names the item, so it's no ask by itself."""
    if not message:
        return False
    from thirties_core.item_facts import name_words
    mine = name_words(title) if title else set()
    for m in CLEAR_ASK_RE.finditer(message):
        verb = m.group(0).split()[0].lower()
        stem = _VERB_STEM.get(verb)
        if stem and any(w.startswith(stem) for w in mine):
            continue
        if not _MOVE_RE.search(_clause(message, m.start(), m.end())):
            return True
    return False


_CLAUSE_END_RE = re.compile(r"[.!?;,\n]|\b(?:and|but|also|then)\b", re.IGNORECASE)


def _clause(message: str, start: int, end: int) -> str:
    """The part of the message a match sits in: between the punctuation or joining words around it."""
    left = [m.end() for m in _CLAUSE_END_RE.finditer(message, 0, start)]
    right = _CLAUSE_END_RE.search(message, end)
    return message[left[-1] if left else 0:right.start() if right else len(message)]


def clear_not_done(pt: "PlanTools", title: str) -> Optional[str]:
    """Why a done can't go ahead because the user asked for the item to be cleared, not said it's finished; None
    when it can."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None
    if not asks_to_clear(message, title) or _DONE_RE.search(message):
        return None
    from thirties_core.write_guard import says_done  # "paid", "sent": it happened, said with the take-off
    if says_done(message, title, _other_titles(pt, title), strict=True):
        return None
    return (f"The user asked to take {title} off, not that it's finished, so it isn't marked done (clearing isn't "
            "finishing). Take it off with remove_event; if they finished it, they'll say so.")


def _assistant(pt: "PlanTools") -> Any:
    return getattr(pt, "assistant", None)


def _other_titles(pt: "PlanTools", title: str) -> List[str]:
    """The day's other items: the plan's and the lines on today's list."""
    titles = []
    for s in pt.day_plan.spans:
        try:
            titles.append(pt._title(s))
        except Exception:
            titles.append(s.title)
    a = _assistant(pt)
    if a is not None:
        try:
            from thirties_core.today import note_items
            titles += [t for t, _ in note_items(a._today_note())]
        except Exception:
            pass
    low = title.strip().lower()
    return list(dict.fromkeys(t for t in titles if t and t.strip().lower() != low))


def said_done_too(pt: "PlanTools", title: str) -> bool:
    """The user asks to take the item off and says it happened ("the rent's paid, take it off"), with no drop word:
    the take-off is a done."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return False
    from thirties_core.write_guard import asks_to_drop, says_done
    return not asks_to_drop(message) and says_done(message, title, _other_titles(pt, title), strict=True)


def done_instead(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """remove_event for an item the user says happened: it is marked done instead (ticked in Joplin, filed as done,
    never dropped); nothing is taken off before the tick. A refused or failed done raises, so nothing else is
    written about the item. None when the user didn't say it happened."""
    a = _assistant(pt)
    if a is None or span.source == "calendar" or span.is_envelope or not said_done_too(pt, pt._title(span)):
        return None
    out = a.log_outcome(event=span.id, status="done")
    return f"Marked {pt._title(span)} done instead of taking it off (the user said it happened): {out}"


def today_line(pt: "PlanTools", title: str, task_id: Optional[str] = None) -> Optional[str]:
    """The item's open line on the Today note, as written there; None when it has none (or there's no Today)."""
    a = _assistant(pt)
    if a is None or getattr(a, "_db", None) is None or not pt.is_current(pt.day_plan):
        return None
    from thirties_core.today import TodayLinks, note_items
    try:
        note = a._today_note()
    except Exception:
        return None
    if note is None:
        return None
    open_lines = [t for t, ticked in note_items(note) if not ticked]
    wanted = [title.strip().lower()]
    if task_id:
        linked = TodayLinks(a._db).today_title_of(task_id)
        if linked:
            wanted.insert(0, linked.strip().lower())
    for w in wanted:
        line = next((t for t in open_lines if t.strip().lower() == w), None)
        if line is not None:
            return line
    return None


def take_off_list(pt: "PlanTools", line: str) -> str:
    """Take an item's line off today's list: its copy only, when it has a home list (which keeps it, untouched);
    filed in the month's Dropped note when Today is its only place. What happened, in a few words ("" if nothing)."""
    a = _assistant(pt)
    if a is None:
        return ""
    from thirties_core.done_log import FILED_ROLES
    from thirties_core.today import TodayLinks
    has_home = TodayLinks(a._db).home_of(line) is not None or any(
        x.title.strip().lower() == line.strip().lower() and x.list_role in FILED_ROLES for x in a.s.tasks)
    try:
        if has_home:
            a._drop_leftovers([{"title": line}])
        else:
            a._archive_in_joplin([{"title": line}])
    except Exception:
        return ""
    return "off today's list" if today_line(pt, line) is None else ""


def after_removal(pt: "PlanTools", span: "EventSpan") -> str:
    """Once an item is off the schedule on the user's clear ask: off today's list too. A note for the tool's
    answer ("" when there's nothing to add)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None or span.source == "calendar" or span.is_envelope:
        return ""
    title = pt._title(span)
    from thirties_core.leave_today import asks_to_skip  # "skip it today" takes it off as well (leave_today.py)
    if not asks_to_clear(message, title) and not asks_to_skip(message):
        return ""
    if any(s.id != span.id and (s.title == span.title or (span.task_id and s.task_id == span.task_id))
           for s in pt.day_plan.spans):
        return ""  # another copy is still planned today: its line stays
    line = today_line(pt, span.title, span.task_id)
    if line is None:
        return ""
    from thirties_core import due_today  # due today: off the list only on a clear ask, said (due_today.py)
    held = due_today.held_due(pt, line, span.task_id)  # overdue too
    if held is not None:
        if not due_today.clear_ask(pt, line, due_today.others_on(pt, line)):
            from thirties_core.today_times import retime_held  # held on today: a new block, never untimed
            return retime_held(_assistant(pt), line, span.task_id, span.end_dt, held) or \
                due_today.list_stays_note(line, held, pt.today())
        done = _off_list(pt, line, title)
        return f" (also {done}; {due_today.moved_line(line, None, pt.today(), held)})" if done else ""
    done = _off_list(pt, line, title)
    return f" (also {done})" if done else ""


def _off_list(pt: "PlanTools", line: str, title: str) -> str:
    """Off today's list as the user put it: dropped only on a drop word (take_off_list; write_guard.asks_to_drop),
    else just not today (leave_today.skip_line: an item that lives only on Today waits for a day later this week).
    "Take it off" is about today: the item stays to do."""
    from thirties_core.leave_today import skip_line
    from thirties_core.write_guard import asks_to_drop
    message = getattr(pt, "user_message", None)
    if message is not None and not asks_to_drop(message) and _assistant(pt) is not None:
        return skip_line(_assistant(pt), line)
    return take_off_list(pt, line)


def remove_listed(pt: "PlanTools", event: Any) -> Optional[str]:
    """remove_event for an item on today's list with no time: off the list, on the user's clear ask for it. None
    when the event isn't such an item (the caller says it isn't on the schedule)."""
    from thirties_core.plan_tools import ToolError
    name = str(event or "").strip()
    a = _assistant(pt)
    if not name or a is None:
        return None
    task = next((t for t in a.s.tasks if t.id == name), None)
    line = today_line(pt, task.title if task is not None else name, task.id if task is not None else None)
    if line is None:
        return None
    if said_done_too(pt, line):  # it happened: ticked done on the list, never dropped
        from thirties_core.if_time import act
        return act(a, line, "done")
    if not getattr(pt, "by_hand", False) and getattr(pt, "user_message", None) is not None:
        from thirties_core.done_reference import check
        held, _ = check(pt, line, [task.id] if task is not None else [])
        from thirties_core.leave_today import asks_to_skip  # "skip it today" too (leave_today.py)
        if held or not (asks_to_clear(pt.user_message, line) or asks_to_skip(pt.user_message)):
            raise ToolError(f"{line} is on today's list with no time. It comes off the list only when the user "
                            "asks for that; ask them if it isn't clear.")
    from thirties_core import due_today  # due today: off the list only on a clear ask, said (due_today.py)
    deadline = ""
    held = due_today.held_due(pt, line, task.id if task is not None else None)  # overdue too
    if held is not None:
        if not due_today.clear_ask(pt, line, due_today.others_on(pt, line)):
            raise ToolError(due_today.stays_line(line, held, pt.today()))
        deadline = " " + due_today.moved_line(line, None, pt.today(), held)
    done = _off_list(pt, line, line)
    if not done:
        raise ToolError(f"{line} couldn't be taken off today's list just now.")
    return f"Took {line} off today's list (it had no time on the schedule).{deadline}"


# a reply saying something is off today's list ("moved the towels off today's list", "cleared it from the list")
LIST_GONE_RE = re.compile(r"\b(?:(?:took|taken|moved|removed|deleted|cleared|dropped|gotten rid|got rid)\b[^.!?\n]{0,60}?"
                          r"\b(?:off|from)\b[^.!?\n]{0,20}?\blist|(?:is|are|'s|now)\s+(?:now\s+)?off\s+(?:the|your|"
                          r"today'?s)\s+list|no longer on (?:the|your|today'?s) list)\b", re.IGNORECASE)


def listed_still(pt: "PlanTools", sentence: str) -> List[str]:
    """Items on today's list with no time that a sentence names: what a claim of taking them off is checked
    against."""
    a = _assistant(pt)
    if a is None:
        return []
    try:
        from thirties_core.day_lists import pencilled_in
        loose = pencilled_in(a)
    except Exception:
        return []
    from thirties_core.done_reference import _named
    titles = [s.title for s in pt.day_plan.spans] + loose
    return [t for t in loose if _named(sentence, t, [o for o in titles if o != t])]
