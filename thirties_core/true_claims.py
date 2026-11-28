"""What a reply check may read as a claim, so a check fires only on a sentence that is false at the end of the turn.

Five readings that called true sentences false, each taken out:

- **A clause about what an earlier turn changed says nothing of this turn.** A call that went through before this
  turn (a plan, list or goal change, with its item or goal name) made that change then; a draft describing it in the
  past tense claims no change now (``earlier_call_names``, ``claim_is_earlier``).
- **An item a sentence names is the one whose whole title it says.** A short item's title inside a longer item's
  whole title is part of that name (``without_inner_titles``).

- **A block phrase names a block, never an item's time.** "You're in the 2:00 PM block, with Patch the spare tubes
  next" says where the user is; the time in it was read as a claim about the item beside it (``without_block_times``).
- **An item that has been handled isn't one that failed to move.** A skipped, done or left-unknown item never gets
  an "is still at ...; I couldn't move it" line, however long ago it was handled (``movable``).
- **Times an outcome recorded are the item's real times.** One marked done with no times known, one skipped, one
  left unknown has no place on the plan a time could be wrong about (``time_is_open``); one with recorded times
  is judged by those and by the forms the schedule shows them in (``shown_forms``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Set, Tuple

_CLOCK = r"\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\b\.?)?"
# "the 2:00 PM block", "the 2:00 – 2:30 block", "the block at 2:30", "the block starting at 2:30"
_BLOCK_PHRASE_RE = re.compile(
    rf"(?:{_CLOCK}\s*(?:–|-|to)\s*)?{_CLOCK}\s+blocks?\b|\bblocks?\s+(?:(?:at|from|of|starting(?:\s+at)?|"
    rf"beginning(?:\s+at)?)\s+){_CLOCK}", re.IGNORECASE)


def without_block_times(text: str) -> str:
    """The text without the times of block phrases: a block is named by its time, and that is no claim about
    an item."""
    return _BLOCK_PHRASE_RE.sub(" ", text or "")


def without_inner_titles(sentence: str, named: list, titles: dict) -> list:
    """The items a sentence names by their whole titles, without one whose title is only part of a longer title the
    sentence says whole: "Stencil the new shop sign is marked done" names that item, never also one called "Stencil".
    ``titles`` maps an item's id to the title the sentence is read against."""
    def found(title: str) -> list:
        return [m.span() for m in re.finditer(rf"(?<![\w'’]){re.escape(title)}(?![\w'’])", sentence, re.IGNORECASE)]
    spans = {s.id: found(titles[s.id]) for s in named}
    out = []
    for s in named:
        inside = all(any(o[0] <= a and b <= o[1] and (o[1] - o[0]) > (b - a)
                         for t in named if t.id != s.id for o in spans[t.id]) for a, b in spans[s.id])
        if not inside:
            out.append(s)
    return out


def movable(span: Any) -> bool:
    """Whether a move can fail for this item: it is still planned. A skipped, done, partial or unknown item has been
    handled, and is never one a move was asked for."""
    from thirties_core.models import SpanStatus
    return getattr(span, "status", None) == SpanStatus.PLANNED


def time_is_open(span: Any) -> bool:
    """Whether no time the reply says about this item can be wrong: it was handled with no time known (skipped, left
    unknown, or marked done with nothing but its plan to go by), so the plan's times were never where it happened."""
    from thirties_core.models import SpanStatus
    status = getattr(span, "status", None)
    if status in (SpanStatus.SKIPPED, SpanStatus.UNKNOWN):
        return True
    return status in (SpanStatus.DONE, SpanStatus.PARTIAL) and getattr(span, "time_source", "") in ("planned", "")


def _key(dt: Optional[datetime]) -> Set[Tuple[int, int]]:
    return {(dt.hour % 12, dt.minute)} if dt is not None else set()


def shown_forms(span: Any) -> Tuple[Set[Tuple[int, int]], Set[Tuple[int, int]]]:
    """(the starts, the ends) a reply may say for an item, in every form the schedule shows them: the times it has
    on the plan, the times it really happened at and its original plan, each as it is or by its block (a start by
    its block's start; an end that isn't a fact by the end of its block), and where a list of what got done puts
    it (shown_times.done_when). An end that is a fact is never read as the end of its block."""
    from thirties_core.clock import thirty_ceil, thirty_of
    from thirties_core.shown_times import done_when, end_exact, shown_end
    starts: Set[Tuple[int, int]] = set()
    ends: Set[Tuple[int, int]] = set()
    for dt in (span.start_dt, getattr(span, "actual_start", None), getattr(span, "planned_start", None)):
        if dt is not None:
            starts |= _key(dt) | _key(thirty_of(dt))
    for dt in (span.end_dt, getattr(span, "actual_end", None), getattr(span, "planned_end", None)):
        if dt is not None:
            ends |= _key(dt)
            if not end_exact(span):
                ends |= _key(thirty_ceil(dt))
    ends |= _key(shown_end(span))
    if getattr(getattr(span, "status", None), "value", "") in ("done", "partial"):
        listed = done_when(span)
        starts |= _key(listed)
        ends |= _key(listed)
    return starts, ends


def where_it_happened(span: Any) -> datetime:
    """When the item starts as the user should hear it: when it really started, if it was marked done with times,
    else where it sits."""
    if span.status.value in ("done", "partial") and getattr(span, "actual_start", None) is not None:
        return span.actual_start
    return span.start_dt



# --- what a call in an earlier turn was about (a change or a recorded outcome made before this turn) ---
_NAME_KEYS = ("event", "title", "item", "items", "task", "tasks", "goal", "target", "name", "rename_to", "first",
              "second", "events")


def earlier_call_names(brain: Any, turn_start: Optional[int], tools: Optional[Any] = None) -> list:
    """What the calls that went through in turns before this one were about (an item's title, a goal): the lower-case
    names, longest first. ``tools``: only these tools' calls; ``None``: every tool that changes something. A call whose
    result says it wasn't done changed nothing, so its names don't count."""
    from thirties_core.plan_tools import NOT_DONE, READ_ONLY_TOOLS
    names: list = []
    messages = brain.messages[:turn_start if turn_start is not None else 0]
    for i, m in enumerate(messages):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        results = []
        for later in messages[i + 1:]:  # the results come back in the calls' order, right after the message
            if later.get("role") != "tool":
                break
            results.append(str(later.get("content") or ""))
        for k, call in enumerate(m.get("tool_calls") or []):
            wanted = call.get("name") in tools if tools is not None else call.get("name") not in READ_ONLY_TOOLS
            if not wanted or (k < len(results) and results[k].startswith(NOT_DONE)):
                continue
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            for key in _NAME_KEYS:
                ref = args.get(key)
                for one in (ref if isinstance(ref, list) else [ref]):
                    if not one or not isinstance(one, (str, int)):
                        continue
                    try:
                        span = brain.tools.day_plan.get_span(str(one))
                    except Exception:
                        span = None
                    seen = getattr(brain.tools, "titles_seen", {}).get(str(one))  # an item since removed
                    name = str(span.title if span is not None else seen or one).strip().lower()
                    if len(name) > 2 and name not in names:
                        names.append(name)
    return sorted(names, key=len, reverse=True)


def _name_re(name: str) -> "re.Pattern":
    return re.compile(rf"(?<![\w'’]){re.escape(name)}(?![\w'’])", re.IGNORECASE)


def done_before(brain: Any, text: str, turn_start: Optional[int], tools: Optional[Any] = None) -> bool:
    """Whether the text names what a successful call in an earlier turn was about: what it says was done then, so it
    is no claim about this turn."""
    return any(_name_re(name).search(text or "") for name in earlier_call_names(brain, turn_start, tools))


_CLAUSE_SPLIT = re.compile(r"[,;]|\band\b|\bthen\b|\bbut\b", re.IGNORECASE)


def claim_is_earlier(brain: Any, sentence: str, m: "re.Match", turn_start: Optional[int]) -> bool:
    """Whether the clause a claim match is in (the words around it up to the nearest break: a comma, "and", "then",
    "but") names what a successful call in an earlier turn was about. A break inside such a name ("Wash and dry the
    towels") is part of the name."""
    names = earlier_call_names(brain, turn_start)
    if not names:
        return False
    masked = sentence
    for name in names:
        for found in _name_re(name).finditer(sentence):
            masked = masked[:found.start()] + "_" * (found.end() - found.start()) + masked[found.end():]
    start = max((b.end() for b in _CLAUSE_SPLIT.finditer(masked, 0, m.start())), default=0)
    after = _CLAUSE_SPLIT.search(masked, m.end())
    clause = sentence[start:after.start() if after else len(sentence)]
    return any(_name_re(name).search(clause) for name in names)
