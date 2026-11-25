"""Taking something off today, when the user asks: off the schedule and off today's list, never ticked as done.

Unscheduling an item used to leave its Today line behind, which then reappeared untimed, and no tool
could remove that line: list tools work on home lists and schedule tools on spans. A request to
clear an item from the list was also read as the item being finished. Now:

- whatever takes an item off today's plan on the user's words (remove_event, clear_time, a new item put in its
  place) takes its line off today's list too, so it never lingers as "no time yet". What the user meant for each
  line is the model's reading, passed as ``today_list`` (``leave_list``): off (the default: off today, and it
  doesn't come back by itself), keep, later, tomorrow, or drop (for good). No wording check decides it; a
  question or a complaint can be the ask. The answer carries the facts back: what happened to the line, and
  whether the item is due today or overdue (a fact, never a hold);
- an item with a home list keeps it there, untouched: only its copy on Today goes. A record of it waiting from
  an earlier day stops offering it (Before today's archive, where it can be brought back), so it doesn't return
  tomorrow as a leftover. A routine's group or steps are never written here;
- dropped for good is filed in the month's Dropped note only on the user's drop word (write_guard.py); without
  one it is off today all the same;
- an item on today's list with no time can be taken off the same way (remove_event by its title);
- items a redraw can't fit wait for another day, so their lines leave today's list as well (``after_no_room``).

A clear ask (``asks_to_clear``) is the user asking for an item to go: to take it off, drop, remove, clear, cancel or
get rid of it, or saying it isn't happening. It is no done, so a done for it is refused (clearing isn't
finishing). And when the user says it happened as well ("the rent's paid, take it off"), taking it off is the done:
it is ticked done, never filed as dropped, and its line is never taken away before the tick (done_instead).
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


def after_removal(pt: "PlanTools", span: "EventSpan", today_list: Any = None) -> str:
    """Once an item is off today's plan on the user's words: its line on today's list goes too, or stays, as the
    model chose (``today_list``, read by ``list_choice``; off by default). Which item the user meant, and whether
    their words take it off the list too, is the model's reading: no wording check here. A note for the tool's
    answer, with the facts the model needs ("" when the item has no line on today's list)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None or span.source == "calendar" or span.is_envelope:
        return ""
    if any(s.id != span.id and (s.title == span.title or (span.task_id and s.task_id == span.task_id))
           for s in pt.day_plan.spans):
        return ""  # another copy is still planned today: its line stays
    line = today_line(pt, span.title, span.task_id)
    if line is None:
        return ""
    return leave_list(pt, line, span.task_id, list_choice(today_list))


def after_clear(pt: "PlanTools", gone: List["EventSpan"], today_list: Any = None) -> str:
    """clear_time took these items off today's plan: their lines on today's list follow, as for remove_event
    (``after_removal``). Notes for the tool's answer."""
    choice = list_choice(today_list)
    out = ""
    for span in gone:
        try:
            out += after_removal(pt, span, choice)
        except Exception as e:  # one item's list never costs the others theirs
            out += f" ({pt._title(span)} is still on today's list: {e})"
    return out


# --- an item's line on today's list when it comes off today: the model's call (today_list) ---
# off: taken off today's list, and it doesn't come back by itself (the default: the user took it off today);
# keep: it stays on today's list with no time; later: off, and back on a later day this week (or the day the user
# named); tomorrow: off, and on tomorrow's list; drop: off and dropped for good (filed as dropped only on the
# user's drop word, write_guard.py)
TODAY_LIST = ("off", "keep", "later", "tomorrow", "drop")
_LIST_SAID = {"": "off", "true": "off", "yes": "off", "remove": "off", "false": "keep", "no": "keep", "stay": "keep",
              "stays": "keep", "week": "later", "this week": "later", "later this week": "later",
              "another day": "later", "for good": "drop", "dropped": "drop"}


def list_choice(value: Any) -> str:
    """What the model asked for the item's line on today's list (TODAY_LIST); off when it said nothing."""
    from thirties_core.plan_tools import ToolError
    if isinstance(value, bool):
        return "off" if value else "keep"
    said = str(value if value is not None else "").strip().lower()
    said = _LIST_SAID.get(said, said)
    if said not in TODAY_LIST:
        raise ToolError("today_list must be off, keep, later, tomorrow or drop.")
    return said


def leave_list(pt: "PlanTools", line: str, task_id: Optional[str], choice: str, also: bool = True) -> str:
    """The item's line on today's list (its copy on the Today note), as the model chose. Its home list, project or
    routine is never written to. The facts go back in the answer (" X was also on today's list: taken off."), with
    its deadline when it's due today or overdue; the call stands either way. A write Joplin refused raises with the
    real reason."""
    from thirties_core.plan_tools import ToolError
    from thirties_core import due_today
    a = _assistant(pt)
    if a is None:
        return ""
    held = due_today.held_due(pt, line, task_id)  # due today or overdue: a fact for the model, never a hold
    due = f" {line} is {due_today.deadline_words(held, pt.today())}." if held is not None else ""
    head = f"{line} was also on today's list: taken off" if also else f"Took {line} off today's list"
    if choice == "keep":
        return f" {line} is still on today's list, with no time.{due}"
    if _of_a_routine(a, line):  # a routine's group or step comes and goes with the routine: today's simply ends
        return ""
    from thirties_core.leave_today import has_home, remove_copy_why
    homed = has_home(a, line)
    stays = " (it stays on its own list)" if homed else ""
    if choice == "drop":
        from thirties_core.write_guard import asks_to_drop
        said_drop = homed or getattr(pt, "by_hand", False) or asks_to_drop(getattr(pt, "user_message", None))
        if said_drop:
            done = take_off_list(pt, line)
            if done:
                filed = "" if homed else "; it's filed in this month's Dropped note"
                return f" {head} and dropped for good{filed}{stays}.{due}"
            if today_line(pt, line) is None:
                return f" {line} is off today's list.{due}"
        # only the user's drop word files it as dropped for good; it's off today all the same
        why = remove_copy_why(a, line)
        if why:
            raise ToolError(f"{line} is off today's plan but still on today's list: {why}.")
        _stop_rolling(a, line)
        unfiled = "the user's words have no drop word" if not said_drop else "Joplin didn't take the filing"
        return (f" {head}, and it won't come back by itself. It isn't filed as "
                f"dropped for good: {unfiled}.{due}")
    why = remove_copy_why(a, line)
    if why:
        raise ToolError(f"{line} is off today's plan but still on today's list: {why}.")
    task = _home_item(a, line, task_id) if homed else None
    if choice == "tomorrow":
        from datetime import timedelta
        from thirties_core.day_lists import AheadLists
        AheadLists(a._db).add(pt.today() + timedelta(days=1), line, task)
        return f" {head}; it's on tomorrow's list{stays}.{due}"
    if choice == "later":
        from thirties_core.not_today import minutes_for, set_aside, when_words, where_to
        day, named = where_to(pt, line, minutes_for(a, line, task))
        set_aside(a, line, task, day, named)
        return f" {head}; it waits for {when_words(pt, day, named)}{stays}.{due}"
    _stop_rolling(a, line)
    gone = "it stays on its own list" if homed else "it lived only on today's list, so it's gone (add it back if " \
                                                    "the user wants it)"
    return f" {head}, and it won't come back by itself; {gone}.{due}"


def _of_a_routine(a: Any, line: str) -> bool:
    """The line is a routine's group on Today, or one of its steps: never written here (it ends with the day)."""
    try:
        low = line.strip().lower()
        return low in {n["title"].strip().lower() for n in a._routines()} or a._routine_of(line) is not None
    except Exception:
        return False


def after_no_room(pt: "PlanTools", spans: List["EventSpan"]) -> List[str]:
    """Items a redraw took off today's plan for lack of room: they wait for a later day (the open pool), so their
    lines leave today's list too, never left behind as "no time yet". The lines taken off."""
    a = _assistant(pt)
    if a is None or not pt.is_current(pt.day_plan):
        return []
    from thirties_core.leave_today import remove_copy
    gone = []
    for span in spans:
        if span.source == "calendar" or span.is_envelope:
            continue
        line = today_line(pt, span.title, span.task_id)
        if line is None or _of_a_routine(a, line):
            continue
        try:
            if remove_copy(a, line):
                gone.append(line)
        except Exception:
            continue
    return gone


def _home_item(a: Any, line: str, task_id: Optional[str]) -> Any:
    from thirties_core.not_today import home_task
    try:
        return home_task(a, line, task_id)
    except Exception:
        return None


def _stop_rolling(a: Any, line: str) -> None:
    """Taken off today on the user's word: a record of it waiting from an earlier day (Before today) stops offering
    it, and the next morning has nothing of it to offer. Its record moves to Before today's archive, where it can be
    brought back; nothing is written when it has no such record."""
    from thirties_core.carry import DROPPED, OpenPool
    if getattr(a, "_db", None) is None:
        return
    pool = OpenPool(a._db)
    if pool.all().get(line.strip().lower()) is None:
        return
    pool.set(line, horizon=DROPPED, pending_drop=False, dropped_on=a.pt.today().isoformat(), dropped_by="user")
# --- end an item's line on today's list ---


# --- dropped for good only on words for that item, and never when the drop is said for today ---
# Which item a removal means is the model's reading (change_asks.py), so a drop word alone no longer says which
# item goes for good: "scrap the glaze bit for today" takes it off today and leaves it to do.
_FOR_TODAY_RE = re.compile(r"\b(?:for|from|off|just) (?:today|tonight|this (?:morning|afternoon|evening|shift))\b|"
                           r"\btoday'?s (?:plan|list|schedule)\b|\bnot today\b", re.IGNORECASE)
_FOR_GOOD_RE = re.compile(r"\b(?:for good|for ever|forever|permanently|altogether|delete|archive|never|no longer)\b",
                          re.IGNORECASE)


def _for_today(message: str) -> bool:
    """The drop is said for today only ("for today", "from today's plan"), with nothing saying for good."""
    return bool(_FOR_TODAY_RE.search(message)) and not _FOR_GOOD_RE.search(message)


def _words_point_to(pt: "PlanTools", title: str) -> bool:
    """The user's words this turn name the item or point back to it (done_reference.check)."""
    from thirties_core.done_reference import check
    try:
        held, _ = check(pt, title)
    except Exception:
        return False
    return held is None


def drop_meant(pt: "PlanTools", title: str) -> bool:
    """The user's words drop this item for good (it stops coming back): a drop word (write_guard.asks_to_drop),
    not said for today only, and words that name the item or point back to it. With nobody talking, the caller's
    own rules apply (True); the app's own controls need only the drop word."""
    from thirties_core.write_guard import asks_to_drop
    message = getattr(pt, "user_message", None)
    if message is None:
        return True
    if not asks_to_drop(message):
        return False
    if getattr(pt, "by_hand", False):
        return True
    return not _for_today(message) and _words_point_to(pt, title)
# --- end dropped for good ---


def remove_listed(pt: "PlanTools", event: Any, today_list: Any = None) -> Optional[str]:
    """remove_event for an item on today's list with no time: off the list, as the model chose (``today_list``,
    off by default). Which line the user meant is the model's reading, even when their words name none of them
    (a reply to a list just shown): a line on today's list can be put back. What holds is the floor: never one
    they asked to keep. None when the event isn't such an item (the caller says it isn't on the schedule)."""
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
    # asked for: which line is the model's reading; never one the user asked to keep (change_asks.py)
    from thirties_core.change_asks import kept
    keep = kept(pt, line, [task.id] if task is not None else [])
    if keep:
        raise ToolError(keep)
    choice = list_choice(today_list)
    if choice == "keep":
        return f"{line} has no time on the schedule; it stays on today's list."
    said = leave_list(pt, line, task.id if task is not None else None, choice, also=False).strip()
    return f"{said} (It had no time on the schedule.)"


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
