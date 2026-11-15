"""Whose is this event? Other people's calendar events, worked out in Python.

Every calendar event is one of four kinds, from the calendar data and the user's standing choices only:

- **mine**: on the user's own calendar (or a calendar they said is theirs);
- **shared**: the same event is on the user's calendar and someone else's (same series, or the same title and
  times); or it's on a household calendar ("Family", "Household", "Home", "Everyone", or one the user called
  shared); or the user is a guest on someone else's event and accepted; or its title says it's everyone's ("family
  dinner", "all of us"). On the user's plan, marked shared;
- **invite**: the user is a guest and hasn't answered: asked about, naming whose calendar it's on;
- **theirs**: on someone else's calendar, the user isn't on it, nobody said: left off the user's plan (their
  events go on it only when the user says so), shown faintly as theirs, never counted against their free time.

Copies of one event on several calendars are one item. A calendar is named the way the user would say it
("Jamie's calendar"; "your partner's calendar" once they've said whose it is). The learning (per series, per calendar,
who a calendar belongs to) is kept with the other event decisions, so it syncs between devices.

What's said about them (after_reply): the first time a series from someone else's calendar is on the day, Thirties
asks once, naming whose calendar it's on ("Are you going to this one too? It's on your partner's calendar."),
and remembers the answer for the whole series; until then it stays off the user's plan. A calendar whose
events the user has said "not mine" to three times, and "mine" to none, isn't asked about again. (Chosen over
leaving them out silently with a daily line, 2026-11-15: on simulated days it halved the events shown in the wrong
place, for about one extra question every other day.)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.clock import format_clock

# Calendars a household shares: their events are everyone's, the user's too (unless the user said otherwise).
HOUSEHOLD_CALENDAR_RE = re.compile(r"\b(family|household|home|everyone|everybody|ours|our)\b", re.I)
# Titles that say an event is for everyone in the house, the user included.
EVERYONE_TITLE_RE = re.compile(r"\b(family|everyone|everybody|all of us|both of us|whole house|household)\b", re.I)

OWNER_KEY = "whose:{calendar}"        # decisions table: who a calendar belongs to, in the user's words ("your partner")
ASKED_KEY = "asked:{series}"          # decisions table: asked about this series once (value: its calendar's name)
QUIET_SAID_KEY = "quiet_said:{calendar}"  # decisions table: said once that it stops asking about this calendar
QUIET_AFTER = 3   # three series from one calendar said "not mine", none "mine": stop asking about that calendar


def cal_key(name: str) -> str:
    return (name or "").strip().lower()


# --- naming ------------------------------------------------------------------------------------------------------

def owner_label(calendar_name: str, decisions: Optional[Dict[str, str]]) -> str:
    """Who the user said a calendar belongs to ("your partner"), or ""."""
    return (decisions or {}).get(OWNER_KEY.format(calendar=cal_key(calendar_name)), "")


def possessive(who: str) -> str:
    who = who.strip()
    return f"{who}'" if who.endswith("s") else f"{who}'s"


def whose(calendar_name: str, decisions: Optional[Dict[str, str]] = None) -> str:
    """The calendar as the user would say it: "your partner's calendar", "Jamie's calendar", "the Family calendar"."""
    label = owner_label(calendar_name, decisions)
    if label:
        return f"{possessive(label)} calendar"
    if HOUSEHOLD_CALENDAR_RE.search(calendar_name or "") and "calendar" not in (calendar_name or "").lower():
        return f"the {calendar_name.strip()} calendar"
    from thirties_core.not_yours import whose_calendar
    return whose_calendar(calendar_name)


def short_whose(phrase: str) -> str:
    """For a faint marker: "Jamie's", "your partner's", "Family calendar"."""
    if phrase.endswith("'s calendar") or phrase.endswith("' calendar"):
        return phrase[: -len(" calendar")]
    return phrase[4:] if phrase.startswith("the ") else phrase


def to_second_person(words: str) -> str:
    """The user's words about a person, as Thirties says them back: "my partner" -> "your partner"."""
    text = (words or "").strip().strip(".").strip()
    text = re.sub(r"^(it'?s |that'?s |this is )", "", text, flags=re.I)
    text = re.sub(r"\bmy\b", "your", text, flags=re.I)
    text = re.sub(r"'s calendar$|' calendar$| calendar$|'s$", "", text, flags=re.I).strip()
    return text


def marker(span) -> str:
    """The faint line under an event that isn't only the user's: "Jamie's calendar", "shared, also on Jamie's
    calendar". "" for the user's own."""
    phrase = getattr(span, "whose", "") or ""
    if not phrase:
        return ""
    if getattr(span, "shared", False):
        return f"shared, also on {phrase}"
    return phrase[0].upper() + phrase[1:] if span.shown_only or span.tentative else ""


# --- ownership ---------------------------------------------------------------------------------------------------

def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def merge_copies(events: List[Any]) -> Tuple[List[Any], Dict[str, List[str]]]:
    """One event per real event: copies of the same one on several calendars (same series and start, or the same
    title and times) collapse into one, the user's own copy first. Returns (events, the other calendars each kept
    event is also on)."""
    groups: Dict[Tuple, List[Any]] = {}
    order: List[Tuple] = []
    by_title: Dict[Tuple, Tuple] = {}
    for e in events:
        series_key_ = (e.series, e.start_dt.timestamp())
        title_key = (_norm_title(e.summary), e.start_dt.timestamp(), e.end_dt.timestamp())
        key = series_key_ if series_key_ in groups else by_title.get(title_key, series_key_)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
        by_title.setdefault(title_key, key)
    out: List[Any] = []
    also: Dict[str, List[str]] = {}
    for key in order:
        group = groups[key]
        if len({cal_key(e.calendar_name) or e.calendar_id for e in group}) <= 1:
            out.extend(group)
            continue
        keep = next((e for e in group if e.is_primary), group[0])
        out.append(keep)
        others = []
        for e in group:
            name = e.calendar_name or e.calendar_id
            if e is not keep and name not in others and cal_key(name) != cal_key(keep.calendar_name):
                others.append(name)
        also[keep.id] = others
    return out, also


def is_household(calendar_name: str, mode: str) -> bool:
    return mode == "shared" or (mode == "" and bool(HOUSEHOLD_CALENDAR_RE.search(calendar_name or "")))


def ownership(event, mode: str, decision: Optional[str], also_on: List[str],
              decisions: Optional[Dict[str, str]]) -> Tuple[str, str, bool]:
    """(mode to use, whose phrase, shared) for one event. The mode may become "mine" (a shared event: on the user's
    plan) or "show" (someone else's, nobody said: left out)."""
    if event.is_primary:
        if also_on:  # also on someone else's calendar: everyone's, the user's included
            return mode, " and ".join(whose(n, decisions) for n in also_on), True
        return mode, "", False
    phrase = whose(event.calendar_name or event.calendar_id, decisions)
    if also_on:
        phrase = " and ".join([phrase] + [whose(n, decisions) for n in also_on])
    if decision == "attend" and mode != "ignore":  # the user said it's theirs: on their plan, and someone else's
        return mode, phrase, True
    if mode in ("ignore", "holidays", "show") or decision == "decline":
        return mode, phrase, False
    if mode == "mine":
        return mode, phrase, False
    invite_open = event.response_status in ("tentative", "needsAction")
    if invite_open:
        return mode, phrase, False  # an invite: asked about, naming whose calendar
    if mode == "ask":
        return mode, phrase, False
    if is_household(event.calendar_name, mode) or getattr(event, "invited", False) \
            or EVERYONE_TITLE_RE.search(event.summary or ""):
        return "mine", phrase, True  # a household calendar, an invite they accepted, a family dinner: theirs too
    return "show", phrase, False  # someone else's, nobody said: left out


def should_ask(event, decisions: Optional[Dict[str, str]], modes_mode: str) -> bool:
    """Ask about this event of someone else's: its series is undecided, never asked, and its calendar hasn't
    proven to be "not the user's"."""
    if event.is_primary or modes_mode not in ("",):
        return False
    from thirties_core.calendar_engine import series_key
    d = decisions or {}
    if d.get(event.id) or d.get(series_key(event)) or d.get(ASKED_KEY.format(series=event.series)):
        return False
    return not calendar_quiet(cal_key(event.calendar_name or event.calendar_id), d)


def calendar_quiet(calendar: str, decisions: Dict[str, str]) -> bool:
    """Per-calendar learning: QUIET_AFTER of its series asked about and said "not mine", none "mine"."""
    mine = not_mine = 0
    for key, value in decisions.items():
        if not key.startswith("asked:") or cal_key(value) != calendar:
            continue
        decision = decisions.get("series:" + key[len("asked:"):])
        if decision == "attend":
            mine += 1
        elif decision in ("not_mine", "decline"):
            not_mine += 1
    return mine == 0 and not_mine >= QUIET_AFTER


# --- what's said ------------------------------------------------------------------------------------------------

def _line(span) -> str:
    return f"{span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)}"


def _mentioned(reply: str, title: str) -> bool:
    words = [w for w in _norm_title(title).split() if len(w) > 3] or _norm_title(title).split()
    low = _norm_title(reply)
    return bool(words) and sum(w in low for w in words) >= max(1, (len(words) + 1) // 2)


def questions(plan, now=None) -> List[Any]:
    """The spans to ask about (someone else's, a new series, not yet over), in time order."""
    return sorted((s for s in plan.spans_in_view() if getattr(s, "ask", False)
                   and (now is None or s.end_dt > now)), key=lambda s: s.start_dt)


def question_text(spans: List[Any]) -> str:
    if len(spans) == 1:
        s = spans[0]
        return (f"{_line(s)} is on {s.whose}. Is that yours too? Until you say, it stays off your plan.")
    return ("These are on other people's calendars today. Are any of them yours too? Until you say, they stay off "
            "your plan.\n" + "\n".join(f"{_line(s)}, {s.whose}" for s in spans))


def after_reply(tools, reply: str) -> str:
    """What Python makes sure a reply says about other people's events: each new series asked about once. Never
    changes the plan."""
    try:
        return _after_reply(tools, reply)
    except Exception:  # never in the way of a reply
        import logging
        logging.getLogger(__name__).exception("Couldn't add the line about other people's events")
        return reply


def _after_reply(tools, reply: str) -> str:
    from thirties_core.messages import join_sections
    plan = tools.day_plan
    sched = getattr(tools, "scheduler", None)
    db = sched.state_db if sched is not None else None
    if db is None:
        return reply
    now = tools._now() if hasattr(tools, "_now") else None
    if now is not None and not plan.day_start <= now < plan.day_end:
        now = None  # planning another day: everything on it is ahead
    asks = questions(plan, now)
    out = reply
    if asks:
        missing = [s for s in asks if not _mentioned(reply, s.title)]
        if missing:
            out = join_sections(out, question_text(missing))
        asked = {s.ask for s in asks}
        for s in plan.spans:  # the rest of each series on this day: asked too
            if getattr(s, "ask", "") in asked:
                s.ask = ""
        for ref in asked:
            calendar, _, series = ref.partition("|")
            db.set_event_decision(ASKED_KEY.format(series=series), calendar)
    return _quiet_notice(db, out)


def _quiet_notice(db, reply: str) -> str:
    """Once a calendar has proven not to be the user's, say once that Thirties stops asking about it."""
    from thirties_core.messages import join_sections
    decisions = db.get_event_decisions()
    calendars = {v for k, v in decisions.items() if k.startswith("asked:") and v}
    for cal in sorted(calendars):
        key = QUIET_SAID_KEY.format(calendar=cal_key(cal))
        if calendar_quiet(cal_key(cal), decisions) and not decisions.get(key):
            db.set_event_decision(key, "1")
            name = whose(cal, decisions)
            reply = join_sections(reply, f"I'll stop asking about {name}: its events stay off your plan unless you "
                                         f"say one is yours.")
    return reply


def prompt_rule() -> str:
    """The prompt's one paragraph about other people's events (frontier.py rule 32)."""
    return ("Other people's calendar events: Python works out whose each event is. An item marked \"shared, also "
              "on …\" is the user's and someone else's: plan it as theirs and say who it's with once. Someone else's "
              "events (Not yours today) are never the user's: never list them among their items, never plan around "
              "them, never count them against free time. When the user says one is theirs (\"that's mine\", \"I'm "
              "going to that\"), resolve_calendar_event attend=true; not theirs (\"that's my partner's\", \"not "
              "mine\"), attend=false; both hold for the whole series unless they say just this once. When they say "
              "whose a calendar is (\"that's my partner's calendar\"), set_calendar_mode with whose. Always say whose "
              "calendar an event is on as the schedule names it. UNCONFIRMED CALENDAR INVITES may list someone "
              "else's events to ask about once: ask in one short question naming whose calendar each is on "
              "(\"Pickup is on Jamie's calendar at 5:30: is that yours too?\"); never ask about any other of their "
              "events, and never ask again. If you don't ask, Python adds the question itself.")


def set_owner(db, calendar_name: str, words: str) -> str:
    """Who a calendar belongs to, in the user's words ("my partner"): used in every mention from now on."""
    label = to_second_person(words)
    if not label:
        raise ValueError("Who does the calendar belong to?")
    db.set_event_decision(OWNER_KEY.format(calendar=cal_key(calendar_name)), label)
    return label


def default_series(span, invite) -> bool:
    """Whether a decision on this event covers its whole series when the user didn't say: yes for someone else's
    event (a "not mine" on one class is about all of them; a "that's mine" too), no for the user's own."""
    if invite is not None and not invite.is_primary:
        return True
    return bool(getattr(span, "whose", "") and (span.shown_only or getattr(span, "ask", False)
                                                or not getattr(span, "shared", False)))


__all__ = ["merge_copies", "ownership", "should_ask", "calendar_quiet", "whose", "short_whose", "marker",
           "after_reply", "set_owner", "default_series", "questions", "question_text", "to_second_person"]
