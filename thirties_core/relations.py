"""A place the user gives by another item ("right after the kiln check", "before the shift") is kept as that tie.

Seen in simulated days: the user asked for an errand right after an appointment that has no set end. It went to the
appointment's end, then half an hour later, then under "If there's time" (the user had said "not at a set time",
meaning "tied to the appointment, not to the clock"), then back, and was finally taken off today while still on the
plan. Each change was one the model made on its own reading, and none of them kept what the user had said.

Now the tie is the item's, in Python:

- **kept**: when a move or a new item the user asked for gives its place by another item on the plan ("glaze the
  bowls right after the kiln check", "wax the paddle before the shift"), and the new time keeps that order, the item
  keeps the tie as its facts (after / before, facts.py). Every later placing keeps it (constraints.py), and the
  day's redraw too;
- **moves with it**: when the item it follows ends at another time (moved, or running long), an item right after it
  moves with it, keeping the gap it had;
- **stays on the plan**: an item tied to another isn't pencilled in under "If there's time" ("not at a set time"
  next to "right after the appointment" means the tie, not no time). Taking an item off "If there's time" while it's
  also on today's plan leaves the planned one where it is (if_time.py), and a follow-up named like an item already
  on today is that item, moved, not a second copy (travel.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional, Tuple

from thirties_core.clock import format_clock

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# "right after X", "before X", "once X is done", "as soon as X wraps": the other item's words follow
TIE_RE = re.compile(r"\b(?:(right|just|straight|directly|immediately)\s+)?(after|before|once|as soon as)\s+"
                    r"([^.;!?\n]{1,80}?)(?=\s*(?:[,.;!?\n]|\b(?:then|but|so|or|not)\b|$))", re.IGNORECASE)
# where one thing said ends: a sentence, not a comma ("Can it go right after the check, before the shift?")
_SENTENCE_RE = re.compile(r"[.;!?\n]+")
_POINTER_RE = re.compile(r"\b(?:it|that|this|them|those)\b", re.IGNORECASE)
RIGHT_AFTER = timedelta(minutes=30)  # an item this close after the one it follows sits right after it


def _titles(pt: "PlanTools") -> List[str]:
    return [s.title for s in pt.day_plan.spans if s.title and not getattr(s, "shown_only", False)]


def _named(text: str, title: str, titles: List[str]) -> bool:
    from thirties_core.item_facts import mentions
    from thirties_core.stated_times import loosely_named
    return mentions(text, title, titles) or loosely_named(text, title, titles)


def _anchors(pt: "PlanTools", words: str, mine: "EventSpan") -> List["EventSpan"]:
    """The items on the plan that ``words`` (what follows "after"/"before") name, other than the item itself."""
    from thirties_core.item_facts import match_words, mentions
    from thirties_core.models import SpanStatus
    titles = _titles(pt)
    out = []
    for s in pt.day_plan.spans:
        if s.id == mine.id or s.status == SpanStatus.SKIPPED or getattr(s, "shown_only", False):
            continue
        if mentions(words, s.title, titles) or (match_words(s.title) & match_words(words)
                                               and not any(mentions(words, t, titles) for t in titles
                                                           if t != s.title)):
            out.append(s)
    return out


def said(pt: "PlanTools", span: "EventSpan") -> List[Tuple[str, "EventSpan", bool]]:
    """The ties the user's message gives this item: (after or before, the other item, right after). A sentence
    gives them when it names the item, or points back to it ("can it go right after...") while the message names
    no other item to be placed."""
    message = getattr(pt, "user_message", None)
    if not message or getattr(pt, "by_hand", False):
        return []
    titles = _titles(pt)
    title = pt._title(span)
    out: List[Tuple[str, "EventSpan", bool]] = []
    for sentence in _SENTENCE_RE.split(message):
        ties = list(TIE_RE.finditer(sentence))
        if not ties:
            continue
        anchors = [(m, _anchors(pt, m.group(3), span)) for m in ties]
        anchor_ids = {a.id for _, found in anchors for a in found}
        rest = TIE_RE.sub(" ", sentence)
        about = _named(rest, title, titles) or (
            _POINTER_RE.search(rest) is not None and _named(message, title, titles)
            and not any(_named(rest, s.title, titles) for s in pt.day_plan.spans
                        if s.id != span.id and s.id not in anchor_ids and not s.is_envelope))
        if not about:
            continue
        for m, found in anchors:
            kind = "before" if m.group(2).lower() == "before" else "after"
            for other in found:
                if (kind, other) not in [(k, o) for k, o, _ in out]:
                    out.append((kind, other, bool(m.group(1)) or m.group(2).lower() in ("once", "as soon as")))
    return out


def keep(pt: "PlanTools", span: "EventSpan") -> str:
    """After the user's move or new item: the ties their words give it, where its new time keeps them, kept as its
    facts. Returns a note for the result, or ""."""
    if getattr(pt, "_state_db", None) is None or span.is_envelope or span.source == "calendar":
        return ""
    from thirties_core.plan_log import is_model_call
    if not is_model_call():
        return ""
    ties = [(kind, other, right) for kind, other, right in said(pt, span)
            if (kind == "after" and span.start_dt >= other.end_dt - timedelta(minutes=1))
            or (kind == "before" and span.end_dt <= other.start_dt + timedelta(minutes=1))]
    if not ties:
        return ""
    store = pt._facts()
    facts = store.get(span.id)
    after = sorted(set(facts.after) | {o.id for k, o, _ in ties if k == "after"})
    before = sorted(set(facts.before) | {o.id for k, o, _ in ties if k == "before"})
    if (after, before) == (facts.after, facts.before):
        return ""
    facts.after, facts.before = after, before
    store.set(span.id, facts)
    parts = [f"{'right ' if right else ''}{kind} {pt._title(o)}" for kind, o, right in ties]
    return f"kept {', '.join(parts)}: it moves with it"


def tied(pt: "PlanTools", span: "EventSpan") -> List["EventSpan"]:
    """The items this one is tied to (after or before), still on the plan."""
    if getattr(pt, "_state_db", None) is None:
        return []
    facts = pt._facts().get(span.id)
    ids = set(facts.after) | set(facts.before)
    return [s for s in pt.day_plan.spans if s.id in ids]


def pencil_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why ``span`` can't go under "If there's time": the user tied it to another item (kept, or said this turn)."""
    others = tied(pt, span) + [o for _, o, _ in said(pt, span)]
    if not others:
        return None
    title = pt._title(span)
    return (f"{title} goes {_where(pt, span, others[0])} {pt._title(others[0])}, as the user said, so it stays on "
            f"the plan at {format_clock(span.start_dt)} and moves when {pt._title(others[0])} does: that's the time "
            "they meant, not no time. Leave it on the plan.")


def _where(pt: "PlanTools", span: "EventSpan", other: "EventSpan") -> str:
    return "before" if other.start_dt >= span.end_dt else "right after"


def follow(pt: "PlanTools", lead: "EventSpan", old_end: datetime) -> List[str]:
    """After ``lead`` came to end at another time: each planned item tied to come after it that sat right after it
    (or now overlaps it) moves to keep the gap it had. Notes for the result."""
    from thirties_core.models import SpanStatus
    if getattr(pt, "_state_db", None) is None or lead.end_dt == old_end:
        return []
    store = pt._facts()
    out = []
    for s in sorted(pt.day_plan.spans, key=lambda s: s.start_dt):
        if s.id == lead.id or s.status != SpanStatus.PLANNED or s.actual_start is not None or s.is_envelope \
                or s.source == "calendar" or lead.id not in store.get(s.id).after:
            continue
        gap = s.start_dt - old_end
        if not (timedelta(0) <= gap <= RIGHT_AFTER or s.start_dt < lead.end_dt):
            continue
        start = lead.end_dt + max(gap, timedelta(0))
        start, end = pt._snap(start, start + (s.end_dt - s.start_dt))
        if start == s.start_dt or start < pt._now() - timedelta(minutes=1):
            continue
        s.start_dt, s.end_dt = start, end
        s.given_start = s.given_end = None
        pt._persist(s)
        out.append(f"{pt._title(s)} now {format_clock(start)}, right after {pt._title(lead)}")
    if out:
        pt.day_plan.spans.sort(key=lambda s: s.start_dt)
    return out


__all__ = ["follow", "keep", "pencil_refused", "said", "tied", "TIE_RE"]
