"""One schedule per reply: Python's.

When Python's standard view goes under a reply (whats_next.py: after a change, or after show_schedule, ``place``),
a schedule the model listed itself in the same reply is left out, so the day is never shown twice and the rows the
user reads are Python's. The model's own "Now:" line and "You're in the … block" line go too: the view says what's on
now, in one place. What counts as the model's listing: a run of lines that are
schedule rows (a time, then one of today's items: "6:00 PM – Repot the cactus", "6:00 PM Repot the cactus"), with the
lines among them that only name an item or an open stretch, or say "Now:", at least two of them rows, and mostly
items Python's text shows too. Its intro line goes with it ("Here's how tonight runs:"), or ends on a full stop
when it says more than that. A single row, rows about other days or about what's done, and prose that mentions times
all stay.

Sections appear once ("If there's time", "Not marked yet"): Python's when its text has one, else the model's first.
A section of today's list lines with no time ("On today's list, no time yet") never appears: the user doesn't want
one, and Python's view has none.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set, Tuple

_TIME = r"\d{1,2}(?::\d{2})?\s*[AaPp]\.?\s?[Mm]\.?"
_ROW_RE = re.compile(rf"^\s*(?:[-•*]\s*|\d+[.)]\s+)?(?:at\s+)?{_TIME}(?:\s*(?:[–—-]|to)\s*{_TIME})?"
                     r"\s*(?:[–—:-]\s*)?(?P<rest>\S.*)$")
_OPEN_RE = re.compile(r"^\s*(?:[-•*]\s*)?(?:free|open|nothing (?:planned|else))\b[^.!?]*$", re.IGNORECASE)
_OPEN_REST_RE = re.compile(r"^(?:free|open|sleep|bed(?:time)?|wind down)\b", re.IGNORECASE)
_NOW_RE = re.compile(r"^\s*(?:[-•*]\s*)?now\s*:", re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)")
_FILLER_RE = re.compile(rf"{_TIME}|\b(?:then|and|now|with|plus|at|until|till|by|from|to)\b|[+,&/–—:;-]",
                        re.IGNORECASE)
# An intro line that only introduces the list ("Here's how tonight runs:", "Next up:"): it goes with the list
_PRESENTS_RE = re.compile(r"\b(?:here(?:'s| is| are)|below|look(?:s|ing)?|lines? up|as follows|stands?|"
                          r"the rest of|coming up|next up|up next|what'?s (?:on|next|left|after|ahead)|"
                          r"(?:updated|new|full) (?:schedule|plan|day)|schedule|agenda|plan for)\b", re.IGNORECASE)
# A heading of the model's own for a schedule, nothing else on its line ("The rest of today:", "Tonight:")
_BARE_HEADING_RE = re.compile(r"^\s*(?:the )?(?:rest of (?:today|the day|your day|the evening|tonight)|"
                              r"next(?: up)?|up next|coming up|(?:the )?whole day|today|tonight|this evening|"
                              r"(?:your |the )?(?:updated |new )?(?:schedule|plan|day|evening)(?: for today)?)\s*:\s*$",
                              re.IGNORECASE)
_STATUS_RE = re.compile(r"^\s*(?:[-•*]\s*)?you'?re in the \d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.? block\b", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?](?:[\"'”’)]*)\s+")

UNTIMED_HEADS = (re.compile(r"^\s*if there'?s time\b[^\n]*:\s*$", re.IGNORECASE),
                 re.compile(r"^\s*not marked yet\b[^\n]*:\s*$", re.IGNORECASE))
# a section the user never wants: today's list lines with no time
NO_TIME_HEAD = re.compile(r"^\s*(?:on )?today'?s list,? (?:with )?no time\b[^\n]*:\s*$", re.IGNORECASE)


def _titles(plan) -> List[str]:
    """Today's item titles, longest first (so "Bake soda bread and jam" wins over "Bake soda bread")."""
    seen: Set[str] = set()
    out = []
    for s in getattr(plan, "spans", []) or []:
        t = (s.title or "").strip()
        if len(t) >= 3 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return sorted(out, key=len, reverse=True)


def _named(text: str, titles: Sequence[str]) -> Tuple[Optional[str], str]:
    """The first (longest) title the text names, and the text with every named title taken out."""
    low = text.lower()
    first = None
    for t in titles:
        pattern = r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)"
        if re.search(pattern, low):
            first = first or t
            low = re.sub(pattern, " ", low)
    return first, low


def _kind(line: str, titles: Sequence[str]) -> Tuple[str, Optional[str]]:
    """("row", title): a time, then an item; ("item", title): a line that only names items; ("open", None): an open
    stretch or the night; ("now", title): a "Now:" line; ("", None): anything else."""
    text = line.strip()
    if not text:
        return "", None
    if _NOW_RE.match(text):
        return "now", _named(text, titles)[0]
    m = _ROW_RE.match(text)
    if m:
        rest = m.group("rest")
        if _OPEN_REST_RE.match(rest):
            return "open", None
        title, left = _named(rest, titles)
        if title and _left_over(left) <= 5 and not rest.rstrip().endswith("?"):  # "Home: Fix the latch at 2:45 PM"
            return "row", title
        return "", None
    if _OPEN_RE.match(text):
        return "open", None
    title, left = _named(text.lstrip("-•* "), titles)
    if title and _left_over(left) <= 3 and not text.endswith(("?", ".", "!")):
        return "item", title
    return "", None


def _left_over(text: str) -> int:
    """How many words a line says besides its items, times, notes in brackets and joining words."""
    return len(_FILLER_RE.sub(" ", _PAREN_RE.sub(" ", text)).split())


def _intro(line: str) -> Optional[str]:
    """What's left of an intro line once its list has gone: None to drop the line."""
    text = line.rstrip()
    if not text.endswith(":"):
        return line
    cuts = [m.end() for m in _SENTENCE_END_RE.finditer(text)]
    head, last = (text[:cuts[-1]].rstrip(), text[cuts[-1]:]) if cuts else ("", text)
    if _PRESENTS_RE.search(last) or len(last.split()) <= 3:
        return head or None
    return text[:-1].rstrip() + "."


def _listings(lines: List[str], protected: Set[int], titles: Sequence[str], shown: str) -> Set[int]:
    """The lines of the model's own listings that repeat what ``shown`` (Python's text) lays out."""
    drop: Set[int] = set()
    i = 0
    while i < len(lines):
        kinds = []
        j = i
        while j < len(lines) and j not in protected:
            kind, title = _kind(lines[j], titles)
            if not kind:
                break
            kinds.append((kind, title))
            j += 1
        if not kinds:
            i += 1
            continue
        rows = sum(1 for k, _ in kinds if k == "row")
        named = [t for k, t in kinds if k in ("row", "item", "now") and t]
        repeats = sum(1 for t in named if t.lower() in shown)
        if rows >= 2 and repeats * 2 >= len(named):
            drop.update(range(i, j))
            k = i - 1
            if k >= 0 and k not in protected and not lines[k].strip():
                k -= 1  # skip one empty line above the rows
            if k >= 0 and k not in protected and lines[k].rstrip().endswith(":") and k not in drop:
                kept = _intro(lines[k])
                if kept is None:
                    drop.add(k)
                else:
                    lines[k] = kept
        i = j
    return drop


def _untimed(lines: List[str], protected: Set[int], shown_lines: Sequence[str]) -> Set[int]:
    """A single copy per section (Python's if its text holds that section, otherwise the model's first); a
    section of list lines without times is always dropped."""
    drop: Set[int] = set()
    for head in UNTIMED_HEADS + (NO_TIME_HEAD,):
        pythons = head is NO_TIME_HEAD or any(head.match(line) for line in shown_lines)
        found = 0
        for i, line in enumerate(lines):
            if i in protected or not head.match(line):
                continue
            found += 1
            if pythons or found > 1:
                j = i
                while j < len(lines) and j not in protected and lines[j].strip():
                    drop.add(j)
                    j += 1
    return drop


def _tidy_lines(lines: List[str], protected: Set[int], shown: str, plan) -> List[str]:
    lines = list(lines)
    titles = _titles(plan)
    drop = _listings(lines, protected, titles, shown.lower())
    drop |= _untimed(lines, protected, shown.splitlines())
    if shown.strip():  # what's on now is said once, in Python's view
        drop |= {i for i, line in enumerate(lines) if i not in protected
                 and (_NOW_RE.match(line) or _STATUS_RE.match(line))}
    return [line for i, line in enumerate(lines) if i not in drop]


def _joined(lines: List[str]) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def tidy(reply: str, shown: str, plan) -> str:
    """The reply with the model's own listing of the day left out, when ``shown`` (Python's text, going under it)
    lays it out; and each untimed section once."""
    if not reply:
        return reply
    lines = reply.splitlines()
    out = _tidy_lines(lines, set(), shown or "", plan)
    return reply if out == lines else _joined(out)


def around(reply: str, schedule: Optional[str], plan) -> str:
    """A reply that carries show_schedule's text as it is: the model's own listing elsewhere in it left out."""
    sched = (schedule or "").strip()
    if not reply or not sched or sched not in reply:
        return reply
    at = reply.index(sched)
    first = reply[:at].count("\n")
    original = reply.splitlines()
    lines = list(original)
    protected = set(range(first, first + sched.count("\n") + 1))
    k = first - 1
    while k >= 0 and not lines[k].strip():
        k -= 1
    if k >= 0 and _BARE_HEADING_RE.match(lines[k]):  # the model's own heading left above Python's schedule
        lines[k] = ""
    out = _tidy_lines(lines, protected, sched, plan)
    return reply if out == original else _joined(out)


def place(reply: str, view: str, plan) -> str:
    """The reply with Python's view under it, exactly once: a copy of it the model made is taken out, and so is
    any listing, "Now:" line, status line or section of the model's own that the view lays out (``tidy``). Only the
    model's own sentences stay above the view."""
    view = (view or "").strip()
    if not view:
        return reply
    text = (reply or "").replace(view, "") if reply else ""
    text = tidy(text, view, plan) if text.strip() else ""
    from thirties_core.messages import join_sections
    return join_sections(text.strip(), view)


__all__ = ["around", "place", "tidy"]
