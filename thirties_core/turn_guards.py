"""What a turn may change, decided by Python from the user's own words, not left to the model's reading of them.

- **A question is a fact for the model, not a block.** When the user's message ends with a question mark,
  the model is told so (``question_fact``) and to change the plan only when the message asks for it. Whether
  "can the run go at 5?" or an exasperated "why is it still there??" is an ask for a change is the model's
  reading; no wording here holds a call back for being a question.
- **Meant for another day, kept there.** Something the user put on a later day (due then, or waiting for that
  day's list) doesn't go on today's plan unless the user's words this turn bring it up ("the tweaks are for
  tomorrow", then put on tonight).
- **Free time stays open.** Relaxing, downtime and free time never go on the plan unless the user asked for it in
  this turn, and something restful the user did ask for is never checked in on.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# words that only carry the tone of a message, not a request or a statement
_NEUTRAL_RE = re.compile(r"^\W*(?:ok(?:ay)?|k|kk|thanks?(?: you)?|thx|ty|lol|lmao|haha+|hmm+|huh|oh|ah|uh+|um+|so|"
                         r"well|yeah|yep|yes|no|nope|right|cool|great|nice|wait|hey|hi|hello|morning|good morning)"
                         r"\W*$", re.IGNORECASE)
# what the user says when they want rest on the plan
REST_ASKED_RE = re.compile(r"\b(?:rest\w*|relax\w*|break|nap\w*|lie down|lying down|couch|chill\w*|breather|downtime|"
                           r"down time|free time|time off|recover\w*|decompress\w*|unwind\w*|wind down|me time|"
                           r"quiet time|sleep\w*|do nothing|nothing planned|lazy|veg out)\b", re.IGNORECASE)
# a plan item that is relaxing, not a thing to do
RELAX_RE = re.compile(r"^\W*(?:(?:some|a|an|quick|short|long)\s+)*(?:relax\w*|chill\w*|downtime|down time|free time|"
                      r"me time|quiet time|unwind\w*|wind down|decompress\w*|leisure|r\s*&\s*r|rest|resting|"
                      r"break|breather|nap|veg out|do nothing|nothing)\b", re.IGNORECASE)


def question_fact(message: Optional[str]) -> str:
    """The note given to the model when the user's message ends with a question mark: a fact, never a block. An
    empty string when it doesn't."""
    if not message or not message.rstrip().endswith("?"):
        return ""
    return ("The user's message is a question. Change the plan, the lists or the records only when the message asks "
            "for that change (a question or a complaint can be that ask: a rhetorical question that demands "
            "something gone or done is a demand). When it only asks, answer from the plan and your notes and "
            "change nothing; if a change would help, say what you'd do and let them ask for it.")


def _building_the_day(pt: "PlanTools") -> bool:
    db = pt._state_db
    if db is None or not pt.is_current(pt.day_plan):
        return False
    from thirties_core import morning
    return not morning.is_done(db, pt.day_plan.target_date)


_PROPOSING = {"propose_day", "apply_day_proposal"}


def refuse(pt: "PlanTools", name: str, args: Dict[str, Any], outermost: bool = True) -> Optional[str]:
    """The reason a call is held back this turn, or None. Two rules apply to the model's own calls when the user has
    spoken: something put on a later day stays there, and free time stays open (a day proposal never plans
    relaxing at all). The message being a question holds nothing (``question_fact``), and the app's controls are
    never held here."""
    if pt.by_hand:
        return None
    if name == "add_event" and not outermost:
        from thirties_core.plan_log import outer_tool
        if outer_tool() in _PROPOSING:
            return refuse_unasked_relax(pt, str(args.get("title") or ""))
    if pt.user_message is None or not outermost:
        return None
    if name in ("add_event", "propose_day") and not _building_the_day(pt) and pt.is_current(pt.day_plan) \
            and args.get("day") in (None, ""):
        refs = ([args] if name == "add_event" else
                [i for i in (args.get("items") or []) if isinstance(i, dict)] if isinstance(args.get("items"), list) else [])
        for ref in refs:
            later = later_day(pt, str(ref.get("title") or ref.get("item") or ref.get("task") or ""),
                              str(ref.get("task_id") or ""))
            if later:
                title, day = later
                return (f"{title} is for {day}, as the user set it; it stays there unless they ask for it today. "
                        "Leave it off today's plan.")
    if name in ("add_event", "doing_now"):
        title = str(args.get("title") or args.get("item") or "")
        if RELAX_RE.match(title) and not REST_ASKED_RE.search(pt.user_message):
            return (f"Free time stays open: '{title}' doesn't go on the plan unless the user asks for rest or a "
                    "break. Leave the time free.")
    return None


def later_day(pt: "PlanTools", ref: str, task_id: str = "") -> Optional[tuple]:
    """(title, "Friday") when this item is meant for a later day than today (due then, or waiting for that day's
    list) and the user's words this turn don't name it; else None."""
    from datetime import date
    from thirties_core.item_facts import mentions
    assistant = pt.assistant
    if assistant is None:
        return None
    task = next((t for t in assistant.s.tasks if task_id and t.id == task_id), None) or \
        (assistant._exact_task(ref) if ref else None)
    title = task.title if task is not None else ref
    if not title or mentions(pt.user_message, title):
        return None
    today = pt.day_plan.target_date
    days = []
    if task is not None and (task.due or "")[:10]:
        try:
            days.append(date.fromisoformat(task.due[:10]))
        except ValueError:
            pass
    if pt._state_db is not None:
        try:
            from thirties_core.day_lists import AheadLists
            low = title.strip().lower()
            days += [date.fromisoformat(r["day"]) for r in AheadLists(pt._state_db).all_records()
                     if r.get("title", "").strip().lower() == low and r.get("state", "waiting") == "waiting"]
        except Exception:
            pass
    later = [d for d in days if d > today]
    if not later:
        return None
    day = min(later)
    return title, ("tomorrow" if (day - today).days == 1 else day.strftime("%A"))


def refuse_unasked_relax(pt: "PlanTools", title: str) -> Optional[str]:
    """For Python's own proposals (no user words): relaxing is never proposed."""
    if RELAX_RE.match(str(title or "")):
        return f"Free time stays open: '{title}' isn't something to plan."
    return None


def after_add(pt: "PlanTools", span: "EventSpan") -> None:
    """Something restful on the plan is never checked in on ("did you get to relaxing?")."""
    if span is None or not RELAX_RE.match(span.title or ""):
        return
    mutes = getattr(getattr(pt.assistant, "s", None), "mutes", None) if pt.assistant is not None else None
    if mutes is None:
        return
    mutes.mute(span.end_dt + timedelta(hours=6), span.id)


__all__ = ["question_fact", "refuse", "later_day", "refuse_unasked_relax", "after_add", "RELAX_RE", "REST_ASKED_RE"]
