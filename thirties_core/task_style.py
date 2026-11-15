"""Help write tasks in the user's own style, learned from their earlier items and notes.

Before the assistant drafts new items (a breakdown, a few tasks for a new project, a reworded line), Python reads
how the user writes the items already on that list, or on all their lists: how long they run, whether they start
with a capital, end with a full stop, start with a verb, the words they often start with, and a few real lines
close to the topic. The model writes the drafts to match; Python does the counting.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

EXAMPLES = 8
MIN_ITEMS = 3
_VERBS = {"add", "ask", "book", "buy", "call", "cancel", "check", "clean", "clear", "do", "draft", "email", "file",
          "find", "finish", "fix", "get", "go", "hang", "install", "learn", "look", "mail", "make", "move", "order",
          "organize", "pack", "paint", "pay", "pick", "plan", "post", "practice", "print", "read", "renew",
          "repair", "replace", "research", "return", "review", "schedule", "send", "set", "ship", "sort", "start",
          "submit", "take", "text", "tidy", "try", "update", "wash", "watch", "water", "write"}


def _own(tasks: Iterable[Any], list_name: str = "") -> List[Any]:
    items = [t for t in tasks or [] if not t.id.startswith("media:") and (t.title or "").strip()]
    if list_name:
        want = list_name.strip().lower()
        on = [t for t in items if want in (t.parent_note_title or "").lower() or want in (t.section or "").lower()
              or want in (t.source_notebook or "").lower()]
        if len(on) >= MIN_ITEMS:
            return on
    return items


def style(titles: List[str]) -> Dict[str, Any]:
    words = [len(t.split()) for t in titles]
    firsts = [t.split()[0] for t in titles if t.split()]
    return {
        "count": len(titles),
        "words": int(statistics.median(words)) if words else 0,
        "capital": sum(1 for f in firsts if f[:1].isupper()) / max(1, len(firsts)),
        "period": sum(1 for t in titles if t.rstrip().endswith(".")) / max(1, len(titles)),
        "verb": sum(1 for f in firsts if f.lower().strip(",:") in _VERBS) / max(1, len(firsts)),
        "openers": [w for w, n in Counter(f.lower() for f in firsts).most_common(4) if n >= 2],
    }


def describe(s: Dict[str, Any]) -> List[str]:
    def share(x: float, yes: str, no: str) -> str:
        return yes if x >= 0.7 else (no if x <= 0.3 else f"{yes} about half the time")
    lines = [f"About {s['words']} words each (from {s['count']} of their items).",
             share(s["capital"], "Starts with a capital letter", "Starts in lower case") + ".",
             share(s["period"], "Ends with a full stop", "No full stop at the end") + ".",
             share(s["verb"], "Starts with a verb (an action)", "Usually names the thing, not the action") + "."]
    if s["openers"]:
        lines.append("Often starts with: " + ", ".join(s["openers"]) + ".")
    return lines


def _close(items: List[Any], about: str) -> List[Any]:
    if not about:
        return items[:EXAMPLES]
    from thirties_core.assistant_tools import rank_tasks
    near = rank_tasks(items, about, limit=EXAMPLES)
    return near + [t for t in items if t not in near][:max(0, EXAMPLES - len(near))]


STYLE_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "task_style",
    "description": "How the user writes their list items (length, capitals, full stops, verbs first, how they "
                   "usually start), with real examples. Call it before drafting new items for them (a breakdown, "
                   "tasks for a new project, a reworded line) and write the drafts to match. Read-only.",
    "parameters": {"list": "The list (note) the items will go on, if known.",
                   "about": "What the new items are about, for the closest examples."},
    "required": [],
}]
STYLE_READ_ONLY = {"task_style"}


def tool(assistant, list_name: Any = "", about: Any = "") -> str:
    items = _own(assistant.s.tasks, str(list_name or ""))
    if len(items) < MIN_ITEMS:
        return "Too few of the user's own items to tell their style; write short, plain lines."
    s = style([t.title for t in items])
    examples = _close(items, str(about or ""))
    return ("Their style:\n" + "\n".join(describe(s)) + "\nTheir own lines:\n"
            + "\n".join(t.title for t in examples))


__all__ = ["style", "describe", "tool", "STYLE_TOOL_SPECS", "STYLE_READ_ONLY"]
