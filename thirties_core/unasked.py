"""A move nobody asked for that didn't happen is never announced.

The move guard (change_scope.move_refused) keeps an item where it is when the user hasn't asked for anything to
move, this turn or just before, and the message doesn't name it: the model shifted something by itself. That's the
guard working, not a failure the user needs to hear about. (A move the user did ask for is the model's to read and
isn't refused: change_asks.py.) Yet the reply checks read every refused change as one, and the reply came back with "Wax the canoe
hull is still in the 2:00 PM block; I couldn't move it." about an item the user never mentioned.

Now:

- the guard's refusal tells the model to leave the move out of its reply (``LEAVE_OUT``);
- the plain "I couldn't ..." line is said only for a change the user asked for: in this message, or in their
  previous one and still not made (``unasked_titles`` lists the others, which the checks skip);
- a reply that claims such a move anyway has the claim dropped, with no line in its place (``mark``: the fact
  carries ``NOT_ASKED``, which plain_admit.from_fact says as nothing).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

# in the guard's refusal: how a refused move nobody asked for is known afterwards
LEAVE_OUT = "Don't mention this move in your reply"
# the aside on a check's fact about such an item: say nothing about it
NOT_ASKED = "(not asked for)"


def refusal_note(title: str) -> str:
    """What the guard adds to its refusal of a move the user didn't ask for."""
    return (f" {LEAVE_OUT} (no line saying {title} couldn't be moved): the user didn't ask for it, so it isn't "
            "news to them.")


def is_unasked(result: str) -> bool:
    """A tool result that is the guard's refusal of a move nobody asked for."""
    return LEAVE_OUT in str(result or "")


def still_asked(tools: Any, title: str) -> bool:
    """The user asked for a change to this item before, still pending: their previous message named it."""
    said, _ = getattr(tools, "prior_exchange", None) or ("", "")
    if not said or not title:
        return False
    from thirties_core.item_facts import mentions
    plan = getattr(tools, "day_plan", None)
    titles = [s.title for s in plan.spans if s.title] if plan is not None else []
    return mentions(said, title, titles)


def skip(tools: Any, title: str, result: str) -> bool:
    """This refused change is left out of what the reply has to own up to."""
    return is_unasked(result) and not still_asked(tools, title)


def unasked_titles(tools: Any, results: Iterable[tuple]) -> set:
    """Lowercased titles whose last change this turn is a refused move the user didn't ask for. ``results``:
    (title, tool result) in the order the calls ran."""
    last: Dict[str, str] = {}
    for title, out in results:
        if title:
            last[str(title).strip().lower()] = str(out or "")
    return {t for t, out in last.items() if skip(tools, t, out)}


def turn_results(brain: Any, turn_start: int) -> List[tuple]:
    """(the item's title, the result) for each tool result this turn, read as the brain reads them."""
    from thirties_core.own_up import _calls, _title
    return [(_title(brain.tools, args), str(m.get("content") or "")) for m, args in _calls(brain.messages, turn_start)]


def mark(brain: Any, turn_start: int, facts: Sequence[str]) -> List[str]:
    """The checks' facts, each about an item whose move was refused unasked marked ``NOT_ASKED``."""
    if not facts:
        return list(facts)
    try:
        quiet = unasked_titles(brain.tools, turn_results(brain, turn_start))
    except Exception:
        return list(facts)
    if not quiet:
        return list(facts)
    out = []
    for fact in facts:
        about = next((t for t in quiet if str(fact).lower().startswith(t + " ")), None)
        out.append(f"{fact} {NOT_ASKED}" if about and not str(fact).endswith(NOT_ASKED) else fact)
    return out


__all__ = ["LEAVE_OUT", "NOT_ASKED", "is_unasked", "mark", "refusal_note", "skip", "still_asked", "unasked_titles"]
