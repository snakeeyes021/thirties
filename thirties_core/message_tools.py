"""Tools for what the user is told: the schedule as Python lays it out, items to check on, and an item's record.

They sit beside the schedule and assistant tools in ``PlanTools`` and share its dispatcher and contract
(natural-language arguments in, short plain-English results out, ``NOT_DONE`` when nothing changed).

- ``show_schedule``: the schedule for a reply, laid out by Python (messages.render_schedule). The model puts it in
  its reply as is; the conversation layer swaps in this text if the model rewrote the rows (A5, C2).
- ``check_on``: the user asks to be checked on for an item ("ask me if I've left for the vet"): contextual.py.
- ``item_history``: what's actually recorded about an item, so the model answers "why is X skipped?" from facts
  instead of making up a story (A4: 10-28 12:40, "accidentally marked skipped when clearing yesterday's
  check-in"; 10-27 21:38, the account of why Cloudflare was split).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List

from thirties_core.assistant_tools import ToolError
from thirties_core.clock import format_clock

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


MESSAGE_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "show_schedule",
        "description": "The schedule laid out for the user: where they are now, what's not marked yet, then the "
                       "rest of the day (or another day). Put the text in your reply as it is. Read-only.",
        "parameters": {"whole_day": "true only if the user asks for the whole day (or for earlier today).",
                       "day": "Which day (default: the day being planned)."},
        "required": [],
    },
    {
        "name": "check_on",
        "description": "The user wants to be asked about an item when its time comes (\"ask me if I've left for "
                       "the vet\", \"make sure I start the taxes\"). One message, once its time has begun.",
        "parameters": {"item": "The item's id or title.", "on": "false to stop (default true)."},
        "required": ["item"],
    },
    {
        "name": "item_history",
        "description": "What is recorded about an item on the plan: its status and times, every change to it "
                       "(added, moved, resized, marked, removed: when, by which tool, and the message or Day page "
                       "action that caused it), the writes to the user's lists about it, and what was said about "
                       "it today. Use it before explaining how something came to be. Read-only.",
        "parameters": {"item": "The item's id or title."},
        "required": ["item"],
    },
]

MESSAGE_READ_ONLY = {"show_schedule", "item_history"}


class MessageTools:
    def __init__(self, tools: "PlanTools") -> None:
        self.tools = tools

    def handlers(self) -> Dict[str, Any]:
        return {"show_schedule": self.show_schedule, "check_on": self.check_on, "item_history": self.item_history}

    # --- show_schedule ---

    def show_schedule(self, whole_day: Any = False, **_: Any) -> str:
        from thirties_core.messages import MissedTracker, missed_for, render_schedule
        tools = self.tools
        plan = tools.day_plan
        now = tools._now()
        live = tools.is_current(plan) and plan.day_start <= now < plan.day_end
        missed = missed_for(tools, now) if live else []
        text = render_schedule(plan, now if live else None, whole_day=_as_bool(whole_day), missed=missed)
        if missed:
            MissedTracker(tools._state_db).mark(now, missed)  # shown: notices and replies needn't list them again
        tools.shown_schedule = text  # the conversation layer makes sure the reply carries this, as it is
        return text

    # --- check_on ---

    def check_on(self, item: Any, on: Any = True, **_: Any) -> str:
        from thirties_core.contextual import ContextualFlags
        if self.tools._state_db is None:
            raise ToolError("There's nowhere to keep that here.")
        span = self.tools._find_span(item)
        flag = on in (None, "") or _as_bool(on)
        ContextualFlags(self.tools._state_db).set(span.id, flag)
        if not flag:
            return f"I won't check on {span.title}."
        return f"I'll check on {span.title} once its time comes ({format_clock(span.start_dt)})."

    # --- item_history ---

    def item_history(self, item: Any, **_: Any) -> str:
        tools = self.tools
        span = tools._find_span(item)
        tz = span.start_dt.tzinfo
        lines = [f"{span.title}: {format_clock(span.start_dt)} – {format_clock(span.end_dt)}, {span.status.value}"
                 + (f" (recorded {span.time_source})" if getattr(span, "time_source", "") else "") + "."]
        if span.actual_start or span.actual_end:
            lines.append("Actual time: " + " – ".join(format_clock(t) for t in (span.actual_start, span.actual_end) if t)
                         + ".")
        db = tools._state_db
        title = span.title.strip().lower()
        if db is not None:  # every change to it on the plan, with the tool and the message behind it (plan_log)
            from thirties_core.plan_log import changes_for, describe
            for row in changes_for(db, span.id, span.title, span.start_dt.date().isoformat()):
                lines.append(describe(row, tz))
        if db is not None:
            from thirties_core.joplin_writeback import WriteLog
            since = tools.day_plan.day_start.astimezone(timezone.utc).isoformat()
            for w in WriteLog(db).since(since):
                text = " ".join([str(w["op"].get("title") or w["op"].get("parent_title") or "")] + w["removed"] + w["added"])
                if title not in text.lower():
                    continue
                when = datetime.fromisoformat(w["at"]).astimezone(tz)
                cause = w.get("turn") or "no message (done by Thirties on its own)"
                lines.append(f"{format_clock(when)}: list write '{w['op'].get('op')}' to {w['note_title'] or 'a note'}"
                             f" ({'ok' if w['ok'] else 'failed'}), caused by: {cause[:200]}")
        if tools.services is not None:
            for conv in tools.services.conversations.for_day(tools.day_plan.target_date):
                for m in conv.messages:
                    if m.get("role") == "user" and title in str(m.get("content", "")).lower():
                        at = m.get("at", "")
                        when = format_clock(datetime.fromisoformat(at).astimezone(tz)) if at else "earlier"
                        lines.append(f"{when}: the user said \"{str(m['content'])[:200]}\"")
        if len(lines) == 1:
            lines.append("Nothing else is recorded about it: no changes logged, no list writes and nothing said "
                         "today. If asked how it got this way, say you don't know.")
        return "\n".join(lines)


__all__ = ["MESSAGE_TOOL_SPECS", "MESSAGE_READ_ONLY", "MessageTools"]
