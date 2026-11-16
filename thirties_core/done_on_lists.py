"""A done for something not on today's plan is looked for on the user's lists first.

"I've finished the quilt binding", with Sew the quilt binding open on a list and nothing like it planned today: the
list item is what got done, so its line is ticked (as any done list item is), rather than a new unplanned item
being recorded beside it while the line stays open. The match is the done guard's (done_reference.py): the user's
words name the item by its distinctive words (item_facts.referenced), and the name the model gave shares a word
with it.

- exactly one open item matches: that's the one;
- several match: which one is the user's to say, so the model is told to ask;
- none: it's something done that wasn't on any list (recorded as new, as before).

Only while the user is talking: the app's own buttons and the background never come here.
"""

from __future__ import annotations

from typing import Any, List, Optional

# lists whose lines are never what a done means: templates and things kept for reference
_NOT_DONE_HERE = ("routine", "reference", "ignore")


def _candidates(assistant) -> List[Any]:
    from thirties_core.list_reading import ENTRY
    return [t for t in assistant.s.tasks if not t.id.startswith("media:") and t.list_role not in _NOT_DONE_HERE
            and not getattr(t, "someday", False) and (t.title or "").strip()
            and (getattr(t, "line_kind", "") or ENTRY) == ENTRY]  # a label, a link or an option is never "done"


def matches(assistant, event: str, message: Optional[str], skip: tuple = ()) -> List[Any]:
    """The open list items this done can be: the name the model gave shares a word with the item, and the user's
    words and that name together have at least half of the item's own words ("finished the quilt binding" for
    Sew the quilt binding; "descaled the kettle" is not Buy a new kettle). When several can, the one the user's
    words name by its distinctive words (item_facts.referenced), if exactly one; else all of them, to ask about. A
    Today line and the home item it's a copy of count once, as the home item. Items whose ids are in skip aren't
    candidates."""
    if not message or not str(event or "").strip():
        return []
    from thirties_core.item_facts import item_name, match_words, referenced
    from thirties_core.list_reading import match_text, name_of
    named, said = match_words(event), match_words(message)
    if not named:
        return []
    tasks = [t for t in _candidates(assistant) if t.id not in skip]
    found = []
    for t in tasks:
        mine = match_words(name_of(t))  # its name, without the notes on its line ("- 45", "- waiting on keys")
        if mine and named & mine and 2 * len(mine & (named | said)) >= len(mine):
            found.append(t)
    homes = {item_name(t.title) for t in found if t.list_role != "today"}
    found = [t for t in found if t.list_role != "today" or item_name(t.title) not in homes]
    if len(found) > 1:
        # two items named alike under different parents: the one whose parents the user's words name
        titles = [match_text(t) for t in tasks]
        pointed = [t for t in found if referenced(message, match_text(t), [o for o in titles if o != match_text(t)])]
        if len(pointed) == 1:
            return pointed
    return found


def on_the_lists(assistant, event: str) -> Optional[Any]:
    """The one open list item this done is about, None when there's none; a ToolError when several match."""
    found = matches(assistant, event, getattr(assistant.pt, "user_message", None))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        from thirties_core.assistant_tools import ToolError, describe_task
        raise ToolError("Nothing on today's plan is that, and the user's words match several open items on their "
                        "lists: " + "; ".join(describe_task(t) for t in found)
                        + ". Ask which one they finished, then call again with its task_id. If it's none of them, "
                          "record it once they say so.")
    return None


def instead_of_settled(assistant, event: str, span: Any) -> Optional[Any]:
    """The open list item a done is about when its name only matches an item settled earlier today ("finished the
    quilt binding after all", with a Quilt binding skipped this afternoon and Sew the quilt binding open on a list):
    that settled item isn't what got done now, the list item is. None (the settled item is corrected, as before)
    when the item is linked to a list item already (its own line on the Today note isn't one), was named by its
    id, or no single open item matches."""
    from thirties_core.models import SpanStatus
    pt = assistant.pt
    if getattr(pt, "user_message", None) is None or getattr(pt, "by_hand", False):
        return None
    if span.status == SpanStatus.PLANNED or str(event or "").strip() == span.id:
        return None
    if span.source == "calendar" or span.is_envelope:
        return None
    if span.task_id and not _own_today_line(assistant, span.task_id):
        return None
    if not span.task_id and assistant._exact_task(span.title) is not None:
        return None
    found = matches(assistant, str(event), pt.user_message, skip=(span.task_id,) if span.task_id else ())
    return found[0] if len(found) == 1 else None


def _own_today_line(assistant, task_id: str) -> bool:
    """The item is a line on the Today note that copies no item on another list (an activity put on Today)."""
    line = next((t for t in assistant.s.tasks if t.id == task_id), None)
    if line is None or line.list_role != "today" or assistant._db is None:
        return False
    from thirties_core.today import TodayLinks
    return TodayLinks(assistant._db).home_of(line.title) is None


__all__ = ["matches", "on_the_lists", "instead_of_settled"]
