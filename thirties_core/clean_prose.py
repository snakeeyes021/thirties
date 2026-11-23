"""Reply text Python edits still reads as clean prose.

A few checks change reply text in Python when a fresh draft from the model didn't fix it: a promise
nobody recorded is cut, a correction of a draft the user never saw is cut, a claim that didn't happen is replaced by
the plain sentence saying what's still as it was. Cutting part of a sentence once left a fragment behind: it began
in lower case, ended in a colon and a full stop together, and the list it had introduced hung under the plain
sentence that followed it. So:

- edits work on whole sentences only, and a sentence that introduces a list (it ends with a colon) takes its list
  with it when it goes;
- a list line an edit rewrites stays a list line (its mark, no closing stop), so the lines under it keep their
  lead-in and none of them is cut as a list without one;
- a sentence Python writes starts with a capital letter;
- ``introduced`` names what an edit broke that the model's own text didn't have (a sentence starting in lower case,
  a stranded colon, doubled punctuation, a list cut from its lead-in, a sentence ending on "and"), so the caller can
  reject the edit and fall back to the model's rewrite;
- ``cut_broken`` is the last step when no rewrite came: the broken sentences go whole.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, List, Optional, Tuple

_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STOP = (".", "!", "?", "…")
# a word ending in a full stop that doesn't end the sentence
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e|vs|etc|approx|[ap]\.m)\.$", re.IGNORECASE)


def cap(text: str) -> str:
    """The sentence with its first letter in capitals (a title the user wrote in lower case starts a sentence)."""
    text = (text or "").strip()
    return text[:1].upper() + text[1:] if text[:1].islower() else text


def _is_prose(line: str) -> bool:
    return line.rstrip().endswith(_STOP)


# the start of a list line: its indent and its mark ("- ", "• ", "2. "), if it has one
_MARK_RE = re.compile(r"^\s*([-*•]\s+|\d+[.)]\s+)?")


def _is_lead_in(line: str) -> bool:
    return line.rstrip().endswith(":")


def list_after(lines: List[str], i: int) -> int:
    """How many lines after line ``i`` (a lead-in ending in a colon) are its list: blank lines right after it, then
    the rows up to the next blank line or a line of prose that isn't a short item."""
    n, j = 0, i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    rows = 0
    while j < len(lines) and lines[j].strip():
        line = lines[j].strip()
        if rows and _is_prose(line) and (len(line) > 60 or len(_SPLIT_RE.split(line)) > 1):
            break
        rows += 1
        j += 1
    if rows:
        n = j - (i + 1)
    return n


def edit(text: str, new_for: Callable[[str], Optional[str]],
         keeps_list: Callable[[str], bool] = lambda s: False) -> Tuple[str, bool]:
    """The text with each sentence ``new_for`` gives a replacement for replaced whole ("" cuts it), and whether
    anything changed. A sentence ending its line with a colon takes its list with it, unless its replacement still
    introduces it or ``keeps_list`` says the list stays (an opening that only announced it). Lines that are left
    empty go."""
    lines = (text or "").split("\n")
    rows = {k for i, line in enumerate(lines) if _is_lead_in(line) for k in range(i + 1, i + 1 + list_after(lines, i))}
    out: List[str] = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            out.append("")
            i += 1
            continue
        parts = _SPLIT_RE.split(line)
        kept: List[str] = []
        cut_list = False
        for j, part in enumerate(parts):
            new = new_for(part) if part.strip() else None
            if new is None:
                kept.append(part)
                continue
            changed = True
            if new.strip():
                kept.append(cap(new))
            if j == len(parts) - 1 and _is_lead_in(part) and not _is_lead_in(new) and not keeps_list(part):
                cut_list = True
        if kept:
            new_line = " ".join(p.strip() if k else p.rstrip() for k, p in enumerate(kept)).rstrip()
            if (i in rows or _MARK_RE.match(line).group(1)) and not _is_prose(line) and new_line != line.rstrip():
                # a list line stays a list line: its mark, no closing stop, so the lines under it keep their lead-in
                new_line = _MARK_RE.match(line).group(0) + _MARK_RE.sub("", new_line, count=1).rstrip("".join(_STOP))
            out.append(new_line)
        i += 1 + (list_after(lines, i) if cut_list else 0)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return result, changed


def cut(text: str, drop: Callable[[str], bool]) -> str:
    """The text without the sentences ``drop`` picks (and the lists they introduce)."""
    out, _ = edit(text, lambda s: "" if drop(s) else None)
    return out


# --- what reads broken ---

_STRANDED_COLON_RE = re.compile(r":[ \t]*[.!?,;]|:[ \t]*$")
_DOUBLE_RE = re.compile(r"[,;][ \t]*[.!?]|(?<!\.)\.[ \t]*[,;:]|(?<!\.)\.\.(?!\.)|\.[ \t]+\.|[!?][ \t]*[.,;:]")
_SPACE_RE = re.compile(r"\w[ \t]+[,;.!?](?=\s|$)")
_DANGLING_RE = re.compile(r"\b(?:and|or|but|the|with)[ \t]*[.!?:](?=\s|$)")
_EMPTY_PARENS_RE = re.compile(r"\([ \t]*\)")
_CAMEL_RE = re.compile(r"^[a-z]+[A-Z]")


def problems(text: str) -> List[Tuple[str, str]]:
    """(what's broken, the sentence or line it's in) for each place the text doesn't read as clean prose."""
    found: List[Tuple[str, str]] = []
    lines = (text or "").split("\n")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if line[:1] in ",;:.":
            found.append(("starts with punctuation", line))
        if _is_lead_in(line) and not list_after(lines, i):
            found.append(("a colon with nothing after it", line))
        prose = _is_prose(line) or len(_SPLIT_RE.split(line)) > 1
        parts = _SPLIT_RE.split(line)
        for j, part in enumerate(parts):
            s = part.strip()
            if not s:
                continue
            after_abbrev = j > 0 and _ABBREV_RE.search(parts[j - 1].strip())
            if (prose or j > 0) and s[:1].islower() and not after_abbrev and not _CAMEL_RE.match(s):
                found.append(("a sentence starting in lower case", s))
            body = s[:-1] if _is_lead_in(s) and j == len(parts) - 1 else s
            if _STRANDED_COLON_RE.search(body):
                found.append(("a stranded colon", s))
            if _DOUBLE_RE.search(s):
                found.append(("doubled punctuation", s))
            if _SPACE_RE.search(s):
                found.append(("a space before punctuation", s))
            if _DANGLING_RE.search(s):
                found.append(("a sentence cut off mid-way", s))
            if _EMPTY_PARENS_RE.search(s):
                found.append(("empty brackets", s))
    # a list with nothing introducing it: rows right under a finished sentence instead of a lead-in, or two or more
    # rows the text starts with
    def is_row(line: str) -> bool:
        return bool(line) and not _is_prose(line) and not _is_lead_in(line)
    rows = [line.strip() for line in lines]
    first = next((i for i, line in enumerate(rows) if line), None)
    if first is not None and is_row(rows[first]) and first + 1 < len(rows) and is_row(rows[first + 1]):
        found.append(("a list cut from its lead-in", rows[first]))
    for i in range(1, len(rows)):
        if is_row(rows[i]) and rows[i - 1] and _is_prose(rows[i - 1]):
            found.append(("a list cut from its lead-in", rows[i]))
    return found


def introduced(text: str, written: Iterable[str]) -> List[Tuple[str, str]]:
    """What reads broken in ``text`` that none of ``written`` (the model's own words this turn) had: what an edit
    broke."""
    had = set()
    for w in written:
        had.update(problems(w))
    return [p for p in problems(text) if p not in had]


def cut_broken(text: str, broken: Iterable[Tuple[str, str]]) -> str:
    """The text without the sentences and rows that read broken (and the lists they introduce)."""
    bad = {where.strip() for _, where in broken}
    if not bad:
        return text
    out = cut(text, lambda s: s.strip() in bad)
    lines = [line for line in out.split("\n") if line.strip() not in bad]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    # rows whose lead-in went: they go too
    orphans = {where for what, where in problems(out) if what == "a list cut from its lead-in"}
    while orphans:
        lines = out.split("\n")
        keep, skipping = [], False
        for line in lines:
            if line.strip() in orphans:
                skipping = True
                continue
            if skipping and line.strip() and not _is_prose(line):
                continue
            skipping = False
            keep.append(line)
        new = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
        if new == out:
            break
        out = new
        orphans = {where for what, where in problems(out) if what == "a list cut from its lead-in"}
    return out


def rewrite_check(broken: Iterable[Tuple[str, str]]) -> str:
    """What the model is told when an edit of its reply came out broken."""
    what = sorted({w for w, _ in broken})
    return ("Your reply reads broken after a part was taken out (" + ", ".join(what) + "). Write it again, whole, "
            "in plain sentences, saying only what's true now; every list keeps the sentence that introduces it.")


__all__ = ["cap", "edit", "cut", "problems", "introduced", "cut_broken", "rewrite_check", "list_after"]
