"""An ask to be left alone, read from the user's own words and kept by Python.

"Leave me be until 3", "nothing from you before my next message", "quiet for an hour please": a clear ask to be quiet is
recorded at the start of the turn, before the model says anything, as a hold on messages (holds.py). The model
can't forget it, refuse it, or end it early. While it runs, nothing Thirties would say by itself goes out: start
bells, check-ins, nudges, the evening catch-up, offers. A reminder the user set for a time still rings, unless the
ask said nothing at all should come ("not even reminders").

It ends:
- "until I write" (or no end given): the next message from the user ends it, in the app or on the phone;
- "until 3", "till dinner", "for an hour", "for the rest of the day": at that time;
- and never later than the end of the day.
An ask whose end has already passed records nothing.

A reply may say it will keep quiet only when a hold is on (``without_false_quiet``): a promise of quiet with nothing
recorded is the claim the user would catch out at the next bell.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, List, Optional

# --- the ask itself ---
_THINGS = (r"(?:pings?|pinging|reminders?|reminding|notifications?|notifs?|messages?|messaging|nudges?|nudging|"
           r"check-?ins?|alerts?|buzz(?:es|ing)?|bells?|interruptions?|updates?)")
_VERBS = r"(?:ping|remind|message|msg|text|nudge|bother|disturb|notify|buzz|interrupt|bug|check in on|check on)"
_ASK_RE = re.compile(
    r"\b(?:don'?t|dont|do not|no need to|you don'?t need to|you dont need to)\s+(?:need to\s+|have to\s+|bother to\s+)?"
    + _VERBS + r"\s+(?:me|us)\b|"
    r"\b(?:stop|hold|pause|mute|silence|quiet|cut|kill)\s+(?:the |all |all the |your |my |any |every |each )?" + _THINGS + r"\b|"
    r"\b(?:keep|stay|go)\s+(?:it |things |everything )?quiet\b|\bquiet (?:please|pls|plz|for|until|till|til)\b|"
    r"\bradio silence\b|\bleave me (?:alone|be)\b|\bsilent mode\b|\bnothing (?:at all )?from you\b|"
    r"\bnot a (?:peep|word) (?:from you|out of you)\b", re.IGNORECASE)
# "no nudges": an ask when it opens the sentence or comes with an end or a please ("there were no messages from the
# studio" is news, not an ask)
_NO_RE = re.compile(r"\bno (?:more )?(?:\w+ )?" + _THINGS + r"\b", re.IGNORECASE)
_NO_OPENS_RE = re.compile(r"^\W*(?:(?:and|so|also|ok|okay|oh|but|pls|please|plz|hey)\W+)*no\b", re.IGNORECASE)
_NO_WITH_RE = re.compile(r"\b(?:till|until|til|unless|before|for (?:the next |an?|\d|one|two|half|the rest)|"
                         r"please|pls|plz|thanks|thx|ty)\b|🙏", re.IGNORECASE)
# about one item, not the whole day ("no reminders about the kiln" is that item's)
_ONE_ITEM_RE = re.compile(r"^\s*(?:about|regarding|on|of)\s+(?!anything|everything|stuff|things|it all|any\b|the rest|"
                          r"today)\w|^\s*for\s+(?:the|my)\s+(?!next|rest|day|afternoon|evening|morning|moment)\w",
                          re.IGNORECASE)
# a question about the past, or a request for one, isn't an ask for quiet
_NOT_ASK_RE = re.compile(r"^\s*(?:why|how come|did|didn'?t|were|was|wasn'?t|when will)\b", re.IGNORECASE)
_STRICT_RE = re.compile(r"\bnothing at all\b|\bnot even (?:\w+ )?(?:reminders?|the reminders?|alarms?)\b|"
                        r"\babsolutely nothing\b|\bincluding (?:the |my )?reminders\b|\bno anything\b|"
                        r"\b(?:no|nothing)\b[^.?!\n]{0,24}\bat all\b", re.IGNORECASE)

# --- its end ---
_TILL = r"(?:till|until|til|'?til|unless|before)"
_USER_RE = re.compile(_TILL + r"\s+(?:i|I)\s*(?:'m|am)?\s*(?:text|write|message|msg|ping|say|tell|get back|reach out|"
                      r"come back|check in|ask|back|hit you|holler|answer|reply)|"
                      r"\buntil you hear from me\b|\btill you hear from me\b|\bi'?ll (?:text|message|write|ping) "
                      r"(?:you|u) when\b", re.IGNORECASE)
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\b\.?|(noon|midday)"
_AT_RE = re.compile(_TILL + r"\s+(?:about |around |like )?(?:" + _CLOCK + r")", re.IGNORECASE)
_MEAL_RE = re.compile(r"\b(till|until|til|before|after)\s+(?:i(?:'ve| have)?\s+(?:had|eaten|eat|have|get)\s+)?"
                      r"(?:my |some )?(lunch|dinner|supper|brunch)\b", re.IGNORECASE)
_FOR_RE = re.compile(r"\bfor (?:the next |another |about |around )?(an?|one|two|three|four|half an?|\d+(?:\.\d+)?)\s*"
                     r"(hours?|hrs?|h|minutes?|mins?)\b", re.IGNORECASE)
_REST_RE = re.compile(r"\b(?:for |all )?(?:the rest of (?:the |to)?day|rest of today|for today|all day|tonight|"
                      r"the rest of the (?:afternoon|evening))\b", re.IGNORECASE)
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}


@dataclass
class QuietAsk:
    ends: datetime            # the latest it lasts (the end of the day for "until I write")
    until_user: bool          # the user's next message ends it
    strict: bool              # nothing at all: reminders the user set wait too


def asks_quiet(said: Optional[str]) -> bool:
    """Whether the message asks, outright, for Thirties to stop sending things by itself for a while."""
    return _match(said or "") is not None


def _match(said: str):
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", said):
        if _NOT_ASK_RE.search(sentence):
            continue
        for m in _ASK_RE.finditer(sentence):
            if not _ONE_ITEM_RE.search(sentence[m.end():]):
                return m
        for m in _NO_RE.finditer(sentence):
            if (_NO_OPENS_RE.search(sentence[:m.start() + 3]) or _NO_WITH_RE.search(sentence[m.end():])) \
                    and not _ONE_ITEM_RE.search(sentence[m.end():]):
                return m
    return None


def _clock_after(hour: int, minute: int, meridiem: str, now: datetime) -> Optional[datetime]:
    """A clock time said in an ask, on today's clock: "3" after 9 AM is 3 PM; "11" at 9 AM is 11 AM."""
    if hour > 23 or minute > 59:
        return None
    if meridiem:
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
        options = [hour]
    else:
        options = [hour] if hour > 12 or hour == 0 else [hour % 12, hour % 12 + 12]
    for h in options:
        at = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if at > now:
            return at
    return None


def _meal_time(plan, meal: str, after: bool, now: datetime) -> Optional[datetime]:
    """The meal on today's plan (its start, or its end for "after"), else when its usual window opens (meals.py)."""
    from thirties_core.meals import MEAL_WINDOWS
    spans = [s for s in getattr(plan, "spans", []) if meal in (s.title or "").lower() and s.end_dt > now
             and not getattr(s, "is_envelope", False)]
    if spans:
        span = min(spans, key=lambda s: s.start_dt)
        return span.end_dt if after else span.start_dt
    opens = MEAL_WINDOWS.get(meal, (time(12, 0), time(13, 0)))[0]
    at = now.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
    return at + timedelta(hours=1) if after else at


def parse(said: Optional[str], now: datetime, plan=None, day_end: Optional[datetime] = None) -> Optional[QuietAsk]:
    """The ask for quiet in the user's message, with its end; None when there's none, or its end has passed."""
    said = said or ""
    if _match(said) is None:
        return None
    last = day_end or getattr(plan, "day_end", None) or now.replace(hour=23, minute=59)
    strict = bool(_STRICT_RE.search(said))
    if _USER_RE.search(said):
        return QuietAsk(last, True, strict)
    ends: Optional[datetime] = None
    m = _AT_RE.search(said)
    if m is not None:
        if m.group(4):
            ends = now.replace(hour=12, minute=0, second=0, microsecond=0)
        else:
            ends = _clock_after(int(m.group(1)), int(m.group(2) or 0), m.group(3) or "", now)
        if ends is None or ends <= now:
            return None
    if ends is None:
        m = _MEAL_RE.search(said)
        if m is not None:
            ends = _meal_time(plan, m.group(2).lower(), m.group(1).lower() == "after", now)
            if ends is None or ends <= now:
                return None
    if ends is None:
        m = _FOR_RE.search(said)
        if m is not None:
            word = m.group(1).lower()
            amount = 0.5 if word.startswith("half") else float(_NUMBERS.get(word, word) if word in _NUMBERS
                                                                else word)
            unit = m.group(2).lower()
            ends = now + (timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount))
    if ends is None and _REST_RE.search(said):
        ends = last
    if ends is None:  # no end given: until they write again
        return QuietAsk(last, True, strict)
    return QuietAsk(min(ends, last), False, strict)


# --- recording it ---

def record(db, ask: QuietAsk, now: datetime, said: str) -> dict:
    """The ask as a hold on messages (holds.py), marked as the user's own: it ends on their next message when it
    waits for that, it lets the reminders they set through unless it's strict, and it ends without a message."""
    from thirties_core import holds
    data = holds.start(db, now, ask.ends)
    data.update({"from_words": True, "until_user": ask.until_user, "strict": ask.strict, "clock": False,
                 "ask": (said or "")[:300]})
    db.set_sync_meta(holds.KEY, json.dumps(data))
    return data


def describe(data: dict) -> str:
    """What's recorded, in the user's terms (for the model to say)."""
    from thirties_core.clock import format_clock
    if data.get("until_user"):
        when = "until the user writes again"
    else:
        try:
            when = "until " + format_clock(datetime.fromisoformat(data["ends"]))
        except (KeyError, ValueError):
            when = "for now"
    rings = ("nothing goes out, not even reminders they set" if data.get("strict") else
             "reminders they set for a time still ring")
    return (f"Quiet {when}: no bells, check-ins or nudges ({rings}). It ends on its own; say so plainly and "
            "briefly, without naming any setting.")


def at_turn_start(db, said: str, now: datetime, plan=None) -> Optional[dict]:
    """The user wrote: an "until I write" quiet ends, and a new ask in this message is recorded. Returns the hold
    recorded from this message, if any."""
    if db is None:
        return None
    from thirties_core import holds
    data = holds.active(db, now)
    if data is not None and data.get("from_words") and data.get("until_user"):
        holds.end(db, now, "wrote")  # they're back: no message to say so, they're talking already
    ask = parse(said, now, plan)
    if ask is None:
        return None
    return record(db, ask, now, said)


def recorded_for(db, said: Optional[str], now: datetime) -> Optional[dict]:
    """The hold recorded from this very message, if it's still on."""
    from thirties_core import holds
    data = holds.active(db, now) if db is not None else None
    if data is None or not data.get("from_words") or not said:
        return None
    return data if data.get("ask") == said[:300] else None


# --- what a reply may say about it ---

_CLAIM_RE = re.compile(
    r"\b(?:keep|keeping|kept)\s+(?:things|it|everything|all)\s+(?:quiet|silent)\b|\b(?:quiet|silent)\s+(?:until|till|"
    r"til|for)\b|\bhold(?:ing)?\s+(?:all\s+|your\s+|off\s+(?:on\s+)?)?(?:the\s+)?(?:notifications|messages|pings|"
    r"reminders|check-?ins|nudges)\b|\bput\s+a\s+hold\b|\bon hold\b|\bwon'?t\s+(?:hear\s+(?:a\s+peep|from\s+me)|"
    r"ping|bother|disturb|remind|message|nudge|interrupt|buzz)\b|\bno\s+more\s+(?:pings|reminders|notifications|"
    r"messages|nudges|check-?ins)\b|\bI'?ll\s+(?:stay|be|keep)\s+(?:quiet|silent)\b|\b(?:paused|muted|silenced)\s+"
    r"(?:all\s+|your\s+|the\s+)?(?:notifications|messages|pings|reminders|check-?ins|nudges)\b|\b(?:stay|go)\s+"
    r"quiet\b|\bradio silence\b|\bleave you (?:alone|be)\b|\bquiet until\b", re.IGNORECASE)


def claims_quiet(text: str) -> bool:
    return any(_CLAIM_RE.search(s) and not s.rstrip().endswith("?") for s in _sentences(text))


def _sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def without_false_quiet(text: str, db, now: datetime, mutes_on: bool = False) -> str:
    """The reply with each sentence promising quiet taken out when no quiet is recorded (no hold, no check-in
    pause): it may only say what Python is keeping."""
    from thirties_core import holds
    if not text or not claims_quiet(text):
        return text
    if mutes_on or (db is not None and holds.active(db, now) is not None):
        return text
    lines = []
    for line in text.split("\n"):
        parts = re.split(r"(?<=[.!?])\s+", line)
        kept = [p for p in parts if not (_CLAIM_RE.search(p) and not p.rstrip().endswith("?"))]
        if parts and not kept and line.strip():
            continue
        lines.append(" ".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or "OK."


__all__ = ["QuietAsk", "asks_quiet", "parse", "record", "describe", "at_turn_start", "recorded_for",
           "claims_quiet", "without_false_quiet"]
