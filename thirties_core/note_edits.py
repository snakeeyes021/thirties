"""Changing the structure of a checklist note: pure functions over Markdown bodies.

Thirties edits the user's own notes, so every edit here touches only the lines it
means to, finds its item by exact wording (and which copy of that wording it is),
and keeps the note's own indentation. They sit beside ``joplin_writeback``'s
check_off / append_task / insert_subtasks, which they build on; ``JoplinWriter``
applies them through the outbox, so each one is logged (heavy principle 2).

A note's sections are its headings; the lines before the first heading are the
"top" section, named "". The edits (J7, 10-17: "it still only has shower"):
- rename an item, split one line into several, add sub-items under one;
- take an item (with what's nested under it) out of a note, and put lines into a
  section of a note: together, moving an item between sections or notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX

# A heading under which finished things are kept (never a place for a new or moved open item).
DONE_HEADING_RE = re.compile(r"(done|completed?|finished|archived?)\b", re.IGNORECASE)
# Headings that say "not committed" even when the user's settings don't list them (10-24: a dated item went under
# Probation). The settings' someday_sections are added to these.
SOMEDAY_HEADING_RE = re.compile(r"\b(someday|some day|maybe|probation|backburner|back burner|later|ideas?|"
                                r"wish ?list|do i want|on hold|parked|icebox)\b", re.IGNORECASE)


def _match(line: str) -> Optional[re.Match]:
    return CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)


def _width(prefix: str) -> int:
    return len(prefix.replace("\t", "    "))


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


@dataclass
class Section:
    """A heading and the checklist items under it (``name`` "" is the top of the note, before any heading)."""
    name: str
    line: int                      # the heading's line (-1 for the top)
    someday: bool = False
    done: bool = False
    items: List[Tuple[str, bool, int]] = field(default_factory=list)  # (text, ticked, depth)

    @property
    def open_items(self) -> List[str]:
        return [t for t, ticked, _ in self.items if not ticked]


def is_someday_heading(name: str, someday_sections: Iterable[str] = ()) -> bool:
    low = name.strip().lower()
    return bool(low) and (low in {s.strip().lower() for s in someday_sections} or bool(SOMEDAY_HEADING_RE.search(low)))


def outline(body: str, someday_sections: Iterable[str] = ()) -> List[Section]:
    """The note's sections in order, each with its items. The top section is included only if it has items."""
    sections = [Section("", -1)]
    for i, line in enumerate((body or "").splitlines()):
        head = HEADING_REGEX.match(line)
        if head:
            name = head.group(1).strip()
            sections.append(Section(name, i, someday=is_someday_heading(name, someday_sections),
                                    done=bool(DONE_HEADING_RE.match(name))))
            continue
        m = _match(line)
        if m:
            sections[-1].items.append((m.group(2).strip(), bool(DONE_CHECKBOX_REGEX.match(line)), _width(m.group(1))))
    return [s for s in sections if s.name or s.items]


def find_item(lines: List[str], text: str, occurrence: int = 0, ticked_too: bool = True,
              open_only: bool = False, ticked_only: bool = False) -> Optional[int]:
    """The line of the item worded ``text``: the n-th open copy (counted like the reader's ids,
    joplin_engine.checklist_tasks), or, failing that and with ``ticked_too``, the n-th ticked copy.
    ``ticked_only`` looks at ticked copies only (a done item being filed)."""
    open_hits = [] if ticked_only else [i for i, line in enumerate(lines)
                                        if (m := CHECKBOX_REGEX.match(line)) and _same(m.group(2), text)]
    if occurrence < len(open_hits):
        return open_hits[occurrence]
    if open_only or not (ticked_too or ticked_only):
        return None
    ticked = [i for i, line in enumerate(lines)
              if (m := DONE_CHECKBOX_REGEX.match(line)) and _same(m.group(2), text)]
    return ticked[occurrence] if occurrence < len(ticked) else None


def item_end(lines: List[str], i: int) -> int:
    """The line after item ``i`` and everything nested under it (blank lines inside the nesting included)."""
    m = _match(lines[i])
    width = _width(m.group(1)) if m else 0
    end = i + 1
    while end < len(lines):
        line = lines[end]
        child = _match(line)
        if child and _width(child.group(1)) > width:
            end += 1
        elif not child and line.strip() and not HEADING_REGEX.match(line) and _width(re.match(r"[ \t]*", line).group(0)) > width:
            end += 1  # a continuation line or note nested under the item
        else:
            break
    return end


def section_of(lines: List[str], i: int) -> str:
    """The heading line ``i`` sits under ("" for the top of the note)."""
    for j in range(i, -1, -1):
        head = HEADING_REGEX.match(lines[j])
        if head:
            return head.group(1).strip()
    return ""


def _rebase(block: List[str]) -> List[str]:
    """An item's lines with its own indent taken off, so it can sit at the top level somewhere else."""
    m = _match(block[0]) if block else None
    if not m or not m.group(1):
        return list(block)
    cut = m.group(1)
    return [line[len(cut):] if line.startswith(cut) else line.lstrip() for line in block]


def _join(lines: List[str], ended: bool) -> str:
    return "\n".join(lines) + ("\n" if ended or lines else "")


def take_item(body: str, text: str, occurrence: int = 0, ticked_too: bool = True,
              open_only: bool = False, ticked_only: bool = False) -> Optional[Tuple[str, List[str], str]]:
    """Take an item and what's nested under it out of the note: (new body, its lines, the section it was in),
    or None if it isn't there."""
    lines = (body or "").splitlines()
    i = find_item(lines, text, occurrence, ticked_too=ticked_too, open_only=open_only, ticked_only=ticked_only)
    if i is None:
        return None
    end = item_end(lines, i)
    return _join(lines[:i] + lines[end:], body.endswith("\n")), _rebase(lines[i:end]), section_of(lines, i)


def insert_lines(body: str, block: List[str], section: Optional[str] = None) -> str:
    """Put lines (an item and its children, at the top level) at the end of a section's items.

    ``section`` None: the end of the note, above a Done heading (as append_task does). "": the top of the note,
    after the items before the first heading. A name: that heading (made at the end of the note if it's missing).
    """
    lines = (body or "").splitlines()
    heads = [(i, HEADING_REGEX.match(l).group(1).strip()) for i, l in enumerate(lines) if HEADING_REGEX.match(l)]
    start, stop = 0, len(lines)
    if section is None:
        done = next((i for i, name in heads if DONE_HEADING_RE.match(name)), None)
        if done is not None:
            stop = done
    elif section == "":
        stop = heads[0][0] if heads else len(lines)
    else:
        here = next((n for n, (_, name) in enumerate(heads) if _same(name, section)), None)
        if here is None:
            lines += ([""] if lines and lines[-1].strip() else []) + [f"## {section.strip()}"]
            start = stop = len(lines)
        else:
            start = heads[here][0] + 1
            stop = heads[here + 1][0] if here + 1 < len(heads) else len(lines)
    last = max((i for i in range(start, stop) if _match(lines[i])), default=None)
    if last is not None:
        at = item_end(lines, last)
        at = min(at, stop)
    else:
        at = stop
        while at > start and not lines[at - 1].strip():
            at -= 1
    out = lines[:at] + list(block) + lines[at:]
    if section == "" and heads and last is None and out[at + len(block):] and out[at + len(block)].strip():
        out.insert(at + len(block), "")  # a gap before the first heading
    return "\n".join(out) + "\n"


def rename_item(body: str, text: str, new: str, occurrence: int = 0) -> Optional[str]:
    """Reword one item, keeping its indent, tick and children."""
    lines = (body or "").splitlines()
    i = find_item(lines, text, occurrence)
    if i is None or not new.strip():
        return None
    m = _match(lines[i])
    box = "[x]" if DONE_CHECKBOX_REGEX.match(lines[i]) else "[ ]"
    lines[i] = f"{m.group(1)}- {box} {new.strip()}"
    return _join(lines, body.endswith("\n"))


def split_item(body: str, text: str, parts: List[str], occurrence: int = 0) -> Optional[str]:
    """One line becomes several, side by side at its level ("Call bank and dentist" → two calls). Anything nested
    under it stays under the last part."""
    parts = [p.strip() for p in parts if p and p.strip()]
    lines = (body or "").splitlines()
    i = find_item(lines, text, occurrence)
    if i is None or len(parts) < 2:
        return None
    m = _match(lines[i])
    box = "[x]" if DONE_CHECKBOX_REGEX.match(lines[i]) else "[ ]"
    lines[i:i + 1] = [f"{m.group(1)}- {box} {p}" for p in parts]
    return _join(lines, body.endswith("\n"))


def move_within(body: str, text: str, section: Optional[str], occurrence: int = 0) -> Optional[str]:
    """Move an item (with its children) to another section of the same note."""
    taken = take_item(body, text, occurrence)
    if taken is None:
        return None
    rest, block, _ = taken
    return insert_lines(rest, block, section)


def untick(block: List[str]) -> List[str]:
    """The first line of a block open again (undoing a done)."""
    if not block:
        return block
    return [re.sub(r"^([ \t]*)-\s*\[[xX]\]", r"\1- [ ]", block[0], count=1)] + block[1:]


__all__ = ["Section", "outline", "find_item", "item_end", "section_of", "take_item", "insert_lines", "rename_item",
           "split_item", "move_within", "untick", "is_someday_heading", "DONE_HEADING_RE", "SOMEDAY_HEADING_RE"]
