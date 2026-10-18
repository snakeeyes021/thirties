"""Deterministic parsing of clock times and durations from model tool calls.

Models speak in natural clock strings ("4:30 PM", "noon") and natural
durations ("2 hours", "90 min"); this module turns them into real datetimes
on a diurnal day, so no model ever does time arithmetic (AGENTS.md §3.2).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import List, Optional, Sequence, Tuple


class ClockError(ValueError):
    """A time or duration the core cannot interpret unambiguously."""


_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?\s*$|^\s*(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)


def parse_clock(text: str) -> time:
    """Parse '4:30 PM', '4pm', '16:30', '07:00', 'noon' or 'midnight' into a time.

    Bare hours or 'h:mm' without AM/PM are rejected for hours 1-12 unless written
    in 24-hour style with a leading zero ('07:00') or above 12 ('16:30').
    """
    raw = str(text).strip().lower()
    if raw in ("noon", "midday"):
        return time(12, 0)
    if raw == "midnight":
        return time(0, 0)
    m = _CLOCK_RE.match(raw)
    if not m:
        raise ClockError(f"Could not read the time '{text}'. Use a clock time like '4:30 PM'.")
    if m.group(1) is not None:
        hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if not 1 <= hour <= 12:
            raise ClockError(f"'{text}' is not a valid 12-hour time.")
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
    else:
        hour, minute = int(m.group(4)), int(m.group(5))
        is_24h_style = hour == 0 or hour > 12 or m.group(4).startswith("0")
        if not is_24h_style:
            raise ClockError(f"'{text}' is ambiguous. Say AM or PM (for example '{hour}:{minute:02d} PM').")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ClockError(f"'{text}' is not a valid time.")
    return time(hour, minute)


def resolve_clock(text: str, day_start: datetime, after: Optional[datetime] = None) -> datetime:
    """Place a clock time on the diurnal day that starts at day_start.

    Without ``after``, returns the occurrence inside [day_start, day_start + 24h).
    With ``after`` (used for end times), returns the first occurrence strictly after it,
    so '10:30 PM' to '6:30 AM' ends on the following morning.
    """
    t = parse_clock(text)
    tz = day_start.tzinfo
    anchor = after if after is not None else day_start
    candidate = datetime.combine(anchor.date(), t, tzinfo=tz)
    if after is not None:
        while candidate <= after:
            candidate = datetime.combine(candidate.date() + timedelta(days=1), t, tzinfo=tz)
        return candidate
    while candidate < day_start:
        candidate = datetime.combine(candidate.date() + timedelta(days=1), t, tzinfo=tz)
    return candidate


_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|h|minutes|minute|mins|min|m)(?![a-z])", re.IGNORECASE)


_WORD_NUMBERS = {"a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10", "couple": "2", "a couple of": "2",
                 "a couple": "2", "half an": "0.5", "half a": "0.5"}


def _normalize_duration_words(raw: str) -> str:
    raw = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:chunks?|thirties|thirtys|blocks?)\b", lambda m: f"{float(m.group(1)) * 30:g} minutes", raw)
    raw = re.sub(r"\b(?:a )?half(?: an)? hour\b", "30 minutes", raw)
    raw = re.sub(r"\bhour and a half\b", "1.5 hours", raw)
    for words, num in sorted(_WORD_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        raw = re.sub(rf"\b{words}\b(?=\s+(?:hours?|hrs?|minutes?|mins?|chunks?|thirties|blocks?))", num, raw)
    raw = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:chunks?|thirties|thirtys|blocks?)\b", lambda m: f"{float(m.group(1)) * 30:g} minutes", raw)
    return re.sub(r"\b(?:at least|about|around|roughly|maybe|like|or so|ish)\b", "", raw).strip()


def parse_duration_minutes(value) -> int:
    """Parse '2 hours', '90 min', '1h30m', '1.5 hours', '3 chunks', 'an hour', 'half an hour' or minutes."""
    if isinstance(value, (int, float)):
        minutes = float(value)
    else:
        raw = _normalize_duration_words(str(value).strip().lower())
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            minutes = float(raw)
        else:
            parts = _DURATION_PART_RE.findall(raw)
            if not parts or _DURATION_PART_RE.sub("", raw).strip(" ,and"):
                raise ClockError(f"Could not read the duration '{value}'. Use something like '90 minutes' or '2 hours'.")
            minutes = sum(float(n) * (60 if unit.startswith("h") else 1) for n, unit in parts)
    if minutes <= 0:
        raise ClockError(f"Duration must be positive, got '{value}'.")
    return int(round(minutes))


def format_clock(dt: datetime) -> str:
    """'4:30 PM' style, without a leading zero."""
    return dt.strftime("%I:%M %p").lstrip("0")


SHORT = timedelta(minutes=30)


def thirty_of(dt: datetime) -> datetime:
    """The start of the thirty (clock half hour) a time falls in."""
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def thirty_ceil(dt: datetime) -> datetime:
    """The end of the thirty a time falls in (the time itself if it's already on a thirty)."""
    start = thirty_of(dt)
    return dt if start == dt else start + SHORT


def group_short_items(items: Sequence[Tuple[datetime, datetime, str, str]]) -> List[Tuple[datetime, datetime, str, str]]:
    """Items under 30 minutes belong to their thirty: consecutive ones in the same thirty become one
    row at the thirty's start ("5:00 – Feed cats, Message Brad"), ending when the thirty ends."""
    out: List[Tuple[datetime, datetime, str, str]] = []
    grouped: List[bool] = []  # parallel to out: is this row a thirty of short items?
    for start, end, title, note in items:
        if end - start < SHORT and thirty_of(start) == thirty_of(end - timedelta(seconds=1)):
            block = thirty_of(start)
            label = f"{title} ({note})" if note else title
            if out and grouped[-1] and out[-1][0] == block:
                out[-1] = (block, block + SHORT, f"{out[-1][2]}, {label}", "")
                continue
            out.append((block, block + SHORT, label, ""))
            grouped.append(True)
        else:
            out.append((start, end, title, note))
            grouped.append(False)
    return out


def agenda_lines(items: Sequence[Tuple[datetime, datetime, str, str]], numbered: bool = False) -> List[str]:
    """A plan as a reader expects it: '- 4:00 PM – Feed cat', one line per item, in order.

    Items are (start, end, title, note). Each item ends when the next one starts, so an end
    time is added only where it isn't implied: before a gap or under an overlap (the last
    item's end isn't worth the noise). Items under 30 minutes are shown by their thirty.
    """
    items = group_short_items(items)
    lines = []
    for i, (start, end, title, note) in enumerate(items):
        next_start = next((s for s, _, _, _ in items[i + 1:] if s > start), None)
        extras = [note] if note else []
        if next_start is not None and next_start != end:
            extras.append(f"until {format_clock(end)}")
        prefix = f"{i + 1}. " if numbered else "- "
        lines.append(f"{prefix}{format_clock(start)} – {title}" + (f" ({', '.join(extras)})" if extras else ""))
    return lines


_TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9]):([0-5]\d)\s?(AM|PM)\b")
_DAY_HINT_RE = re.compile(r"\b(?:tomorrow|yesterday|tonight|today|mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|"
                          r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d", re.IGNORECASE)


def trim_meridiems(text: str, now: Optional[datetime] = None) -> str:
    """Say AM/PM only where it could be mistaken.

    Kept on the first time unless it's in the same half of the day as now, wherever the
    half changes from the previous time (crossing noon or midnight), wherever a day or
    date is named in between, and always on 12 o'clock. "Dinner 6:00 PM – 7:00 PM" said
    in the afternoon becomes "Dinner 6:00 – 7:00".
    """
    out: List[str] = []
    pos = 0
    last: Optional[str] = None
    now_half = None if now is None else ("AM" if now.hour < 12 else "PM")
    for m in _TIME_RE.finditer(text):
        half = m.group(3).upper()
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[pos:m.start()] if last is not None else text[line_start:m.start()]
        if int(m.group(1)) == 12 or _DAY_HINT_RE.search(before):
            keep = True
        elif last is None:
            keep = now_half is None or half != now_half
        else:
            keep = half != last
        out.append(text[pos:m.start()])
        out.append(m.group(0) if keep else f"{m.group(1)}:{m.group(2)}")
        pos = m.end()
        last = half
    out.append(text[pos:])
    return "".join(out)


def format_duration(minutes: int) -> str:
    """'2h 30m', '45m', '3h'."""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


_SHIFT_SIGN_RE = re.compile(r"^\s*([+-])\s*")


def parse_shift_minutes(value) -> int:
    """Parse a signed shift: '+1 hour', '-30 min', '+90', '1 hour later', '15 minutes earlier'."""
    if isinstance(value, (int, float)):
        if value == 0:
            raise ClockError("Shift must not be zero.")
        return int(round(value))
    raw = str(value).strip().lower()
    sign = 0
    m = _SHIFT_SIGN_RE.match(raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        raw = raw[m.end():]
    for word, word_sign in (("later", 1), ("earlier", -1)):
        if raw.endswith(word):
            if sign and sign != word_sign:
                raise ClockError(f"Shift '{value}' contradicts itself.")
            sign = word_sign
            raw = raw[: -len(word)].strip()
    if not sign:
        raise ClockError(f"Shift '{value}' needs a direction: '+1 hour' (later) or '-30 minutes' (earlier).")
    return sign * parse_duration_minutes(raw)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]


def parse_day(text, today):
    """Resolve 'today', 'tomorrow', 'yesterday', 'monday', 'next friday', 'last tuesday',
    'oct 20', 'october 20', '10/20' or '2026-10-20' relative to the diurnal date ``today``.

    A bare weekday means the next one on or after today; 'last <weekday>' the most recent
    before today. Month-day without a year picks the nearest such date (past or future).
    """
    from datetime import date as _date
    if isinstance(text, _date):
        return text
    raw = str(text or "").strip().lower().replace(",", " ")
    raw = re.sub(r"\s+", " ", raw)
    if raw in ("", "today", "tonight", "this evening", "this morning", "this afternoon"):
        return today
    if raw in ("tomorrow", "tomorrow night", "tomorrow morning"):
        return today + timedelta(days=1)
    if raw in ("yesterday", "last night"):
        return today - timedelta(days=1)
    m = re.fullmatch(r"(?:in )?(\d+) days?(?: from now)?", raw)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(\d+) days? ago", raw)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(next |this |last |on )?([a-z]+)", raw)
    if m:
        name = next((w for w in _WEEKDAYS if w.startswith(m.group(2)) and len(m.group(2)) >= 3), None)
        if name:
            target = _WEEKDAYS.index(name)
            delta = (target - today.weekday()) % 7
            if m.group(1) == "last ":
                return today - timedelta(days=(today.weekday() - target) % 7 or 7)
            if m.group(1) == "next " and delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    month = day_num = None
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?", raw) or re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)", raw)
    if m:
        a, b = m.group(1), m.group(2)
        word, num = (a, b) if not a.isdigit() else (b, a)
        month = next((i + 1 for i, mo in enumerate(_MONTHS) if mo.startswith(word[:3]) and len(word) >= 3), None)
        day_num = int(num)
    m2 = re.fullmatch(r"(\d{1,2})/(\d{1,2})", raw)
    if m2:
        month, day_num = int(m2.group(1)), int(m2.group(2))
    if month and day_num:
        candidates = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(_date(year, month, day_num))
            except ValueError:
                pass
        if candidates:
            return min(candidates, key=lambda d: abs((d - today).days))
    raise ClockError(f"Could not read the day '{text}'. Use 'today', 'tomorrow', a weekday, or a date like 'Oct 20'.")
