"""A reply that says an item's time was set, when it wasn't.

The moves check (FrontierBrain._unmade_moves) reads "moved", "shifted", "pushed". A time the user gave was also
claimed as "locked in", "all set", "set for 1:10", "updated" or "fixed" over a change the tools refused, and the
reply passed: the user asked three and four times over. Now a sentence that claims a time for an item, where a change
to that item was refused this turn and nothing about its time changed since, is sent back like an unmade move, item
by item (claim_parts.py).

A claim about an item no change this turn touched is a statement of where it is, which the times check reads
(FrontierBrain._wrong_times); a question, an offer, or a sentence that says it didn't happen is no claim.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from thirties_core.brains.frontier import FrontierBrain

# a time said to be set: "locked in", "all set", "set for 1:10", "is now at", "updated", "fixed", "sorted"
TIME_CLAIM_RE = re.compile(r"\b(?:locked(?:\s+it)?\s+in|all set|(?:is|are|'s|now|been)\s+set|set\s+(?:for|to|at|"
                           r"from)|now\s+(?:at|starts?|runs?|begins?)|updated|changed|fixed|sorted|confirmed|in place|"
                           r"(?:got|have|has|put)\s+(?:it|that|them|\w+)\s+(?:down\s+)?(?:at|for|from))\b",
                           re.IGNORECASE)
# a clock time in the claim: "1:10", "4pm", "noon"
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*[ap]\.?m\b|\bnoon\b", re.IGNORECASE)

# a refusal because the item already had those times
_NOTHING_RE = re.compile(r"\bNothing to change\b")
# the tools that change an item's time
_CHANGING = {"update_event", "add_event", "put_routine", "swap_events", "push_back", "doing_now", "still_doing"}


def _refused_items(brain: "FrontierBrain", turn_start: int) -> Dict[str, str]:
    """Titles (lowercased) of items whose change was refused this turn with nothing after it that worked, with the
    refusal."""
    from thirties_core.plan_tools import NOT_DONE
    refused: Dict[str, str] = {}
    pending: List[Dict[str, Any]] = []  # the calls asked for, in order: a result carries only the tool's name
    for m in brain.messages[turn_start - 1:]:
        if m.get("role") == "assistant":
            pending += [c for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
            continue
        if m.get("role") != "tool":
            continue
        call = next((c for c in pending if c.get("name") == m.get("name")), None)
        if call is not None:
            pending.remove(call)
        if m.get("name") not in _CHANGING:
            continue
        args = m.get("arguments") if isinstance(m.get("arguments"), dict) else (call or {}).get("arguments")
        key = brain._item_of({"arguments": args if isinstance(args, dict) else {}})
        if key:
            refused[key] = str(m.get("content") or "")
    return {k: v for k, v in refused.items() if v.startswith(NOT_DONE)}


def unmade_time_claims(brain: "FrontierBrain", turn_start: int, reply: str) -> List[str]:
    """"Stroll by the canal is still in the 1:00 PM block" per item the reply says has its time set, where a change
    to it was refused this turn and nothing about its time changed."""
    from thirties_core.shown_times import shown_start, start_exact
    from thirties_core.clock import format_clock
    refused = _refused_items(brain, turn_start)
    if not refused:
        return []
    before: Optional[Dict[str, Any]] = getattr(brain, "_times_before", None)
    out: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
        m = TIME_CLAIM_RE.search(sentence)
        if m is None or not _CLOCK_RE.search(sentence) or sentence.rstrip().endswith("?") \
                or brain._ADMITS_RE.search(sentence):
            continue
        lead = sentence[:m.start()]
        if brain._NEGATED_RE.search(lead[-40:]) or brain._NOT_YET_RE.search(lead):
            continue
        for span in brain._named(sentence):
            why = refused.get(brain.tools._title(span).strip().lower()) or refused.get(span.title.strip().lower())
            if span.is_envelope or why is None:
                continue
            if _NOTHING_RE.search(why) and shown_start(span) == span.start_dt:
                continue  # it was already where it shows: "set for 5:00" is true
            if before is not None and before.get(span.id) not in (None, (span.start_dt, span.end_dt)):
                continue  # it did change this turn
            where = (f"still at {format_clock(span.start_dt)}" if start_exact(span) else
                     f"still in the {format_clock(shown_start(span))} block")
            out.append(f"{brain.tools._title(span)} is {where} (that change was refused)")
    return list(dict.fromkeys(out))[:3]


__all__ = ["TIME_CLAIM_RE", "unmade_time_claims"]
