"""A time the user gives for an item is applied, on every path that places or changes it.

The rule is the one in given_times.py: a time the user states for their own item is theirs, kept on it and shown to
the minute. A clear time the user gave still went unapplied on a few paths, while the replies said it was set:

- **already there**: the item was at that very time under the hood (a routine put it there, or an earlier change),
  shown by its block. Changing it to the time it has was "nothing to change", and the time was never marked as the
  user's, so the block went on showing whatever the user said. Now the time becomes theirs (``already_there``).
- **named loosely**: "the afternoon stroll is 1:10" for Stroll by the canal, beside another stroll: no word of the
  clause named it outright, so the time wasn't read as the user's. A clause that gives the time, shares a word of
  the item's name, and names no other item outright is about it (``loosely_named``, read by given_times).
- **a routine step's own time**: placed at the time the user gave for it, but by put_routine, whose times are
  Thirties' own; the step the user gave a time for keeps it as theirs (``keep_step_time``).
- **this morning, before the plan begins**: up at 6:20 with the plan starting at 7:00, a time the user gives for
  6:40 is still ahead this morning; it's planned there instead of refused as tomorrow's (``this_morning_ahead``).
  One wholly before the plan begins is kept on the day like this morning's records (day_edges.is_this_mornings).

A time the user didn't give is never made theirs here: these only apply what their words this turn say.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Optional

from thirties_core.clock import format_clock

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools



def loosely_named(clause: str, title: str, titles: Iterable[str]) -> bool:
    """The clause is about the item though it doesn't name it outright: it has a word of the item's name, and it
    names no other item outright ("the afternoon stroll" beside Stroll by the canal and Stroll to the bakery)."""
    from thirties_core.item_facts import item_name, match_words, mentions
    titles = list(titles)
    if not match_words(title) & match_words(clause):
        return False
    return not any(mentions(clause, t, titles) for t in titles if item_name(t) != item_name(title))


def said_for(pt: "PlanTools", title: str, dt: Optional[datetime]) -> bool:
    """The user's message this turn gives this time for this item, whichever tool is placing it (a routine's step,
    a time this morning): the reading given_times.stated makes, without its own-placing rule."""
    from thirties_core.given_times import stated
    return stated(pt, title, dt, any_tool=True)


def already_there(pt: "PlanTools", span: "EventSpan", start_named: bool, end_named: bool) -> Optional[str]:
    """update_event to the times the item already has: when the user's words give them, they're theirs now and
    shown as said, and the change is done (not "nothing to change"). None when the user gave no such time."""
    if span.is_envelope or getattr(pt, "by_hand", False):
        return None
    from thirties_core.given_times import stated
    start_ok = start_named and stated(pt, span.title, span.start_dt)
    end_ok = end_named and stated(pt, span.title, span.end_dt)
    if not (start_ok or end_ok):
        return None
    from thirties_core.shown_times import shown_end, shown_start
    if (not start_ok or shown_start(span) == span.start_dt) and (not end_ok or shown_end(span) == span.end_dt):
        return None  # already shown as the user said it: there's nothing to change
    before = (span.given_start, span.given_end)
    if start_ok:
        span.given_start = span.start_dt
    if end_ok:
        span.given_end = span.end_dt
    if (span.given_start, span.given_end) != before:
        pt._persist(span)
        pt._save_plan_after_change()
    from thirties_core.shown_times import span_when
    return (f"{pt._title(span)} is at {span_when(span)} [id {span.id}], the time the user gave; it's shown at that "
            "time now.")


def calendar_already(pt: "PlanTools", span: "EventSpan", start: Any, end: Any) -> Optional[str]:
    """update_event on a calendar event to the times it already has, which the user's words give: there's nothing to
    keep apart from the calendar, and nothing is refused. None otherwise."""
    from thirties_core.facts import gave_time
    message = getattr(pt, "user_message", None)
    if message is None and not getattr(pt, "by_hand", False):
        return None
    try:
        wanted = [pt._start(start) if start not in (None, "") else None,
                  pt._start(end) if end not in (None, "") else None]
    except Exception:
        return None
    given = [dt for dt in wanted if dt is not None]
    if not given or wanted[0] not in (None, span.start_dt) or wanted[1] not in (None, span.end_dt):
        return None
    if not getattr(pt, "by_hand", False) and not any(gave_time(message, dt) for dt in given):
        return None  # the user gave one of them; the other is the calendar's own
    return (f"{span.title} is at {format_clock(span.start_dt)} – {format_clock(span.end_dt)} on the user's plan, the "
            "times they gave; nothing needed changing (their calendar itself isn't changed).")


def keep_step_time(pt: "PlanTools", plan: Any, title: str, at: datetime) -> None:
    """A routine step placed at its own time: the user's when their words this turn give it for the step."""
    if not said_for(pt, title, at):
        return
    from thirties_core.models import SpanStatus
    for span in plan.spans:
        if span.title == title and span.status == SpanStatus.PLANNED and span.start_dt == at:
            span.given_start = at
            pt._persist(span)


def this_morning_ahead(pt: "PlanTools", title: str, dt: datetime) -> Optional[datetime]:
    """The time this morning the user gave for this item, still ahead, before the day's plan begins: where it goes
    instead of being refused as tomorrow's. None when that isn't it."""
    from thirties_core.day_edges import this_morning_meant
    earlier = this_morning_meant(pt, dt)
    if earlier is None:
        return None
    try:
        now = pt._now()
    except Exception:
        return None
    if earlier < now or not said_for(pt, title, earlier):
        return None
    return earlier


__all__ = ["loosely_named", "said_for", "already_there", "calendar_already", "keep_step_time", "this_morning_ahead"]
