"""Tools for what the user is told: the schedule as Python lays it out, items to check on, and an item's record.

They sit beside the schedule and assistant tools in ``PlanTools`` and share its dispatcher and contract
(natural-language arguments in, short plain-English results out, ``NOT_DONE`` when nothing changed).

- ``show_schedule``: where things stand, the one standard view (whats_next.standing: Now, The rest of today, Not
  marked yet), or another day's plan (messages.render_schedule). Python puts it under the reply itself, once
  (one_schedule.place); the model only writes its own sentences.
- ``check_on``: the user asks to be checked on for an item ("ask me if I've left for the vet"): contextual.py.
- ``item_history``: what's actually recorded about an item, so the model answers "why is X skipped?" from facts
  instead of making up a story (A4: 10-28, an item skipped with no recorded
  cause; 10-27, a made-up account of why an item was split).
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
        "description": "The standard view of where things stand (Now, The rest of today, Not marked yet), or "
                       "another day's plan; the default answer whenever the plan is asked for. "
                       "Python puts it under your reply by itself, once; you write only your own "
                       "sentences, never the rows. Read-only.",
        "parameters": {"whole_day": "true only if the user asks for earlier today (or the whole day): what today "
                                    "held before now goes above the standard view.",
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
        """The day being lived: the standard view (whats_next.standing), the same text a change puts under a reply.
        Another day, or outside the day: that day's plan (messages.render_schedule). Either way the text goes under
        the reply by Python (conversation.py), and the model is told so: it never lays the day out itself."""
        from thirties_core.messages import MissedTracker, missed_for, render_schedule
        from thirties_core.whats_next import PLACED, standing
        tools = self.tools
        plan = tools.day_plan
        now = tools._now()
        live = tools.is_current(plan) and plan.day_start <= now < plan.day_end
        text = standing(tools, now, whole_day=_as_bool(whole_day)) if live else ""
        if not text:
            from thirties_core.item_path import labelled_plan, tasks_of  # a nested item with its parents
            text = render_schedule(labelled_plan(plan, tasks_of(tools)), None, whole_day=_as_bool(whole_day))
        missed = missed_for(tools, now, muted_too=True) if live else []  # asked for: muted or not
        if missed:
            MissedTracker(tools._state_db).mark(now, missed)  # shown: notices and replies needn't list them again
            from thirties_core.away import LastSentList  # the list as last sent (away.py)
            LastSentList(tools._state_db).mark(now, missed)
        tools.shown_schedule = text  # the conversation layer puts this under the reply, once
        note = PLACED if live else ("Python puts this day's plan under your reply by itself, exactly once. Never "
                                    "copy, rewrite or summarise it; write only your own sentences.")
        return f"{note}\n\n{text}"

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
                    # a hidden message is Thirties' own (a system check), never the user's words
                    if (m.get("role") == "user" and not m.get("hidden")
                            and not str(m.get("content", "")).startswith("[System check")
                            and title in str(m.get("content", "")).lower()):
                        at = m.get("at", "")
                        when = format_clock(datetime.fromisoformat(at).astimezone(tz)) if at else "earlier"
                        lines.append(f"{when}: the user said \"{str(m['content'])[:200]}\"")
        if len(lines) == 1:
            lines.append("Nothing else is recorded about it: no changes logged, no list writes and nothing said "
                         "today. If asked how it got this way, say you don't know.")
        return "\n".join(lines)


__all__ = ["MESSAGE_TOOL_SPECS", "MESSAGE_READ_ONLY", "MessageTools"]
