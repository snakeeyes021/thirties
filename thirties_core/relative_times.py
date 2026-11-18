"""Times the user gives relative to now, worked out by Python: "glazed it 35 minutes ago", "began 50 minutes ago",
"at it since 2:15".

The model never does the arithmetic (AGENTS.md §3.2), and a time it works out is dropped when the user's message
doesn't show it (assistant_tools._said_times_only). Without this, "finished the glaze an hour ago" left the model's
correct answer on the floor and the record took the planned time, or now, instead; an item planned for later in the
day was even recorded in the future.

So Python reads the user's words itself. The clauses of the message about the item (one that names it, the ones
after it that name nothing else, or all of them when the message names no item) give its times, each read as a start
or an end:

- "<amount> ago": now minus the amount ("20 minutes", "an hour and a half", "half an hour", a bare "30" is minutes);
- "since <clock>" or "since <amount> ago": a start;
- "started/began/finished/done ... at <clock>": the most recent time on that clock that isn't later than now.

A time after a start word ("started", "began", "since") is a start; after an end word ("done", "finished", "back")
an end; with neither, the caller says which (log_outcome reads "glazed it 25 minutes ago" as an end, doing_now
reads "the kiln, 25 minutes ago" as a start). Every time is kept between the day's start and now: something done is
never recorded in the future, nor before the day began.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, Optional, Tuple

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

START, END = "start", "end"

_LONGEST = timedelta(hours=18)  # "ago" further back than this isn't about today

_AGO_RE = re.compile(r"\bago\b", re.IGNORECASE)
_SINCE_CLOCK_RE = re.compile(r"\bsince\s+(?:about\s+|around\s+|like\s+)?(noon|midday|(\d{1,2})(?::(\d{2}))?\s*"
                             r"([ap])?\.?\s*m?\.?)(?![\w:])", re.IGNORECASE)
_VERB_CLOCK_RE = re.compile(r"\b(?:at|around|about)\s+(noon|midday|(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?)"
                            r"(?![\w:])", re.IGNORECASE)
_START_WORDS = re.compile(r"\b(?:start(?:ed|ing)?|began|begun|begin|kicked off|got going|since|been (?:at|on|doing)"
                          r"|got started|set off|left)\b", re.IGNORECASE)
_END_WORDS = re.compile(r"\b(?:done|finish(?:ed)?|ended|wrapped(?: up)?|completed?|stopped|over|got back|back|"
                        r"came back|returned)\b", re.IGNORECASE)
# where one thing said ends and the next begins (as in given_times, without splitting on "and": "the glaze and
# the kiln log finished 35 minutes ago" gives both the same time)
_AND_RE = re.compile(r"\band\b", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[;!?\n]|,\s|(?<![ap]\.m)\.(?:\s|$)|\b(?:then|but|also)\b", re.IGNORECASE)

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25,
                 "twenty five": 25, "thirty": 30, "forty": 40, "forty-five": 45, "forty five": 45, "fifty": 50}


def _normalize(text: str) -> str:
    t = " ".join(text.lower().replace("-", " - ").split()).replace(" - ", "-")
    t = re.sub(r"\b(?:about|around|roughly|maybe|like|almost|nearly|just|only|or so|ish)\b", " ", t)
    t = re.sub(r"\b(?:an?|one) hour and a half\b", "90 minutes", t)
    t = re.sub(r"\b(\d+) and a half hours?\b", lambda m: f"{int(m.group(1)) * 60 + 30} minutes", t)
    t = re.sub(r"\b(?:a )?half (?:an )?hour\b", "30 minutes", t)
    t = re.sub(r"\ba (?:few|couple(?: of)?)\b", "few", t)
    t = re.sub(r"\bfew (?=hours?|hrs?)", "2 ", t)
    t = re.sub(r"\bfew (?=minutes?|mins?)", "5 ", t)
    t = re.sub(r"\ban?\b(?=\s+(?:hours?|hrs?|minutes?|mins?)\b)", "1", t)
    for word, n in sorted(_NUMBER_WORDS.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\b{word}\b", str(n), t)
    return " ".join(t.split())


def amount_before(text: str) -> Optional[int]:
    """The minutes in the words just before an "ago" ("an hour and a half" → 90, "30" → 30), or None."""
    from thirties_core.clock import ClockError, parse_duration_minutes
    words = _normalize(text).split()
    for n in range(min(len(words), 8), 0, -1):
        tail = " ".join(words[-n:])
        if not re.match(r"\d", tail):
            continue
        try:
            minutes = parse_duration_minutes(tail)
        except (ClockError, ValueError):
            continue
        return minutes if timedelta(minutes=minutes) <= _LONGEST else None
    return None


def _clock_back(m: "re.Match", now: datetime) -> Optional[datetime]:
    """The most recent time on this clock that isn't later than now (a bare "2" is 2 PM at 3 PM, 2 AM at 3 AM)."""
    if m.group(1).lower() in ("noon", "midday"):
        hours, minute = [12], 0
    else:
        h, minute = int(m.group(2)), int(m.group(3) or 0)
        if not (0 <= h <= 23 and 0 <= minute <= 59):
            return None
        mer = (m.group(4) or "").lower()
        if mer:
            if not 1 <= h <= 12:
                return None
            hours = [(h % 12) + (12 if mer == "p" else 0)]
        else:
            hours = [h] if h == 0 or h > 12 else [h % 12, h % 12 + 12]
    candidates = [datetime.combine(now.date() - timedelta(days=back), datetime.min.time(), tzinfo=now.tzinfo)
                  .replace(hour=hour, minute=minute) for back in (0, 1) for hour in hours]
    past = [c for c in candidates if c <= now + timedelta(minutes=5)]
    best = max(past, default=None)
    return best if best is not None and now - best <= _LONGEST else None


def _kind(before: str, default: Optional[str]) -> Optional[str]:
    """Start or end, from the nearest start or end word before the time."""
    last = None
    for m in _START_WORDS.finditer(before):
        last = (m.start(), START)
    for m in _END_WORDS.finditer(before):
        if last is None or m.start() > last[0]:
            last = (m.start(), END)
    return last[1] if last else default


def _times_in(clause: str, now: datetime, default: Optional[str]) -> List[Tuple[str, datetime]]:
    """The (kind, time) pairs a clause gives, in order."""
    found: List[Tuple[int, int, Optional[str], datetime]] = []
    for m in _AGO_RE.finditer(clause):
        minutes = amount_before(clause[:m.start()])
        if minutes is not None:  # its kind is read below, from the words since the time before it
            found.append((m.start(), m.end(), None, now - timedelta(minutes=minutes)))
    for m in _SINCE_CLOCK_RE.finditer(clause):
        dt = _clock_back(m, now)
        if dt is not None:
            found.append((m.start(), m.end(), START, dt))
    for m in _VERB_CLOCK_RE.finditer(clause):
        if any(a <= m.start() < b for a, b, _, _ in found):
            continue
        before = clause[:m.start()]
        kind = _kind(before, None)  # a clock time with no start or end word isn't read here (given_times is)
        dt = _clock_back(m, now) if kind else None
        if dt is not None:
            found.append((m.start(), m.end(), kind, dt))
    out: List[Tuple[str, datetime]] = []
    last_end = 0
    for start, end, kind, dt in sorted(found):
        if kind is None:
            kind = _kind(clause[last_end:start], None) or _kind(clause[:start], default)
        if kind:
            out.append((kind, dt.replace(second=0, microsecond=0)))
        last_end = end
    return out


def _clauses(message: str, now: datetime) -> List[str]:
    """The message cut where one thing said ends; pieces joined by "and" are cut only after a piece with its own
    time ("woke up two hours ago and started work 20 minutes ago" is two; "the glaze and the kiln log done 20
    minutes ago" stays one)."""
    out: List[str] = []
    for clause in _CLAUSE_RE.split(message):
        if not clause or not clause.strip():
            continue
        pending: List[str] = []
        for piece in _AND_RE.split(clause):
            pending.append(piece)
            if _times_in(piece, now, END):
                out.append(" and ".join(pending))
                pending = []
        if pending:
            out.append(" and ".join(pending))
    return out


def said_times(message: Optional[str], about: Optional[Callable[[str], bool]], now: datetime,
               day_start: Optional[datetime], default: Optional[str] = END,
               elsewhere: Optional[Callable[[str], bool]] = None) -> Tuple[Optional[datetime], Optional[datetime]]:
    """The (start, end) the user's message gives relative to now for one thing. ``about`` says whether a clause
    names it, ``elsewhere`` whether it names something else; a clause that names nothing is about whatever the
    message named last ("sorted the glaze buckets, began 50 minutes ago, finished 10 minutes ago"). In a message
    that names nothing at all, its times are the item's: the model already chose the item. ``day_start``: nothing
    is earlier than this (None: no floor). Neither is ever later than now."""
    if not message:
        return None, None
    current: Optional[bool] = None  # True: this item was named last; False: something else; None: nothing yet
    mine: List[Tuple[str, datetime]] = []
    unnamed: List[Tuple[str, datetime]] = []
    named_any = False
    for clause in _clauses(message, now):
        if about is not None and about(clause):
            current, named_any = True, True
        elif elsewhere is not None and elsewhere(clause):
            current, named_any = False, True
        times = _times_in(clause, now, default)
        if current is True:
            mine += times
        elif current is None:
            unnamed += times
    if not mine and not named_any:
        mine = unnamed
    start = end = None
    for kind, dt in mine:
        dt = min(dt, now)
        if day_start is not None:
            dt = max(dt, day_start)
        if kind == START and start is None:
            start = dt
        elif kind == END and end is None:
            end = dt
    if start is not None and end is not None and end <= start:
        start = None  # an end said before the start: the end is what's sure
    return start, end


def _about_item(title: str, titles: Iterable[str]) -> Callable[[str], bool]:
    from thirties_core.item_facts import mentions
    others = [t for t in titles if t and t != title]
    return lambda clause: mentions(clause, title, others)


def for_item(pt: "PlanTools", title: Optional[str], default: Optional[str] = END,
             about: Optional[Callable[[str], bool]] = None, floor: bool = True) -> Tuple[Optional[datetime],
                                                                                         Optional[datetime]]:
    """What the user's words this turn say about when ``title`` started or ended, on the day being lived. Nothing
    when nobody is talking (the app's buttons, the background) or the plan is another day's."""
    message = getattr(pt, "user_message", None)
    if not message or getattr(pt, "by_hand", False):
        return None, None
    plan = pt.day_plan
    try:
        if not pt.is_current(plan):
            return None, None
    except Exception:
        return None, None
    titles = [s.title for s in plan.spans if s.title]
    if about is None and title:
        about = _about_item(title, titles)
    others = [t for t in titles if t != title]
    from thirties_core.item_facts import mentions

    def names_another(clause: str) -> bool:  # a clause about some other item: its times aren't this one's
        return any(mentions(clause, t, [x for x in titles if x != t]) for t in others)
    return said_times(message, about, pt._now(), plan.day_start if floor else None, default,
                      names_another if title else None)


def outcome_times(pt: "PlanTools", title: str, start_dt: Optional[datetime], end_dt: Optional[datetime],
                  started: Any, finished: Any, duration: Any, length: Optional[timedelta]) -> Tuple[
                      Optional[datetime], Optional[datetime], Any, Any]:
    """log_outcome's times with what the user said relative to now put in: (start, end, started, finished), where
    ``started``/``finished`` are set to the time when the user said it, so the record shows it as said. A start with
    nothing said about the end ends after its length (``duration``, else ``length``), and never later than now."""
    said_start, said_end = for_item(pt, title, END)
    if said_start is None and said_end is None:
        return start_dt, end_dt, started, finished
    from thirties_core.clock import format_clock, parse_duration_minutes
    now = pt._now()
    span_len = timedelta(minutes=parse_duration_minutes(duration)) if duration not in (None, "") else None
    if said_start is not None:
        start_dt, started = said_start, format_clock(said_start)
        if said_end is None and (end_dt is None or end_dt <= said_start):
            end_dt = min(said_start + (span_len or length or timedelta(minutes=30)), now)
            finished = None
            if end_dt <= said_start:
                end_dt = now
    if said_end is not None:
        end_dt, finished = said_end, format_clock(said_end)
        if said_start is None and (start_dt is None or start_dt >= said_end):
            start_dt = said_end - span_len if span_len else None
            started = format_clock(start_dt) if start_dt is not None else None
            if start_dt is not None:
                start_dt = max(start_dt, pt.day_plan.day_start)
    return start_dt, end_dt, started, finished


def begun_at(pt: "PlanTools", title: str) -> Optional[datetime]:
    """When the user's words say something they're doing now began ("started 20 minutes ago", "since 2"), or
    None."""
    start, _ = for_item(pt, title, START)
    return start


_DAY_TIMES = (  # (log_day_times argument, what a clause about it names, read as, which one)
    ("woke", re.compile(r"\b(?:woke|wake|awake|got up)\b", re.IGNORECASE), END, False),
    ("slept", re.compile(r"\b(?:slept|sleep|asleep|bed)\b", re.IGNORECASE), START, False),
    ("work_started", re.compile(r"\bwork", re.IGNORECASE), START, True),
    ("work_ended", re.compile(r"\bwork", re.IGNORECASE), END, True),
)


def day_times(pt: "PlanTools", given: dict) -> dict:
    """log_day_times' arguments with a time the user said relative to now put in ("got up an hour ago", "started
    work 20 minutes ago", "off work since 5"), as a clock time. Only arguments the model passed are touched."""
    from thirties_core.clock import format_clock
    out = dict(given)
    for name, words, kind, on_the_day in _DAY_TIMES:
        if given.get(name) in (None, ""):
            continue
        start, end = for_item(pt, None, kind, about=lambda c, w=words: bool(w.search(c)), floor=on_the_day)
        dt = start if kind == START else end
        if dt is not None:
            out[name] = format_clock(dt)
    return out


__all__ = ["amount_before", "begun_at", "day_times", "for_item", "outcome_times", "said_times"]
