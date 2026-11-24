"""Lines that read like tickets stay off the plan: a project's backlog is planned as time on the project, never
ticket by ticket.

A **ticket** is a line that belongs to a project's backlog rather than to the user's own things to do:
- any line on a list whose role is "backlog" (a project's backlog: planned as time on the project, "Recipe app
  dev", never ticket by ticket), which agent-written notes are guessed to be (lists.agent_written);
- a single line that reads like an agent's work log wherever it is (marked up and linked or very long: two of
  bold or code markup, a link, more than 140 characters), so one ticket dropped into an ordinary list isn't
  scheduled either.

Two rules, in code:
1. The day's options never offer a ticket on its own (with_steady_pieces drops them, after everything else).
2. A tool call that would put a ticket on the plan by itself (add_event, add_to_today, propose_day, ...) is held
   back unless the user's own message names it. It says to plan time on the project instead. The app's own
   buttons (no message) aren't held: the user pressed them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

TICKET_TOOLS = {"add_event", "add_to_today", "propose_day", "pin_to_period", "place_leftover"}
_BOLD = re.compile(r"\*\*|__")
_CODE = re.compile(r"`")
_LINK = re.compile(r"\]\(|https?://")
LONG = 140


def reads_like_ticket(text: str) -> bool:
    """A single line written like an agent's work log: two or more of markup, code, a link, great length."""
    text = text or ""
    signs = sum(1 for hit in (_BOLD.search(text), _CODE.search(text), _LINK.search(text), len(text) > LONG) if hit)
    return signs >= 2


def is_ticket(task) -> bool:
    if task is None or str(getattr(task, "id", "")).startswith("media:"):
        return False
    return getattr(task, "list_role", "") == "backlog" or reads_like_ticket(getattr(task, "title", ""))


def drop_tickets(cands: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The day's candidates without single tickets (rule 1); time on a project (no task) stays."""
    return [c for c in cands if not is_ticket(c.get("task"))]


def placed_refs(name: str, args: Dict[str, Any]) -> List[str]:
    if name == "add_to_today":
        items = args.get("items") or args.get("item") or args.get("title") or []
        if isinstance(items, str):
            items = [x for x in re.split(r"\n|;", items) if x.strip()]
        return [str(i.get("title") or i.get("item") or "") if isinstance(i, dict) else str(i) for i in items]
    from thirties_core.backoff import placing_refs
    return placing_refs(name, args)


def ticket_guard(pt, name: str, args: Dict[str, Any]) -> Optional[str]:
    """Rule 2: a NOT_DONE sentence when ``name`` would place a ticket the user didn't name, else None. Only for the
    outermost call (a tool's own steps were checked when it was called)."""
    if name not in TICKET_TOOLS or pt.assistant is None or getattr(pt, "_home_plan", None) is not None:
        return None
    said = pt.user_message
    if said is None:
        return None
    from thirties_core.item_facts import mentions
    others = [t.title for t in pt.assistant.s.tasks]  # a word only the ticket has names it; a shared one doesn't
    held = []
    for ref in placed_refs(name, args or {}):
        ref = (ref or "").strip()
        if not ref:
            continue
        try:
            task = pt.assistant._exact_task(ref)
        except Exception:
            task = None
        if task is None or not is_ticket(task):
            continue
        if mentions(said, task.title, others):
            continue  # the user asked for that very ticket
        held.append(task)
    if not held:
        return None
    from thirties_core.plan_tools import NOT_DONE
    where = sorted({t.parent_note_title or "its project" for t in held})
    names = "; ".join(_short(t.title) for t in held)
    return (f"{NOT_DONE}: {names} is a ticket in {', '.join(where)}, a project's backlog, and the user didn't name "
            f"it. Plan time on the project as one item instead ('Time on {where[0]}'); a single ticket goes on "
            "only when the user asks for it.")


def _short(title: str) -> str:
    try:
        from thirties_core.assistant_tools import _short as short
        return short(title, 60)
    except Exception:
        return title[:60]


__all__ = ["drop_tickets", "is_ticket", "placed_refs", "reads_like_ticket", "ticket_guard", "TICKET_TOOLS"]
