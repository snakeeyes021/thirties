"""When a change the reply talks about didn't happen: which items, whether the reply already says so, and a plain
line when it doesn't.

A refused change used to add "(One part of that didn't go through, so it isn't recorded yet.)" under the reply,
even when the reply itself already asked about the very item the refusal was about, and in words that tell the user
nothing. Now:

- a refusal that changes nothing anyway ("nothing to change": it was already so) is no failure;
- a reply that names the item in a sentence that asks about it or says it's still as it was already handles it:
  nothing is added;
- otherwise the reply is sent back once with the plain sentence to say, and if it still claims the change, that
  claim says it in place ("Soup night is still on the plan at 6:00 PM; I couldn't take it off.": plain_admit.py);
  nothing is ever added under the reply.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# a refusal that changes nothing anyway: the item was already that way
_NO_OP_RE = re.compile(r"^\W*nothing to change\b", re.IGNORECASE)
# what a sentence says about an item that's still as it was, or asks about it
_STILL_RE = re.compile(r"\b(?:still|stays?|stayed|kept|keep|left|as it was|unchanged|hasn'?t|haven'?t|isn'?t|aren'?t|"
                       r"wasn'?t|weren'?t|didn'?t|couldn'?t|can'?t|won'?t|not)\b", re.IGNORECASE)
_SENTENCES_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_ID_RE = re.compile(r"^[0-9a-f]{8,}$|^cal:|^routine-", re.IGNORECASE)

# how each kind of change that didn't happen is said
_SAID = {
    "remove_event": "{} is still on the plan", "clear_time": "{} is still on the plan",
    "log_outcome": "{} isn't marked yet", "mark_done": "{} isn't marked yet",
    "add_event": "{} isn't on the plan", "add_to_today": "{} isn't on today's list",
}
_DEFAULT = "{} wasn't changed"


def _calls(messages: Sequence[Dict[str, Any]], turn_start: int) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(tool result, the call's arguments) for each tool result this turn. A result carries its arguments, or is
    paired with its call in the order the calls were made."""
    out, queue = [], []
    for m in messages[turn_start:]:
        if m.get("role") == "assistant":
            queue = list(m.get("tool_calls") or [])
        elif m.get("role") == "tool":
            call = queue.pop(0) if queue else {}
            args = m.get("arguments") if isinstance(m.get("arguments"), dict) else call.get("arguments")
            out.append((m, args if isinstance(args, dict) else {}))
    return out


def failed_items(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int, read_only: Iterable[str],
                 not_done: str) -> Tuple[List[Tuple[str, str]], bool]:
    """([(the item's title, or "" when it can't be named; the tool)] for each change that didn't happen this turn
    and wasn't made after by another call on the same item; whether the lists in Joplin lag behind). A refusal
    that would have changed nothing isn't counted."""
    read_only = set(read_only)
    failed: Dict[str, Tuple[str, str]] = {}
    lagging = False
    for m, args in _calls(messages, turn_start):
        name = str(m.get("name") or "")
        if name in read_only:
            continue
        out = str(m.get("content") or "")
        title = _title(tools, args)
        key = title.lower() or f"<{name}>"
        if out.startswith(not_done):
            # --- a move nobody asked for, refused: not news to the user (unasked.py) ---
            from thirties_core.unasked import skip as unasked
            if unasked(tools, title, out):
                continue
            # --- end a move nobody asked for ---
            if not _NO_OP_RE.search(out[len(not_done):].lstrip(": ")):
                failed[key] = (title, name)
        else:
            failed.pop(key, None)
            if re.search(r"\((couldn'?t [^:]*Joplin.*)\)\.?$", out):
                lagging = True
    return list(failed.values()), lagging


def _title(tools: Any, args: Dict[str, Any]) -> str:
    ref = next((args.get(k) for k in ("event", "title", "item", "task", "target", "routine") if args.get(k)), "")
    if isinstance(ref, list):
        ref = " ".join(map(str, ref))
    ref = str(ref or "").strip()
    try:
        span = tools.day_plan.get_span(ref) if ref else None
    except Exception:
        span = None
    if span is not None:
        try:
            return tools._title(span)
        except Exception:
            return span.title
    return "" if _ID_RE.match(ref) else ref


def handled(reply: str, items: Sequence[Tuple[str, str]], others: Iterable[str] = ()) -> bool:
    """The reply already tells the user about every item that didn't change: it names each one in a sentence that
    asks about it or says it's still as it was."""
    from thirties_core.item_facts import mentions
    if not items or any(not title for title, _ in items):
        return False
    others = list(others)
    sentences = [s for s in _SENTENCES_RE.split(reply or "") if s.strip()]
    return all(any(mentions(s, title, others) and (s.rstrip().endswith("?") or _STILL_RE.search(s))
                   for s in sentences) for title, _ in items)


def plain_line(items: Sequence[Tuple[str, str]], lagging: bool = False) -> str:
    """What didn't happen, in plain words, for under the reply."""
    named = [_SAID.get(tool, _DEFAULT).format(title) for title, tool in items if title]
    parts = list(dict.fromkeys(named))
    if any(not title for title, _ in items):
        parts.append("one of those changes didn't happen")
    if lagging:
        parts.append("your lists haven't caught up with that yet")
    if not parts:
        return ""
    text = "; ".join(parts)
    return f"({text[0].upper()}{text[1:]}.)"


def own_up_line(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int, read_only: Iterable[str],
                not_done: str, reply: str) -> Optional[str]:
    """The line to add under a reply that claims a change that didn't happen, or None when nothing should be added
    (nothing failed for real, or the reply already says so)."""
    items, lagging = failed_items(tools, messages, turn_start, read_only, not_done)
    if not items and not lagging:
        return None
    plan = getattr(tools, "day_plan", None)
    others = [s.title for s in plan.spans] if plan is not None else []
    if items and handled(reply, items, others) and not lagging:
        return None
    return plain_line(items, lagging) or None


# --- said in the reply itself, never as a note under it (plain_admit.py) ---

def _still_to_say(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int, read_only: Iterable[str],
                  not_done: str, reply: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """(the changes that didn't happen and the reply doesn't already say so, as (title, tool); the plan's titles)."""
    from thirties_core.plain_admit import LISTS
    items, lagging = failed_items(tools, messages, turn_start, read_only, not_done)
    plan = getattr(tools, "day_plan", None)
    others = [s.title for s in plan.spans] if plan is not None else []
    todo = [i for i in items if not (i[0] and handled(reply, [i], others))]
    return todo + ([("", LISTS)] if lagging else []), others


def still_lines(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int, read_only: Iterable[str],
                not_done: str, reply: str) -> List[str]:
    """The plain sentences the reply should say ("Bake bread is still at 5:00 PM; I couldn't move it.")."""
    from thirties_core.plain_admit import still_line
    todo, _ = _still_to_say(tools, messages, turn_start, read_only, not_done, reply)
    return list(dict.fromkeys(still_line(tools, t, tool) for t, tool in todo))


def said_plainly(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int, read_only: Iterable[str],
                 not_done: str, reply: str, claim_re: "re.Pattern") -> str:
    """The reply with what didn't change said in place of the claim that it did (plain_admit.say_failed)."""
    from thirties_core.plain_admit import say_failed
    todo, others = _still_to_say(tools, messages, turn_start, read_only, not_done, reply)
    return say_failed(tools, reply, todo, claim_re, others) if todo else reply

# --- end said in the reply itself ---


# "All fixed", "everything is back in place", "it's all exactly as you laid out": a claim about the whole plan that
# names no change, so the usual claim patterns miss it
BLANKET_RE = re.compile(
    r"\b(?:all|everything(?:'s)?|it'?s all|that'?s all)\s+(?:is\s+|are\s+|has been\s+|now\s+)*(?:fixed|sorted|"
    r"sorted out|back|restored|in place|right where|where you want|exactly (?:as|how|where)|as you (?:laid|wrote|asked|"
    r"said|had|wanted)|the way you (?:laid|wrote|asked|said|had|wanted)|corrected|updated)\b"
    r"|\b(?:back|right) (?:in place|where (?:you|it|they) (?:want|wanted|had|wrote|said|laid))"
    r"|\bexactly (?:as|how) you (?:laid|wrote|asked|said|had|wanted)\b"
    r"|\b(?:i'?ve |i have )?fixed (?:it|that|them|those|everything|all of (?:it|them|that))\b",
    re.IGNORECASE)
_ALREADY_RE = re.compile(r"\b(?:already|still)\b", re.IGNORECASE)


def claims_all_fixed(sentence: str) -> bool:
    """A sentence saying the whole plan is fixed or back as the user had it ("All fixed", "Everything is back in
    place"). Saying it was already so ("everything was already where you had it") claims no change."""
    s = sentence or ""
    return bool(BLANKET_RE.search(s)) and not s.rstrip().endswith("?") and not _ALREADY_RE.search(s)


__all__ = ["failed_items", "handled", "plain_line", "own_up_line", "still_lines", "said_plainly",
           "claims_all_fixed", "BLANKET_RE"]
