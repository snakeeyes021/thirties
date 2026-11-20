"""Asked why an item is where it is, the model hears where the item's record is, before it answers.

This used to be a check after the reply: one that gave a cause without looking at the item's record was sent back
to "call item_history first". The rewrite then came back as a line announcing the lookup, and that line was what
the user got. Whether the record answers the question is the model's judgement, so now the model gets a note with
the user's message instead (the record is in item_history), and answers once, from it, in the same turn.
"""

from __future__ import annotations

from typing import Any, List, Optional


def asked_about(tools: Any, said: str) -> List[str]:
    """The plan's items a "why" question names ("why is the canoe patch so late?"), or [] when it isn't one."""
    from thirties_core.reply_checks import WHY_RE
    if not said or not WHY_RE.search(said):
        return []
    plan = getattr(tools, "day_plan", None)
    if plan is None:
        return []
    from thirties_core.item_facts import mentions
    try:
        titles = [tools._title(s) for s in plan.spans if not s.is_envelope]
    except Exception:
        titles = [s.title for s in plan.spans if not s.is_envelope]
    return list(dict.fromkeys(t for t in titles if mentions(said, t, titles)))


def note(tools: Any, said: str) -> Optional[str]:
    """The note for the model, or None when the message asks no "why" about an item on the plan."""
    named = asked_about(tools, said)[:2]
    if not named:
        return None
    items = " and ".join(named)
    return (f"(The user's message asks why about {items}. item_history has the record for {items}: answer from "
            "what it records, in this reply; if it records no reason, say you don't know.)")


__all__ = ["asked_about", "note"]
