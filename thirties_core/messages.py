"""What the user is told about where the day stands: the status line, what's not marked yet, and the schedule.

One place builds these, so replies, notices, Telegram's /now and /day, and the prompt all say the same thing the
same way (10-24 12:03: the app said "travel starting at 1:45", the phone said "1:30 block"). Python lays the
schedule out; the model only decides when to show it (AGENTS.md §7.3).

- **The status line** says which block the user is in and what's in it ("You're in the 10:00 block: Shower + Journal").
- **Missed items** ("Not marked yet") come right after it, one per line, never twice, and never items of the block
  the user is still in (the user's own proposal, 10-29 17:27; C2: 10-26 09:15, 10-29 18:00, 10-30 10:10).
- **The schedule** reads from now on unless the whole day is asked for (10-26 10:16: the user asked for the
  schedule from the current hour on), with no "(until…)" when the next item follows straight on (10-29 16:24),
  and things sharing time as one entry (10-18 15:00: "Garden app + Thirties").
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, Sequence

from thirties_core.clock import agenda_lines, format_clock, slice_lines, thirty_ceil, thirty_of
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus
from thirties_core.shown_times import exact_ends, shown_end, start_exact

HALF = timedelta(minutes=30)
MISSED_HEADING = "Not marked yet:"


# --- what's behind ---

def missed_items(plan: DayPlan, now: datetime, skip: Optional[Callable[[EventSpan], bool]] = None) -> List[EventSpan]:
    """Today's planned items whose time is over with nothing said, in order, once each (an earlier day's are that
    day's: Before today sorts them).

    An item counts only once the block it ends in is over: what's in the block the user is still in isn't missed
    (10-26 09:15: the user was asked about items of the block still under way). The same title
    twice (a copy left by a move, a routine step planned twice) is listed once (10-29 18:00)."""
    block = thirty_of(now)
    out: List[EventSpan] = []
    seen: dict = {}  # title -> the list items listed under it (two list lines with the same words are two items)
    for s in sorted(plan.active_spans(), key=lambda s: (s.start_dt, s.end_dt)):
        if s.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK) or s.status != SpanStatus.PLANNED \
                or s.tentative or thirty_ceil(s.end_dt) > block or s.start_dt < plan.day_start:  # today's only
            continue
        if skip is not None and skip(s):
            continue
        key = s.title.strip().lower()
        ids = seen.get(key)
        if ids is not None and (not s.task_id or s.task_id in ids or None in ids):
            continue
        seen.setdefault(key, set()).add(s.task_id or None)
        out.append(s)
    return out


def missed_lines(spans: Iterable[EventSpan]) -> List[str]:
    """One line per item, no times (they're behind; a time would only say how far). Not one line per block:
    10-30 10:10, a routine's steps were listed as a single item."""
    out, seen = [], set()
    for s in spans:
        if s.title.strip().lower() not in seen:
            seen.add(s.title.strip().lower())
            out.append(f"- {s.title}")
    return out


def join_sections(*sections: Optional[str]) -> str:
    """The one place a message's sections are put together (a reply, a status, the schedule, what's next, what's
    not marked yet, a notice): one blank line between each, empty ones left out, so the layout never drifts."""
    return "\n\n".join(str(s).strip("\n") for s in sections if s and str(s).strip())


def missed_block(spans: Sequence[EventSpan]) -> str:
    """What the user reads: the heading and one plain line per item (no bullets)."""
    return "\n".join([MISSED_HEADING] + [line[2:] for line in missed_lines(spans)]) if spans else ""


class MissedTracker:
    """Which missed items the user has already been shown today (in a notice, a schedule reply, or a question).

    Notices add the list only when something new fell behind since it was last shown (C2: 10-25 every notice
    appended the Not marked yet list; 10-26 to 10-27 the list grew with every notice all evening), and a reply doesn't ask
    again about what was shown (C5). Kept in sync_meta, per day, like the background service's other memories."""

    KEY = "missed_shown"

    def __init__(self, state_db) -> None:
        self.db = state_db

    def _load(self, day: str) -> dict:
        try:
            data = json.loads(self.db.get_sync_meta(self.KEY) or "{}") if self.db is not None else {}
        except ValueError:
            data = {}
        return data if data.get("day") == day else {"day": day, "ids": [], "titles": []}

    def shown(self, now: datetime) -> dict:
        return self._load(now.date().isoformat())

    def anything_new(self, now: datetime, spans: Sequence[EventSpan]) -> bool:
        data = self.shown(now)
        return any(s.id not in data["ids"] for s in spans)

    def mark(self, now: datetime, spans: Sequence[EventSpan] = (), titles: Sequence[str] = ()) -> None:
        if self.db is None:
            return
        data = self.shown(now)
        data["ids"] = sorted(set(data["ids"]) | {s.id for s in spans})
        data["titles"] = sorted(set(data["titles"]) | {s.title.strip().lower() for s in spans}
                                | {t.strip().lower() for t in titles})
        self.db.set_sync_meta(self.KEY, json.dumps(data))

    def titles_shown(self, now: datetime) -> set:
        return set(self.shown(now)["titles"])


# --- the status line ---

def _row_items(plan: DayPlan) -> List[EventSpan]:
    """What the user's schedule lists: their own things. Someone else's events (show-only calendars) are for the
    Agenda's quiet "not yours" note, never the text schedule sent in chat or Telegram."""
    return [s for s in plan.spans_in_view() if s.category != SpanCategory.SLEEP and not s.shown_only
            and s.status not in (SpanStatus.SKIPPED,) and s.end_dt > s.start_dt]


# The person asking where they are now: the status line is the answer, whatever they set and however recently
# it was shown
WHERE_ASKED_RE = re.compile(r"\b(where am i|what(?:'s| is) (?:on )?(?:right )?now|which block|what block|"
                            r"what am i (?:on|doing))\b", re.IGNORECASE)


def status_wanted(config, db, day, said: str = "") -> bool:
    """Whether replies say which block the person is in: on unless they turned it off (reply_extras.py), and
    always when they ask where they are."""
    if WHERE_ASKED_RE.search(said or ""):
        return True
    from thirties_core.reply_extras import effective
    return bool(effective(config, db, day)["status_line"])


def status_line(plan: DayPlan, now: datetime, next_start: Optional[datetime] = None, config=None, db=None,
                said: str = "") -> str:
    """Where the user is: the block, and what's in it. An end time only when what's on now runs past the block
    and isn't followed straight on by the next thing listed (10-29: an "(until 8:30)" right before the
    next item added nothing). With ``config`` or ``db`` (a reply to the person): "" when they turned it off and
    didn't ask where they are (``said``)."""
    if (config is not None or db is not None) and not status_wanted(config, db, plan.target_date, said):
        return ""
    block = thirty_of(now)
    here = [s for s in _row_items(plan) if not s.tentative and s.status == SpanStatus.PLANNED
            and s.start_dt < block + HALF and s.end_dt > block]
    on_top = [s for s in here if not s.is_envelope]
    here = on_top or here  # what's placed on Work is what's happening; Work alone when nothing is
    if not here:
        return f"You're in the {format_clock(block)} block, with nothing planned in it."

    def said(s: EventSpan) -> str:  # its end by its block, unless the user or a calendar gave it (shown_times)
        runs_on = shown_end(s) > block + HALF and shown_end(s) != next_start
        return f"{s.title} (until {format_clock(shown_end(s))})" if runs_on else s.title
    lines = slice_lines([(max(s.start_dt, block), s.end_dt, said(s)) for s in here])
    return f"You're in the {format_clock(block)} block: " + ", then ".join(lines) + "."


# --- the schedule ---

def _note(s: EventSpan) -> str:
    if s.tentative:
        return "unconfirmed"
    return s.status.value if s.status != SpanStatus.PLANNED else ""


def _titled(s: EventSpan, block_start: Optional[datetime] = None) -> str:
    """The item's title in a block's row, with its real start when the user or a calendar gave one inside the block
    ("Orthodontist at 2:45 PM" in the 2:30 PM row); Thirties' own minutes are never said (shown_times)."""
    start = thirty_of(s.start_dt) if block_start is None else block_start
    return f"{s.title} at {format_clock(s.start_dt)}" if start_exact(s) and s.start_dt != start else s.title


def schedule_rows(plan: DayPlan, start: Optional[datetime] = None, planned_only: bool = False) -> List[str]:
    """The plan from ``start`` on (the whole day without one), by blocks: each item once, in the block it starts
    in, things at the same time joined with +, a long item with its end, and others with an end time only before a
    gap (clock.once_lines). The one row
    formatter: the schedule (render_schedule) and what's next after a change (whats_next) both lay rows out
    here. Done items are never rows (11-02: done items still on the schedule were noise), not even for the whole
    day. ``planned_only``: what's still to come (nothing skipped or unanswered either)."""
    items = [s for s in _row_items(plan) if (start is None or s.start_dt >= start) and s.status != SpanStatus.DONE
             and (not planned_only or (s.status == SpanStatus.PLANNED and not s.tentative))]  # done: never listed
    rows = [(s.start_dt, s.end_dt, _titled(s), _note(s)) for s in sorted(items, key=lambda s: s.start_dt)]
    # each item once, a long one with its end: an item lasting all morning was a row in every thirty it touched,
    # and read as several items (clock.once_lines); ends by their blocks unless the user or a calendar gave them
    return agenda_lines(rows, once=True, exact=exact_ends(items))


def plain_rows(rows: List[str]) -> List[str]:
    """schedule_rows' rows as plain lines: no bullets, and a block with one thing after another on one line
    ("7:00 PM – Shower, then Journal (until 7:30 PM)"); everything else exactly as the schedule shows it."""
    grouped: List[list] = []
    for row in rows:
        if row.startswith("  ") and grouped:  # what follows within that half hour
            grouped[-1][1].append(row.strip())
        else:
            grouped.append([row[2:] if row.startswith("- ") else row, []])
    lines = []
    for head, parts in grouped:
        if parts:
            m = re.match(r"^(.*?–)\s*(\(until [^)]*\))?$", head)
            when, until = (m.group(1), m.group(2)) if m else (head, None)
            head = f"{when} {', then '.join(parts)}" + (f" {until}" if until else "")
        lines.append(head)
    return lines


def whole_day_lines(plan: DayPlan) -> List[str]:
    """The whole day as a record, by blocks: one plain line per thirty that something starts in, with the block it
    starts in and the end of the last block it reaches ("9:00 AM – 9:30 AM Wake stretch, Kettle on, Floss, Tidy desk"),
    done items too (it's what the day held). Things one after another in the thirty are joined with ", ", things
    at the same time with " + "; one that runs past the others says its own end ("Podcast (until 4:00 PM)"). The
    minutes under the hood (a routine's steps, a done item's estimate, a placed item's length) are never shown: a
    time is a block edge, unless the user or a calendar gave it ("Dentist at 2:45 PM", "I did it 3:10 to 3:40").
    Never a row per half hour, an empty time line with the items under it, or bullets."""
    groups: List[List[EventSpan]] = []
    for s in sorted(_row_items(plan), key=lambda s: (s.start_dt.timestamp(), -s.end_dt.timestamp())):
        if groups and thirty_of(groups[-1][0].start_dt) == thirty_of(s.start_dt):
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for group in groups:
        block = thirty_of(group[0].start_dt)
        end = max(shown_end(s) for s in group)
        # a line of facts only (a calendar event, what the user said they did, a time they stated) keeps its real start
        first = group[0].start_dt if all(start_exact(s) and s.start_dt == group[0].start_dt for s in group) else block
        notes = [_note(s) for s in group]
        one_note = notes[0] if len(group) > 1 and notes[0] and len(set(notes)) == 1 else ""

        def said(s: EventSpan) -> str:
            extras = []
            if shown_end(s) != end and shown_end(s) > block + HALF:
                extras.append(f"until {format_clock(shown_end(s))}")
            if _note(s) and not one_note:
                extras.append(_note(s))
            return _titled(s, first) + (f" ({', '.join(extras)})" if extras else "")  # a real time: "at 2:45 PM"
        lines = slice_lines([(s.start_dt, s.end_dt, said(s)) for s in group])
        out.append(f"{format_clock(first)} – {format_clock(end)} " + ", ".join(lines)
                   + (f" (all {one_note})" if one_note else ""))
    return out


def render_schedule(plan: DayPlan, now: Optional[datetime], whole_day: bool = False,
                    missed: Optional[Sequence[EventSpan]] = None, label: str = "", config=None, db=None,
                    said: str = "") -> str:
    """The schedule as the user reads it, in plain lines (no bullets). For the day being lived (``now`` given):
    the status line, what's not marked yet right after it, then the rest of the day by blocks (or, asked for, the
    whole day as a record: whole_day_lines). For another day: the day."""
    if now is None or not (plan.day_start <= now < plan.day_end):
        rows = whole_day_lines(plan) if whole_day else plain_rows(schedule_rows(plan))
        head = f"Here's {label or plan.target_date.strftime('%A, %B %-d')}:"
        return "\n".join([head] + rows) if rows else f"Nothing is planned for {label or 'that day'} yet."
    block = thirty_of(now)
    rows = whole_day_lines(plan) if whole_day else plain_rows(schedule_rows(plan, block + HALF))
    upcoming = [s for s in _row_items(plan) if s.start_dt >= block + HALF]
    next_start = min((thirty_of(s.start_dt) for s in upcoming), default=None)
    missed = list(missed if missed is not None else missed_items(plan, now))
    return join_sections(with_missed(status_line(plan, now, next_start, config, db, said), missed),
                         ("The whole day:\n" if whole_day else "") + "\n".join(rows) if rows
                         else "Nothing else is planned today.")


def with_missed(status: str, missed: Sequence[EventSpan]) -> str:
    """A status (a reply's "you're in…" line, or a notice) with what's not marked yet under it, as its own
    section."""
    return join_sections(status, missed_block(missed) if missed else "")


def missed_for(tools, now: datetime, muted_too: bool = False) -> List[EventSpan]:
    """missed_items for a PlanTools' day. A mute (mute_checkins) only quiets what Thirties says by itself, notices
    and check-ins, so those leave muted items out; a view the user asks for (the schedule, /now) passes
    ``muted_too`` and shows where everything stands."""
    mutes = tools.services.mutes if getattr(tools, "services", None) and not muted_too else None
    return missed_items(tools.day_plan, now, skip=(lambda s: mutes.is_muted(s.id, now)) if mutes else None)


def now_section(tools) -> str:
    """For the prompt: where the user is and what's behind, worked out here so the model never counts blocks or
    keeps track of what it already said."""
    plan = tools.day_plan
    now = tools._now()
    if not (plan.day_start <= now < plan.day_end):
        return ""
    missed = missed_for(tools, now, muted_too=True)  # where things stand, muted or not
    askable = {s.id for s in missed_for(tools, now)}
    from thirties_core.item_path import labelled_plan, span_label, tasks_of  # a nested item with its parents
    tasks = tasks_of(tools)
    lines = ["NOW", status_line(labelled_plan(plan, tasks), now)]
    if missed:
        shown = MissedTracker(tools._state_db).titles_shown(now)
        new = [span_label(s, tasks) for s in missed if s.title.strip().lower() not in shown and s.id in askable]
        muted = [span_label(s, tasks) for s in missed if s.id not in askable]
        lines.append("Not marked yet (their time is over, nothing said): "
                     + ", ".join(span_label(s, tasks) for s in missed) + ". "
                     + ("The user has already been shown all of these: don't ask about them again."
                        if not new else "Not shown to the user yet: " + ", ".join(new) + ".")
                     + (" Check-ins are muted for " + ", ".join(muted) + ": never ask about those, though a schedule "
                        "still lists them." if muted else ""))
    return "\n".join(lines) + "\n\n"


# --- where what's carried over went (the morning) ---

def carried_where(plan: DayPlan, item: dict) -> str:
    """Where an item kept from before went, said plainly for the morning: "today at 3:00 PM" when it's on today's
    plan, else when it waits for, with "not today" said outright when its window still holds today (11-12: "kept
    for this week" read as today's, and it wasn't on the schedule). ``item``: a kept leftover ({"title", "when",
    "task_id"})."""
    title = str(item.get("title") or "").strip().lower()
    task_id = item.get("task_id") or ""
    on = sorted((s for s in plan.active_spans() if s.status == SpanStatus.PLANNED and not s.is_envelope
                 and ((task_id and s.task_id == task_id) or s.title.strip().lower() == title)),
                key=lambda s: s.start_dt)
    if on:
        return f"today at {format_clock(thirty_of(on[0].start_dt))}"
    when = str(item.get("when") or "").strip()
    if not when or when == "today":
        return "today, no time yet"
    if when.startswith("this "):
        return f"not today, later {when}"
    return when


# --- what any message may say: plain lines, names not ids, nothing done listed ---

_BULLET_RE = re.compile(r"^(\s*)[-•*]\s+(?=\S)")
# An id noted beside a name: "(id 3f9a…)", "[id …]", "(item id …)", "id=…", "[ID: …]"
_ID_NOTE_RE = re.compile(r"\s*[\[(](?:item |reminder )?id[:=]?\s*[\w:.-]+[\])]|\s*\bid=[\w:.-]+", re.IGNORECASE)
# A token that is an id: hex of 12 or more (with a digit and a letter), a routine's or a calendar's id
_ID_TOKEN_RE = re.compile(r"(?<![\w/.#=-])(?:cal:)?(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{12,40}(?![\w/-])"
                          r"|(?<![\w-])routine-\d{4}-\d{2}-\d{2}-[\w-]+")
# The user asking what got done: then done rows are the answer, and stay
DONE_ASKED_RE = re.compile(r"\b(done|did i|finished|completed|accomplish\w*|got through|ticked)\b", re.IGNORECASE)
_DONE_ROW_RE = re.compile(r"^\s*(?:[-•*]\s*)?\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\s*[–—-].*\((?:(?:all )?done|[^)]*, done)\)\s*$")


def plain_lines(text: str) -> str:
    """Lists as one item per line, with no "- " bullets (11-05, the user's firm preference): in the app's chat as
    on the phone. Code blocks are left as they are; numbered lists keep their numbers."""
    out, code = [], False
    for line in str(text or "").split("\n"):
        if line.lstrip().startswith("```"):
            code = not code
        out.append(line if code else _BULLET_RE.sub(r"\1", line))
    return "\n".join(out)


def names_not_ids(text: str, names: Optional[dict] = None) -> str:
    """Never an id in a message (the user asked what one meant): an id noted beside a name is dropped,
    and an id standing in for a name is replaced by the name (``names``: id → title), or dropped when unknown."""
    names = {str(k): str(v) for k, v in (names or {}).items() if k and v}

    def token(m) -> str:
        found = names.get(m.group(0)) or names.get(m.group(0).removeprefix("cal:"))
        return found if found and not _ID_TOKEN_RE.fullmatch(found) else ""
    lines = []
    for line in str(text or "").split("\n"):
        out = _ID_TOKEN_RE.sub(token, _ID_NOTE_RE.sub("", line))
        if out != line:  # tidy only where an id came out
            out = re.sub(r"\(\s*\)|\[\s*\]", "", out)
            out = re.sub(r"[ \t]+([,.;:!?)])", r"\1", out)
            out = re.sub(r"[ \t]{2,}", " ", out).rstrip(" ,;")
            if not line[:1].isspace():
                out = out.lstrip(" ,;")
        lines.append(out)
    return "\n".join(lines)


def without_done_rows(text: str, keep: Iterable[str] = ()) -> str:
    """A schedule row for something already done is left out of any message (11-02), unless it's one of ``keep``:
    lines Python laid out to be shown (the whole day's record lists what got done on purpose)."""
    keep = {line.strip() for line in keep if line.strip()}
    return "\n".join(line for line in str(text or "").split("\n")
                     if not _DONE_ROW_RE.match(line) or line.strip() in keep)


def known_names(tools) -> dict:
    """Every id Thirties could let slip, with its name: today's items, the lists, and what titles.py knows."""
    names = {}
    try:
        from thirties_core.titles import known_titles
        tasks = list(getattr(getattr(tools, "services", None), "tasks", None) or [])
        names.update(known_titles(getattr(tools, "_state_db", None), tasks))
    except Exception:
        pass
    try:
        for s in tools.day_plan.spans:
            names[s.id] = s.title
    except Exception:
        pass
    return names


def clean_message(text: str, tools=None, said: str = "") -> str:
    """The last step for every message the user reads, from the model or from Python: no done rows (unless
    ``said``, the user's message, asks what got done), names not ids, plain lines. The same in the app and on the
    phone."""
    if not text:
        return text
    from thirties_core.internal_text import strip_internal  # what only the model was fed never reaches the user
    text = strip_internal(text) or "Sorry, I lost my train of thought there. Could you say that again?"
    shown = str(getattr(tools, "shown_schedule", "") or "") if tools is not None else ""
    out = text if DONE_ASKED_RE.search(said or "") else without_done_rows(text, shown.splitlines())
    out = names_not_ids(out, known_names(tools) if tools is not None else None)
    out = plain_lines(out)
    return re.sub(r"\n{3,}", "\n\n", out).strip() or text


# --- placing the schedule in a reply ---

_ROW_RE = re.compile(r"^\s*(?:(?:[-•*]|\d+\.)\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?|\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))"
                     r"\s*(?:–|-|—)")
_STATUS_RE = re.compile(r"^\s*You're in the \d{1,2}:\d{2}")


def place_schedule(reply: str, schedule: str) -> str:
    """A reply that shows the schedule shows Python's layout of it. The model's own rows (it was told to copy the
    text as is, and sometimes rewrites it) are replaced by the canonical text; with no rows at all, it goes at
    the end. Nothing else in the reply changes."""
    if not schedule.strip():
        return reply
    if schedule.strip() in reply:
        return reply
    lines = reply.splitlines()
    first_line = schedule.strip().splitlines()[0]
    marks = [i for i, line in enumerate(lines)
             if _ROW_RE.match(line) or _STATUS_RE.match(line) or line.strip() == MISSED_HEADING
             or line.strip() == first_line.strip()]
    if not marks:
        return (reply.rstrip() + "\n\n" + schedule.strip()).strip()
    start, end = marks[0], marks[0]
    own = {_BULLET_RE.sub("", line).strip() for line in schedule.strip().splitlines() if line.strip()}
    for i in range(marks[0] + 1, len(lines)):  # the run: rows, bullets, indented lines, blank lines between, and
        line = lines[i]                         # the schedule's own lines (its plain Not marked yet ones)
        if i in marks or line.startswith((" ", "\t")) or line.lstrip().startswith(("-", "•", "*")) \
                or _BULLET_RE.sub("", line).strip() in own \
                or line.strip().lower() in ("the whole day:", "coming up:", "next:"):
            end = i
        elif line.strip():
            break
    return "\n".join(lines[:start] + schedule.strip().splitlines() + lines[end + 1:]).strip()


# --- check-in questions that repeat (C5) ---

_QUESTION_RE = re.compile(r"[^.!?\n]*\?")
_CHECKIN_RE = re.compile(r"\b(did|have|were|was|are)\s+you\b|\bany luck\b|\bhow did\b|\bstill on\b", re.IGNORECASE)


def _mentions(sentence: str, title: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(title.strip()) + r"(?!\w)", sentence, re.IGNORECASE))


def drop_repeat_checkins(reply: str, missed: Sequence[EventSpan], already: set) -> tuple:
    """Questions about missed items the user was already shown or asked about are dropped (C5: 10-30 09:37–10:05,
    "Did you get to Make coffee, Pack lunch, Water the plants, or Breakfast?" after each report). Returns the reply
    and the titles a surviving question asked about (they count as shown from now on)."""
    titles = [s.title for s in missed]
    asked: List[str] = []
    out = reply

    for m in list(_QUESTION_RE.finditer(reply)):
        sentence = m.group(0)
        if not _CHECKIN_RE.search(sentence):
            continue
        named = [t for t in titles if _mentions(sentence, t)]
        if not named:
            continue
        if all(t.strip().lower() in already for t in named):
            out = out.replace(sentence, "", 1)
        else:
            asked += named
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if not out:
        return reply, asked
    return out, asked


__all__ = ["MISSED_HEADING", "MissedTracker", "carried_where", "clean_message", "plain_lines", "names_not_ids",
           "without_done_rows", "known_names", "drop_repeat_checkins", "join_sections", "missed_block", "missed_for", "missed_items",
           "missed_lines", "now_section", "with_missed",
           "place_schedule", "render_schedule", "schedule_rows", "status_line", "whole_day_lines"]
