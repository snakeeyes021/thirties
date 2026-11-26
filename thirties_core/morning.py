"""The morning: one flow, one message, one policy.

When the user's first message of the day arrives, the day is built in that same turn: the morning reset, the
routines and anchors, yesterday's leftovers (carry.py), and a plan for the free time. The reply to that message is
the morning's one message. The background service does the same only if no turn did (``done`` is still unset),
for instance when the model couldn't be reached. ``done`` is set only after the morning has gone out.

``POLICY`` is the one written rule for what proposing means. The model prompt, the tool description and the
morning instruction all include it, so they can't drift apart.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Tuple

logger = logging.getLogger(__name__)

POLICY = ("What you put on the plan goes on at once: the schedule just IS the schedule, with no approval step. "
          "Tell the user what you put on, as a short notice, not a question. Call an item pressing, urgent or due "
          "only when its urgency (the user wants it soon, or by a date) or its due date says so; otherwise give the "
          "real reason you chose it, or none.")


def done_key(day) -> str:
    return f"rollover_offered:{day.isoformat()}"


def is_done(db, day) -> bool:
    return bool(db is not None and db.get_sync_meta(done_key(day)))


def mark_done(db, day, now: datetime, how: str = "1") -> None:
    """After the morning has gone out (never before: a failed morning is tried again)."""
    db.set_sync_meta(done_key(day), how)
    db.set_sync_meta("morning_sent_at", now.isoformat())


def prepare(manager, level: str = "medium") -> Tuple[str, str]:
    """Build the day and gather what the morning says: (facts for the message, the leftover brief for the
    model). Each step runs once a day, so calling this again is harmless."""
    import re
    db = manager.scheduler.state_db if manager.scheduler is not None else None
    assistant = manager.tools.assistant
    day = manager.day_plan.target_date
    holiday = ahead = ""
    if assistant is not None:
        from thirties_core.carry import settle
        from thirties_core.day_lists import planned_ahead_text
        settle(assistant, day)  # what's left from before, sorted before the morning is written
        assistant.start_the_day()
        with manager.turn_lock:
            assistant.routines.set_up_day(manager.day_plan)  # the routines go on by themselves: not news
            from thirties_core.morning_day import place_pinned
            place_pinned(assistant)  # what the user asked for today is on it (morning_day.py)
        name = db.get_sync_meta(f"holiday:{day.isoformat()}") if db is not None else None
        holiday = f"It's {name}, so I've left Work off today; say if you're working." if name else ""
    text = manager.execute_tool("rollover_proposal", {})
    if text.startswith("Nothing from yesterday") or text.startswith("NOT_DONE"):
        text = ""
    if assistant is not None:
        from thirties_core.day_lists import planned_ahead_text
        offered = [re.sub(r" \(since [^)]*\)$", "", line[2:].split(" – ", 1)[-1])
                   for line in text.splitlines() if line.startswith("- ")]
        ahead = planned_ahead_text(assistant, skip=offered)
    from thirties_core.calendar_engine import unread_text
    unread = unread_text(manager.day_plan.calendar_unread)  # an empty-looking day may not be free
    invites = invites_text(manager.day_plan)  # the assistant asks about them in its own words
    pulled = ""
    if assistant is not None and db is not None:  # what the longer plans pulled forward since the last morning
        from thirties_core.horizons import HorizonPlanner
        try:
            last = db.get_sync_meta("morning_sent_at")
            pulled = HorizonPlanner(manager.tools).pulled_text(datetime.fromisoformat(last) if last else None)
        except Exception as e:
            logger.warning("Couldn't say what the plans pulled forward: %s", e)
    week = week_line(manager) if assistant is not None and db is not None else ""
    # --- the leftovers said from the plan as it stands once every placement is made (leftover_facts.py) ---
    from thirties_core.leftover_facts import restate
    text = restate(assistant, text)
    # --- end as it stands ---
    facts = "\n\n".join(t for t in (holiday, unread, invites, ahead if level != "off" or text else "", text, pulled,
                                    week) if t)
    brief = assistant.leftover_brief() if assistant is not None else ""
    return facts, brief


WEEK_KEY = "week_notice:{day}"


def week_line(manager) -> str:
    """A new week, month or year: its plan is made now, while the day is built, and applied at once (no approval
    step). Returns the one line that says what went in, kept for the morning reply, which carries it: there's
    never a separate message for it (one morning message)."""
    db = manager.scheduler.state_db
    day = manager.day_plan.target_date
    try:
        from thirties_core.horizons import HorizonPlanner
        planner = HorizonPlanner(manager.tools)
        line = planner.offer_session() or ""
    except Exception as e:
        logger.warning("Planning the new period failed: %s", e)
        return ""
    if line:
        import json
        db.set_sync_meta(WEEK_KEY.format(day=day.isoformat()),
                         json.dumps({"line": line, "names": getattr(planner, "session_names", [])}))
    return line


def week_notice(db, day) -> Tuple[str, list]:
    """The new period's line made this morning, and the names in it ("" and [] when there's none)."""
    import json
    try:
        data = json.loads(db.get_sync_meta(WEEK_KEY.format(day=day.isoformat())) or "{}") if db is not None else {}
    except ValueError:
        data = {}
    return str(data.get("line") or ""), list(data.get("names") or [])


def invites_text(plan) -> str:
    """Today's calendar invites not answered yet, for the morning's facts: the user decides, once."""
    from thirties_core.clock import format_clock
    pending = sorted((s for s in plan.spans if s.tentative and not s.shown_only), key=lambda s: s.start_dt)
    if not pending:
        return ""
    return ("Calendar invites not answered yet (ask once whether the user is going; resolve_calendar_event "
            "records it): " + "; ".join(f"{s.title}, {format_clock(s.start_dt)}" for s in pending) + ".")


def in_turn_instruction(facts: str, brief: str) -> str:
    """For the user's first message of the day: answer it, and make that reply the morning."""
    from thirties_core.day_proposal import MORNING_FACTS, MORNING_INSTRUCTION
    body = MORNING_INSTRUCTION.split("] ", 1)[-1].replace(
        "If there's nothing to put on and nothing to say, reply with only: NOTHING", "")
    return ("[System check, not from the user] This is the user's first message of the day, and the day is being "
            "built now. Answer it, and in the same reply do the morning (this reply is the morning's only "
            "message): " + body + (MORNING_FACTS.format(facts=facts) if facts else "") + ("\n" + brief if brief else ""))


__all__ = ["POLICY", "done_key", "is_done", "mark_done", "prepare", "week_line", "week_notice",
           "in_turn_instruction"]
