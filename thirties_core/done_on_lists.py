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
    return [t for t in assistant.s.tasks if not t.id.startswith("media:") and t.list_role not in _NOT_DONE_HERE
            and not getattr(t, "someday", False) and (t.title or "").strip()]


def matches(assistant, event: str, message: Optional[str]) -> List[Any]:
    """The open list items this done can be: the name the model gave shares a word with the item, and the user's
    words and that name together have at least half of the item's own words ("finished the quilt binding" for
    Sew the quilt binding; "descaled the kettle" is not Buy a new kettle). When several can, the one the user's
    words name by its distinctive words (item_facts.referenced), if exactly one; else all of them, to ask about. A
    Today line and the home item it's a copy of count once, as the home item."""
    if not message or not str(event or "").strip():
        return []
    from thirties_core.item_facts import item_name, match_words, referenced
    named, said = match_words(event), match_words(message)
    if not named:
        return []
    tasks = _candidates(assistant)
    found = []
    for t in tasks:
        mine = match_words(t.title)
        if mine and named & mine and 2 * len(mine & (named | said)) >= len(mine):
            found.append(t)
    homes = {item_name(t.title) for t in found if t.list_role != "today"}
    found = [t for t in found if t.list_role != "today" or item_name(t.title) not in homes]
    if len(found) > 1:
        titles = [t.title for t in tasks]
        pointed = [t for t in found if referenced(message, t.title, [o for o in titles if o != t.title])]
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


__all__ = ["matches", "on_the_lists"]
