"""Asking the same model once more about a past one-time item before it ends or is kept (carry.py rule 3, past_one_offs.py).

The morning model sorts each leftover; for a one-time item whose own time or date is over, its word alone doesn't
end it or keep it. A small helper call to the same model (the active brain's ``ask_json``) is asked one thing: has
the reason for the item passed? Both must agree:

- the model ends it as an event and the helper says OCCASION_PASSED: it ends silently;
- the model keeps it as a task and the helper says STILL_NEEDED: it is kept;
- anything else (they disagree, the helper can't tell, or no answer): the one morning question, not marked yet.

Python fills the helper's prompt from stored facts only (the item's plan, home, dates, the other items of that day,
and the user's own messages that day that name it, verbatim); it never judges the wording itself. Items the
model already leaves unknown get no helper call. One call per past one-time item kept or ended in a morning.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OCCASION_PASSED, STILL_NEEDED, CANT_TELL = "OCCASION_PASSED", "STILL_NEEDED", "CANT_TELL"
ANSWERS = (OCCASION_PASSED, STILL_NEEDED, CANT_TELL)
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": list(ANSWERS)},
                                           "reason": {"type": "string"}}, "required": ["answer", "reason"]}
WORDS_MAX, WORDS_MESSAGES = 200, 3

SYSTEM = """You help a personal planning app decide what happens to one item left over from an earlier day. You judge one
thing only, from the facts given. Answer with a single JSON object and nothing else."""

USER = ("""Today is {weekday}, {date}{holiday_note}.

The item: "{title}"
Planned for: {planned_day} at {planned_time} ({time_source}).
Where it lives: {home}.
Dates in its name: {dates_or_none}.
Other things planned that same day: {same_day_items}.
What the user said about it that day, word for word: {user_words_or_none}.
It was not marked done or skipped.

Decide which is true:
- OCCASION_PASSED: the item was tied to that day's occasion, so its purpose has now passed. It was for a trip, a
  show, an appointment, a meeting, an event or a holiday that is now over, or it was preparation for or a step of
  such an occasion.
- STILL_NEEDED: the need behind it still stands today. The time was only when the user meant to do it: an errand,
  a call, a chore, a purchase, a payment still owed, something to fix, make or send.
- CANT_TELL: the facts do not settle it.

Judge whether the REASON for the item still exists, not whether it could physically be done later (almost anything
could). Use the other items """
"""that day and the dates: if the occasion itself is past its date, treat preparation for
it as passed too, whether or not the occasion is marked done. When the item IS the occasion itself (a trip, a show, an
appointment, a meeting at a set time) and its own time is over, it is OCCASION_PASSED, even if a related day (a
holiday, a visit) is today: a missed one would be a new plan, not this item. Preparation for an occasion that is
today or later has not passed, so it is still needed. Something that still serves its purpose when it comes late (a belated gift, a
reply to a letter, a card, a payment) is not ended by the occasion passing: it is STILL_NEEDED, or CANT_TELL if unclear.
A passed due date alone does not end an item: if it is
still owed or still useful late, it is STILL_NEEDED. Only if missing the date made it pointless (a registration that
closed, a sale that ended, an RSVP for an event that's over) is it OCCASION_PASSED. When the facts point both ways,
answer CANT_TELL.

Reply only with: {"answer": "OCCASION_PASSED" | "STILL_NEEDED" | "CANT_TELL", "reason": "<one short line>"}""")  # one prompt, split only between source lines


# --- the call path: the first JSON object of a reply ---

def first_json(text: Any) -> Optional[Dict[str, Any]]:
    """The first JSON object in a reply (from its first "{"), or None."""
    if isinstance(text, dict):
        return text
    s = str(text or "")
    i = s.find("{")
    while i >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[i:])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            i = s.find("{", i + 1)
    return None


def offer(tools: Any, ask_json: Callable[[str, str], Optional[Dict[str, Any]]]) -> None:
    """Let the morning's tools reach the active brain's helper call (the brain made last is the one in use)."""
    assistant = getattr(tools, "assistant", None)
    if assistant is not None:
        assistant.ask_json = ask_json


# --- the facts, as the prompt says them ---

def _day_words(d: date) -> str:
    return d.strftime("%A, %B ") + str(d.day)


def _plan(assistant: Any, d: date):
    try:
        plan = assistant._plan_peek(d) if hasattr(assistant, "_plan_peek") else assistant.pt.plan_for(d)
    except Exception:
        return None
    return plan if plan is not None and getattr(plan, "target_date", d) == d else None


def same_day_items(assistant: Any, title: str, d: Optional[date]) -> str:
    """The other items planned that day: "<time> <title> (done|not marked|skipped)"; "none" when there are none."""
    from thirties_core.clock import format_clock
    from thirties_core.models import SpanStatus
    from thirties_core.step_times import key
    plan = _plan(assistant, d) if d is not None else None
    if plan is None:
        return "none"
    out = []
    for s in sorted((s for s in plan.spans if not s.is_envelope and key(s.title) != key(title)),
                    key=lambda s: s.start_dt):
        said = {SpanStatus.DONE: "done", SpanStatus.PARTIAL: "done", SpanStatus.SKIPPED: "skipped"}.get(s.status,
                                                                                                     "not marked")
        out.append(f"{format_clock(s.given_start or s.start_dt)} {s.title} ({said})")
    return "; ".join(out) or "none"


_WORD_RE = re.compile(r"[a-z][a-z'-]{3,}")
_COMMON = {"with", "from", "that", "this", "then", "into", "onto", "about", "before", "after", "today", "tomorrow"}


def _title_words(title: str) -> List[str]:
    from thirties_core.step_times import split_leading_time
    rest, _, _ = split_leading_time(title or "")
    return [w for w in _WORD_RE.findall((rest or title or "").lower()) if w not in _COMMON]


def user_words(assistant: Any, title: str, d: Optional[date]) -> str:
    """The user's visible messages that day that share the item's title words, word for word and short; "none"."""
    services = getattr(assistant, "s", None)
    store = getattr(services, "conversations", None)
    words = _title_words(title)
    if store is None or d is None or not words:
        return "none"
    need = 1 if len(words) == 1 else 2
    found = []
    try:
        convs = store.for_day(d)
    except Exception:
        return "none"
    for conv in convs:
        for m in conv.messages:
            text = str(m.get("content") or "")
            if m.get("role") != "user" or m.get("hidden") or m.get("kind") == "command" or text.startswith("[System"):
                continue
            low = text.lower()
            if sum(1 for w in set(words) if w in low) >= need:
                found.append('"' + (text if len(text) <= WORDS_MAX else text[:WORDS_MAX].rstrip() + "…") + '"')
    return " | ".join(found[:WORDS_MESSAGES]) or "none"


def fill(assistant: Any, item: Dict[str, Any], day: date) -> str:
    """The helper's user message, from stored facts only."""
    from thirties_core.holidays import names
    facts = item.get("facts") or {}
    title = item["title"]
    planned = date.fromisoformat(facts["planned_day"]) if facts.get("planned_day") else None
    holidays = names(getattr(assistant, "pt", None), day)
    text = USER
    if planned is not None and facts.get("planned_at"):
        source = "the user's own time" if facts.get("time_by_user") else "a time the app chose"
        planned_line = f"{_day_words(planned)} at {facts['planned_at']} ({source})"
    else:
        on = _day_words(planned) if planned is not None else _day_words(day - timedelta(days=1))
        planned_line = f"no time; it was on {on}'s list"
    home = f"on the list '{facts['home']}'" if facts.get("home") else "only on that day's list"
    dates = ", ".join(date.fromisoformat(x).strftime("%a %b ") + str(date.fromisoformat(x).day)
                      for x in facts.get("dates") or []) or "none"
    values = {
        "{weekday}": day.strftime("%A"),
        "{date}": day.strftime("%B ") + str(day.day) + day.strftime(", %Y"),
        "{holiday_note}": f", a holiday on the user's calendar ({', '.join(holidays)})" if holidays else "",
        "{title}": title,
        "{planned_day} at {planned_time} ({time_source})": planned_line,
        "{home}": home,
        "{dates_or_none}": dates,
        "{same_day_items}": same_day_items(assistant, title, planned or (day - timedelta(days=1))),
        "{user_words_or_none}": user_words(assistant, title, planned or (day - timedelta(days=1))),
    }
    for k, v in values.items():
        text = text.replace(k, v)
    return text


# --- the verdict ---

def verdict(assistant: Any, item: Dict[str, Any], day: date) -> Optional[str]:
    """The helper's answer for one past one-time item (OCCASION_PASSED, STILL_NEEDED or CANT_TELL), or None when
    there's no helper, no answer or no readable answer."""
    cache = getattr(assistant, "_helper_verdicts", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            assistant._helper_verdicts = cache
        except Exception:
            pass
    key = (day.isoformat(), str(item.get("title") or "").strip().lower())
    if key in cache:  # asked once a morning: a retried proposal doesn't ask again
        return cache[key]
    ask = getattr(assistant, "ask_json", None)
    if not callable(ask):
        logger.info("Second opinion on %s: no helper available.", item.get("title"))
        return None
    try:
        reply = ask(SYSTEM, fill(assistant, item, day))
    except Exception as e:
        logger.info("Second opinion on %s: the helper failed (%s).", item.get("title"), e)
        return None
    answer = str((reply or {}).get("answer") or "").strip().upper() if isinstance(reply, dict) else ""
    answer = answer if answer in ANSWERS else ""
    logger.info("Second opinion on %s: %s%s", item.get("title"), answer or "no answer",
                f" ({str(reply.get('reason') or '')[:120]})" if answer and isinstance(reply, dict) else "")
    if answer:
        cache[key] = answer  # no answer (a timeout) may be asked again
    return answer or None


def agrees(assistant: Any, item: Dict[str, Any], day: date, wanted: str) -> bool:
    """Whether the helper gives the answer the model's choice needs (OCCASION_PASSED to end, STILL_NEEDED to keep)."""
    return verdict(assistant, item, day) == wanted


__all__ = ["SYSTEM", "USER", "SCHEMA", "ANSWERS", "OCCASION_PASSED", "STILL_NEEDED", "CANT_TELL", "first_json",
           "offer", "fill", "same_day_items", "user_words", "verdict", "agrees"]
