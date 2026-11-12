"""What a message can ask for: the fixed set of intents both brains map a turn onto, and which of them destroy
something.

The turn is understand → decide and act → say. Understanding is the model's job: it maps the user's meaning (never
just their verb) to intents from this set, with their slots. Python decides and acts through the shared tools,
and the reply is worded from what actually happened.

Each intent says whether it's **destructive**: whether it changes the user's own lists or records in a way a plain
"undo" in the same breath wouldn't. A destructive intent runs only when the user's own words ask for it outright
("delete it", "archive it", "get rid of it for good", "forget that"); anything less takes the intent's safe reading
instead, and the reply says what was done, so the user can correct it. "Drop it" about tonight's item means "not
today"; "drop it, I'm never doing that" means archive.

Destructive here: archiving an item, rewording, splitting or moving list lines, moving a group of items to
another list, changing a whole calendar's mode, forgetting a memory note, and rewriting a whole profile section.

The guard is applied where the tools run (PlanTools.execute), so it holds for every brain and every path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class Intent:
    name: str
    means: str
    slots: Tuple[str, ...] = ()
    destructive: bool = False
    safe: str = ""            # what a destructive intent becomes without explicit words
    tools: Tuple[str, ...] = field(default_factory=tuple)


INTENTS: Dict[str, Intent] = {i.name: i for i in (
    Intent("add", "put something on the plan", ("title", "start", "after", "before", "end", "duration", "day",
                                                 "alongside"), tools=("add_event",)),
    Intent("move", "change when something happens", ("target", "start", "end", "shift", "after", "before", "to_day",
                                                      "push"), tools=("update_event", "push_back", "swap_events")),
    Intent("take_off_plan", "take something off the plan (it stays on its list)", ("target",),
           tools=("remove_event", "clear_time")),
    Intent("outcome", "say what happened: done, partly done, skipped", ("target", "status", "started", "finished"),
           tools=("log_outcome",)),
    Intent("today", "put something on today: with a time, unless the user asks for none", ("title",),
           tools=("add_to_today", "if_there_is_time")),
    Intent("list_add", "add something to one of the user's lists", ("title", "list"), tools=("add_task",)),
    Intent("list_edit", "add steps under a list item, or break it down", ("target", "add_under"),
           tools=("edit_list_item", "break_down_task")),
    Intent("list_restructure", "reword, split or move list items to another list or section", ("target", "rename_to",
                                                                                              "split_into", "to_list"),
           destructive=True, safe="", tools=("edit_list_item", "apply_triage_group")),
    Intent("drop", "stop rolling something over (its list is untouched)", ("target",), tools=("leftover_action",)),
    Intent("archive", "take an item off its list for good: ticked and filed", ("target",), destructive=True,
           safe="drop", tools=("leftover_action",)),
    Intent("forget", "delete something the assistant remembers", ("target",), destructive=True, safe="",
           tools=("forget",)),
    Intent("hide_calendar", "hide a whole calendar", ("target",), destructive=True, safe="",
           tools=("set_calendar_mode",)),
    Intent("profile_rewrite", "rewrite a whole section of the user's profile", ("section", "text"), destructive=True,
           safe="remember", tools=("update_profile",)),
    Intent("facts", "how pressing an item is, when it belongs, rules it must keep", ("target", "urgency", "when",
                                                                                     "after", "fixed", "daylight"),
           tools=("set_urgency", "set_horizon", "set_item_facts", "pin_to_period")),
    Intent("routine", "put on, skip or change a routine", ("title", "start"),
           tools=("put_routine", "skip_routine", "set_routine", "set_anchor")),
    Intent("show", "see the schedule, lists, free time or history", ("topic",),
           tools=("show_schedule", "list_schedule", "find_open_time", "show_period", "search_backlog")),
    Intent("suggest", "ideas for what to do", ("topic", "duration"), tools=("day_options", "propose_day")),
    Intent("answer", "yes, no, or a choice, to the assistant's last question", ("option",)),
    Intent("remember", "how something works for the user, or a goal or preference", ("text",),
           tools=("remember", "update_profile")),
    Intent("energy", "how energetic or tired they are", ("level",), tools=("log_energy",)),
    Intent("chat", "small talk: nothing to change"),
)}

# The user's own words asking for removal outright. Nothing less runs a destructive intent.
EXPLICIT_REMOVAL = re.compile(
    r"\b(archive|delete|remove it|get rid of|drop (it|that|this) (completely|for good|entirely)|for good|"
    r"never (going to|gonna|doing|do)|not ever)\b", re.IGNORECASE)
EXPLICIT_FORGET = re.compile(r"\b(forget|delete|remove|erase|stop remembering)\b", re.IGNORECASE)
EXPLICIT_HIDE = re.compile(r"\b(hide|ignore|don'?t show|stop showing|not mine|isn'?t mine|show[- ]only|only show)\b",
                           re.IGNORECASE)
EXPLICIT_CALENDAR = re.compile(r"\bcalendars?\b", re.IGNORECASE)  # about the calendar itself, not one event
EXPLICIT_RESTRUCTURE = re.compile(
    r"\b(rename|reword|retitle|call (it|that|this)|split|break (it|this|that) (up|into)|move|put (it|that|this|them) "
    r"(in|on|under|into)|file (it|that|them)|change (it|the wording)|rewrite|merge|combine)\b", re.IGNORECASE)
EXPLICIT_YES = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|do it|go ahead|sounds good|please do|split|move)\b",
                          re.IGNORECASE)
EXPLICIT_REWRITE = re.compile(r"\b(rewrite|replace|start over|scrap|clear|wipe|delete|remove)\b", re.IGNORECASE)


def explicit(intent: str, said: Optional[str]) -> bool:
    """The user's words ask for this destructive intent outright."""
    if not said:
        return False
    if intent == "hide_calendar":
        return bool(EXPLICIT_HIDE.search(said) and EXPLICIT_CALENDAR.search(said))
    pattern = {"archive": EXPLICIT_REMOVAL, "forget": EXPLICIT_FORGET, "list_restructure": EXPLICIT_RESTRUCTURE,
               "triage": EXPLICIT_YES, "profile_rewrite": EXPLICIT_REWRITE}.get(intent)
    return bool(pattern is not None and pattern.search(said))


def destructive_intent(tool: str, args: Dict[str, Any]) -> Optional[str]:
    """The destructive intent a tool call carries out, if any."""
    if tool == "leftover_action" and str(args.get("action") or "").strip().lower() == "archive":
        return "archive"
    if tool == "forget":
        return "forget"
    if tool == "set_calendar_mode" and str(args.get("mode") or "").strip().lower() in (
            "ignore", "hide", "hidden", "show", "show only", "show-only"):
        return "hide_calendar"
    if tool == "edit_list_item" and any(args.get(k) for k in ("rename_to", "split_into", "to_section", "to_list")):
        return "list_restructure"
    if tool == "apply_triage_group":
        return "triage"
    if tool == "update_profile" and str(args.get("replace") or "").strip().lower() in ("true", "1", "yes"):
        return "profile_rewrite"
    return None


def guard(tool: str, args: Dict[str, Any], said: Optional[str]) -> Tuple[str, Dict[str, Any], str]:
    """(tool, args, note): a destructive call without explicit words becomes its safe reading, or is held back with
    a note saying what would be needed. ``said`` is the user's message; None when the app's own buttons act (the
    user pressed it: that is explicit)."""
    intent = destructive_intent(tool, args)
    if intent is None or said is None or explicit(intent, said):
        return tool, args, ""
    if intent == "archive":
        return tool, {**args, "action": "drop"}, " Say \"archive it\" to take it off its list too."
    if intent == "profile_rewrite":  # the non-destructive reading: add the line, keep what's there
        return tool, {**args, "replace": False}, " (Added, not rewritten: say \"rewrite it\" to replace the section.)"
    if intent == "hide_calendar":  # "don't show me the rehearsal" is that event, never the whole calendar
        return "", args, ("NOT_DONE: that would change the whole calendar, and the user only talked about an event. "
                          "Hide that event instead (resolve_calendar_event with attend=false, for this time or the "
                          "series), and say so; change the calendar only if they say the calendar.")
    means = INTENTS[intent].means if intent in INTENTS else "move a group of items to another list"
    return "", args, (f"NOT_DONE: that would {means}, and the user didn't ask for that outright. "
                      "Ask them first, in one short question.")


__all__ = ["Intent", "INTENTS", "EXPLICIT_REMOVAL", "explicit", "destructive_intent", "guard"]
