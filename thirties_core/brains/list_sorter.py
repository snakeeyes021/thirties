"""Guessing what each list is for and how pressing each item is, with the user's chosen model.

The core (``thirties_core/lists.py``) stores roles and urgencies and decides what
still needs a guess; this module holds the prompts. Two sorters, one per brain:

- Gemini reads a whole note at once and returns every item's urgency.
- Gemma (weak, small context) makes one constrained choice per call: a note's
  role, then one item at a time, seeing the note around that item (its headings,
  blank-line groups and nesting are how people mark urgency in a list).

A guess is only a starting point: the user confirms or corrects it.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX
from thirties_core.lists import ROLES, URGENCIES
from thirties_core.models import TaskItem

logger = logging.getLogger(__name__)

Guess = Tuple[str, Optional[date]]  # (urgency, due date)

ROLE_LINES = "\n".join(f"- {name}: {meaning}" for name, meaning in ROLES.items())
URGENCY_LINES = "\n".join(f"- {name}: {meaning}" for name, meaning in URGENCIES.items())

ROLE_PROMPT = f"""You sort a person's to-do notes for their planning assistant. Given one note (its notebook, title and how it begins), say what the note is for:
{ROLE_LINES}
Notes are usually organized by area of life (home, errands, medical, a project). A note of daily repeated steps is a routine. A note named for today's tasks is today. A list of options for free time ("go outside", "read a book", "pick one item from...") is a menu. A software or other ongoing project's list of features, bugs or tickets is a backlog. A catch-all of unrelated quick items waiting to be sorted ("Assorted Quickies", "Inbox", "Misc") is a dropbox. Shopping lists are lists unless they read as wishlists. Notes of facts, plans, ideas, cheat sheets or logs, with few or no checkboxes, are ignore."""

URGENCY_PROMPT = f"""You help a planning assistant judge how pressing each item on a person's to-do list is:
{URGENCY_LINES}
Most items on a list are "whenever". Use "now" only for something that clearly can't wait past tomorrow (a deadline, something broken or unsafe, a reply someone is waiting for), and "soon" only when there's a reason it should happen within a couple of weeks.
Read the note the way its owner wrote it: headings ("Active", "Probation", "Someday", "Do I want?"), groups separated by blank lines (an item set apart at the top is often the pressing one), nesting (sub-items share their parent's urgency unless they say otherwise), and the wording ("for THIS winter", "before the trip", "need to call"). Medical, money and deadline items lean soon; home improvements and purchases lean whenever unless something makes them pressing; anything under a someday, probation or maybe heading is someday."""


# Signs in an item's own words that it can't wait: a time word, a reply someone's waiting for, something broken.
NOW_CUE = re.compile(r"\b(today|tonight|tomorrow|asap|urgent|now|this week|deadline|due|overdue|before|by "
                     r"(mon|tue|wed|thu|fri|sat|sun)\w*|back|reply|respond|return|broken|leak\w*|expir\w*|"
                     r"renew\w*|refill)\b", re.I)


def note_outline(body: str, mark: Optional[str] = None, numbered: Optional[List[str]] = None,
                 max_lines: int = 70) -> str:
    """A note as its owner sees it, compactly: headings, checklist items (ticked ones marked), blank-line
    groups as '(gap)', long lines cut. ``mark`` puts an arrow on one item; ``numbered`` numbers these
    items' lines (by text, in order)."""
    out, want, n, mark_at = [], list(numbered or []), 0, None
    previous_blank = False
    for line in body.splitlines():
        if not line.strip():
            if out and not previous_blank:
                out.append("(gap)")
            previous_blank = True
            continue
        previous_blank = False
        head = HEADING_REGEX.match(line)
        item = CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)
        if head:
            out.append(f"## {head.group(1).strip()}")
        elif item:
            indent = "  " * (len(item.group(1).replace("\t", "    ")) // 2)
            text = item.group(2).strip()
            ticked = " (ticked)" if DONE_CHECKBOX_REGEX.match(line) else ""
            prefix = ""
            if want and text == want[0]:
                want.pop(0)
                n += 1
                prefix = f"{n}. "
            if mark is not None and mark_at is None and text == mark:
                prefix, mark_at = "→ ", len(out)
            out.append(f"{indent}- {prefix}{text[:100]}{ticked}")
        else:
            out.append(line.strip()[:100])
    if len(out) > max_lines:
        centre = mark_at if mark_at is not None else 0
        lo = max(0, min(centre - max_lines // 2, len(out) - max_lines))
        out = (["…"] if lo else []) + out[lo:lo + max_lines] + (["…"] if lo + max_lines < len(out) else [])
    return "\n".join(out)


def _head(note: Dict[str, Any]) -> str:
    return (f"Notebook: {note.get('source_notebook', '')}\nTitle: {note.get('title', '')}\n"
            f"Beginning:\n{note_outline(note.get('body') or '', max_lines=18) or '(empty)'}")


def _due(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


class GemmaListSorter:
    """One small constrained decision per call."""

    ROLE_SCHEMA = {"type": "object", "properties": {"role": {"type": "string", "enum": list(ROLES)}},
                   "required": ["role"]}
    URGENCY_SCHEMA = {"type": "object", "properties": {"urgency": {"type": "string", "enum": list(URGENCIES)}},
                      "required": ["urgency"]}

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def role(self, note: Dict[str, Any]) -> Optional[str]:
        out = self.runtime.json_call(ROLE_PROMPT, _head(note), self.ROLE_SCHEMA, max_output_tokens=24)
        return out.get("role") if out.get("role") in ROLES else None

    def urgencies(self, note: Dict[str, Any], role: str, items: List[TaskItem], today: date) -> Dict[str, Guess]:
        out: Dict[str, Guess] = {}
        for t in items:
            user = (f"List: {note.get('title', '')} ({role})\n{note_outline(note.get('body') or '', mark=t.title)}\n\n"
                    f"How pressing is the item marked →, '{t.title}'?")
            got = self.runtime.json_call(URGENCY_PROMPT, user, self.URGENCY_SCHEMA, max_output_tokens=24)
            urgency = got.get("urgency")
            if urgency == "now" and not NOW_CUE.search(t.title):
                urgency = "soon"  # Gemma says "now" far too often; it stands only with a sign of urgency
            if urgency in URGENCIES:
                out[t.id] = (urgency, None)
        return out


class FrontierListSorter:
    """Gemini: a whole note per call."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def _ask(self, system: str, user: str) -> Dict[str, Any]:
        from thirties_core.brains.slm.parse import extract_json
        reply = self.engine.chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
        content = str(reply.get("content") or "")
        data = extract_json(content)
        if not data:
            logger.warning("List sorting: no JSON in the model's reply: %r", content[:200])
        return data or {}

    def role(self, note: Dict[str, Any]) -> Optional[str]:
        out = self._ask(ROLE_PROMPT + '\nReturn ONLY a JSON object: {"role": "<one of the roles>"}', _head(note))
        return out.get("role") if out.get("role") in ROLES else None

    def urgencies(self, note: Dict[str, Any], role: str, items: List[TaskItem], today: date) -> Dict[str, Guess]:
        titles = [t.title for t in items]
        user = (f"Today is {today:%A, %B %-d, %Y}.\nList: {note.get('title', '')} ({role})\n"
                f"{note_outline(note.get('body') or '', numbered=titles, max_lines=400)}\n\n"
                f"Judge each numbered item. Return ONLY a JSON object: "
                '{"items": [{"n": <number>, "urgency": "now" | "soon" | "whenever" | "someday", '
                '"due": "<YYYY-MM-DD only if the note gives a date or deadline, else empty>"}...]}')
        out: Dict[str, Guess] = {}
        for row in self._ask(URGENCY_PROMPT, user).get("items") or []:
            try:
                n = int(row.get("n"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(items) and row.get("urgency") in URGENCIES:
                out[items[n - 1].id] = (row["urgency"], _due(row.get("due")))
        return out


def list_sorter_for(config, state_db=None, inference_engine=None):
    """The sorter for the user's chosen brain, else whichever model is available here; None if none is."""
    from thirties_core.brains import brain_choice
    from thirties_core.brains.slm.runtime import GemmaRuntime, SlmUnavailable, slm_unavailable_reason
    from thirties_core.inference import GeminiSourcesEngine

    def gemma():
        if slm_unavailable_reason(config) is not None:
            return None
        try:
            return GemmaListSorter(GemmaRuntime.shared(config.assistant.slm_model_path))
        except SlmUnavailable:
            return None

    def gemini():
        engine = inference_engine if isinstance(inference_engine, GeminiSourcesEngine) else GeminiSourcesEngine(config)
        return FrontierListSorter(engine) if engine.is_available() else None

    order = (gemini, gemma) if brain_choice(config, state_db) == "frontier" else (gemma, gemini)
    for make in order:
        sorter = make()
        if sorter is not None:
            return sorter
    return None


__all__ = ["GemmaListSorter", "FrontierListSorter", "list_sorter_for", "note_outline"]
