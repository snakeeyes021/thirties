"""The assistant's tools beyond the day's plan, from their own modules, registered in one place:
notes to the engineers (engineer_notes.py),
people to keep in touch with (people.py), the statistics' observations in the profile once a week
(observations.py, run as the prompt is built), the sun and the moon on request (sky.py),
rush tickets at the user's theatre (tickets.py), writing items in the user's style (task_style.py), something to read or watch from their lists (media_picks.py),
and what was pushed back to the backlog at the end of the progress reports (progress.py: review_day and
week_overview are wrapped here, last in the handlers, so their own code stays as it is).

assistant_tools.py adds EXTRA_TOOL_SPECS, EXTRA_READ_ONLY and extra_handlers() in one block, so these modules stay
on their own.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from thirties_core import engineer_notes, goals, media_picks, observations, people, progress, sky, task_style, tickets

EXTRA_TOOL_SPECS: List[Dict[str, Any]] = [*engineer_notes.ENGINEER_TOOL_SPECS, *media_picks.MEDIA_TOOL_SPECS,
                                          *people.PEOPLE_TOOL_SPECS, *sky.SKY_TOOL_SPECS,
                                          *tickets.TICKET_TOOL_SPECS, *task_style.STYLE_TOOL_SPECS]
EXTRA_READ_ONLY = (set(engineer_notes.ENGINEER_READ_ONLY) | media_picks.MEDIA_READ_ONLY | people.PEOPLE_READ_ONLY
                   | sky.SKY_READ_ONLY | task_style.STYLE_READ_ONLY)


def extra_handlers(assistant) -> Dict[str, Callable[..., str]]:
    """The handlers, bound to the assistant's tools (``assistant._db`` is the app's database)."""
    return {
        "engineer_notes": lambda **_: engineer_notes.list_tool(assistant._db),
        "suggest_media": lambda kind="", alongside="", **_: media_picks.suggest(assistant, kind, alongside),
        "log_contact": lambda person="", how="", when=None, note="", **_: people.log_contact(
            assistant, person, how, when, note),
        "keep_in_touch": lambda person="", every=None, stop=None, **_: people.keep_in_touch(
            assistant, person, every, stop),
        "people_overview": lambda person="", **_: people.overview(assistant, person),
        "sky": lambda day=None, **_: sky.tool(assistant, day),
        "rush_tickets": lambda watch=None, theatre=None, listing=None, terms=None, **_: tickets.tool(
            assistant, watch, theatre, listing, terms),
        "task_style": lambda list="", about="", **_: task_style.tool(assistant, list, about),
        "review_day": lambda **kw: progress.review_day(assistant, **kw),
        "week_overview": lambda **kw: progress.week_overview(assistant, **kw),
    }


RULES = """MORE YOU CAN DO (with the tools listed below)
- The sun and the moon (the next sunrise, the moon's phase): call sky and say what it gives; never work them out or guess.
- Something to read, watch or listen to: suggest_media, then pick one or two that fit and say where each is from. Alongside other work, add it at that item's start (add_event); the two share the time.
- How a day or a week went: review_day or week_overview; include what was pushed back to the backlog, as they give it.
- When the user mentions seeing, texting, calling or spending time with someone, log_contact; how often they want to be in touch is keep_in_touch.
- Before drafting list items for the user (a breakdown, a new project's tasks), call task_style and write them like their own lines.
- Rush tickets at the user's theatre: rush_tickets (a daily check, only when the user turns it on)."""


def prompt_section(tools) -> str:
    """What the brain's prompt shows from these modules (each part only when it has something to say)."""
    services = getattr(tools, "services", None)
    assistant = getattr(tools, "assistant", None)
    if assistant is not None:
        observations.refresh_if_due(assistant)  # weekly: the statistics' lasting patterns into the profile
    parts: List[str] = [media_picks.prompt_section(services.tasks if services else [])]
    if services is not None and getattr(services, "profile", None) is not None:
        parts.append(goals.prompt_section(services.profile.sections(), tools.today()))
    if assistant is not None:
        parts.append(people.prompt_section(assistant._db, tools.today()))
    parts.append(RULES)
    return "\n\n".join(p for p in parts if p)


def after_reply(tools, reply: str) -> str:
    """The final reply, with what these modules guarantee in it (a suggestion says where it's from)."""
    return media_picks.after_reply(getattr(tools, "assistant", None), reply)


__all__ = ["EXTRA_TOOL_SPECS", "EXTRA_READ_ONLY", "extra_handlers", "prompt_section", "after_reply"]
