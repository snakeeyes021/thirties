"""One item, one place on the plan: copies of the same item that the plan shouldn't show twice.

An item can end up on the day twice without anyone asking for a second session:

- **Stretched over its own copy.** Clearing time out of the middle of an item splits it in two; stretching one part
  back over the gap leaves the other part sitting inside it, the same item listed twice at once. A copy whose whole
  time lies inside another copy of the same item is folded into it (merge_covered).
- **Started by its list id.** When doing_now names a list line whose item sits later on the plan, that planned
  copy moves to now: the id finds it (planned_copy), and no second copy is added.
- **Taking a copy off.** A copy inside another copy's time adds nothing, so it can come off whenever the plan
  holds it (covered); and when the user says an item is on the plan twice ("the trimming is on there twice"), the
  extra copy of the item their words fit comes off, even when the word they used is one another item shares
  (said_doubled).

A second session asked for with again=true, at a time of its own, stays: nothing here folds copies that
only touch or sit apart.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# the user saying an item is on the plan more than once
TWICE_RE = re.compile(r"\b(?:twice|two times|two of (?:it|them|those)|in there two|double[ds]?|doubled up|"
                      r"duplicat\w*|listed again|on there again|shows? up again|(?:there|in) two places)\b",
                      re.IGNORECASE)


def _same_item(a: "EventSpan", b: "EventSpan") -> bool:
    from thirties_core.item_facts import item_name
    if a.task_id and b.task_id:
        return a.task_id == b.task_id
    return item_name(a.title) == item_name(b.title)


def _copies(pt: "PlanTools", span: "EventSpan") -> List["EventSpan"]:
    """The other copies of this item still to do today (not calendar events, not work or sleep)."""
    from thirties_core.models import SpanStatus
    return [s for s in pt.day_plan.spans if s.id != span.id and s.status == SpanStatus.PLANNED
            and s.source != "calendar" and not s.is_envelope and not getattr(s, "shown_only", False)
            and _same_item(s, span)]


def covered(pt: "PlanTools", span: "EventSpan") -> bool:
    """Whether another copy of this item, still to do, spans the whole of this copy's time."""
    return any(c.start_dt <= span.start_dt and span.end_dt <= c.end_dt for c in _copies(pt, span))


def merge_covered(pt: "PlanTools", span: "EventSpan") -> List[str]:
    """Fold the copies of this item that lie wholly inside its time into it; notes for the tool result."""
    from thirties_core.models import SpanStatus
    if span.status != SpanStatus.PLANNED or span.source == "calendar" or span.is_envelope:
        return []
    notes = []
    for c in _copies(pt, span):
        if span.start_dt <= c.start_dt and c.end_dt <= span.end_dt:
            pt._drop(c)
            notes.append(f"its other copy, {pt.describe(c)}, was inside this time, so it's one item now")
    return notes


def said_doubled(pt: "PlanTools", span: "EventSpan") -> bool:
    """True when this turn's message calls an item doubled and the item meant is this span's: it has
    another copy, and of the items the words fit, it's the only one with copies."""
    message = getattr(pt, "user_message", None)
    if not message or not TWICE_RE.search(message) or not _copies(pt, span):
        return False
    from thirties_core.item_facts import match_words
    said = match_words(message)
    if not match_words(span.title) & said:
        return False
    fitting = [s for s in pt.day_plan.spans if s.source != "calendar" and match_words(s.title) & said]
    with_copies = {s.task_id or s.title for s in fitting if _copies(pt, s)}
    return len(with_copies) == 1


def extra_copy(pt: "PlanTools", span: "EventSpan") -> bool:
    """Whether this copy can come off as the plan's own duplicate: covered by another copy, or the one the user
    says is there twice."""
    return covered(pt, span) or said_doubled(pt, span)


def planned_copy(pt: "PlanTools", item: object) -> Optional["EventSpan"]:
    """The planned copy of the item given by its list line's id (or words), for doing_now to move to now: the only
    one, or of several the one starting nearest to now. None when the day doesn't have it planned."""
    from thirties_core.titles import is_raw_id
    ref = str(item or "").strip()
    if not ref:
        return None
    try:
        title, task_id = pt._words_for(ref)
    except Exception:
        return None
    copies = pt._planned_copies(pt.day_plan, title, task_id if is_raw_id(ref) else None)
    if not copies:
        return None
    now = pt._now()
    return min(copies, key=lambda s: abs((s.start_dt - now).total_seconds()))


__all__ = ["TWICE_RE", "covered", "extra_copy", "merge_covered", "planned_copy", "said_doubled"]
