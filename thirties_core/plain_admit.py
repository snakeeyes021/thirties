"""What a change that didn't happen leaves as it was, said in the reply itself, like a person would say it.

When a change was refused or didn't happen and the reply said otherwise, a note used to be added under the reply
("(Bake bread is still on the plan.)", "(That move didn't happen: ...)", "(One of those changes didn't happen.)"):
a second voice, below the first one that had just claimed the opposite. Now the reply says it in its own words:

- it is sent back once, with the plain sentence it should say ("Bake bread is still at 5:00 PM; I couldn't move
  it."), and told to write it fresh: the user never saw the draft, so there's nothing to correct;
- if the rewrite still doesn't say it, the false sentence itself is replaced with that plain sentence, in place;
  nothing is added under the reply, and the rest of it stays as written.

Only the items that didn't change are named: in a reply about two changes, one refused, the other stands.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# the parenthetical a check adds to its fact for the model ("(that change was refused)")
_ASIDE_RE = re.compile(r"\s*\([^)]*\)\s*$")

# the tools whose refusal leaves an item where it was, and how that's said
_MOVING = {"update_event", "swap_events", "push_back", "doing_now", "still_doing", "redraw", "put_routine",
           "apply_day_proposal"}
_OFF = {"remove_event", "clear_time"}
_ADD = {"add_event", "add_to_today"}
_MARK = {"log_outcome", "mark_done"}
# not an item: the change was made, but the lists in Joplin lag behind
LISTS = "<lists>"


def _span(tools: Any, title: str) -> Any:
    plan = getattr(tools, "day_plan", None)
    if plan is None or not title:
        return None
    want = title.strip().lower()
    for span in plan.spans:
        try:
            name = tools._title(span)
        except Exception:
            name = span.title
        if want in (str(name).strip().lower(), str(span.title).strip().lower()):
            return span
    return None


def where(span: Any) -> str:
    """"at 5:00 PM" for a time the user or a calendar gave, or one on a block's start, else "in the 1:00 PM block":
    as the schedule shows it."""
    from thirties_core.clock import format_clock
    from thirties_core.shown_times import shown_start, start_exact
    if start_exact(span) or shown_start(span) == span.start_dt:
        return f"at {format_clock(span.start_dt)}"
    return f"in the {format_clock(shown_start(span))} block"


def still_line(tools: Any, title: str, tool: str) -> str:
    """The plain sentence for one change that didn't happen ("Bake bread is still at 5:00 PM; I couldn't move it.")."""
    if tool == LISTS:
        return "Your lists haven't caught up with that yet."
    if not title:
        return "I couldn't make one of those changes."
    span = _span(tools, title)
    if tool in _ADD and span is None:
        return f"{title} isn't on the plan; I couldn't add it." if tool == "add_event" else \
            f"{title} isn't on today's list; I couldn't add it."
    if tool in _MARK:
        return f"{title} isn't marked done yet; I couldn't mark it."
    if span is None:
        return f"I couldn't change {title}."
    if tool in _OFF:
        return f"{title} is still on the plan {where(span)}; I couldn't take it off."
    if tool in _MOVING:
        return f"{title} is still {where(span)}; I couldn't move it."
    return f"{title} is still {where(span)}; I couldn't change it."


def from_fact(fact: str) -> str:
    """A check's fact about an item ("Bake bread is still at 5:00 PM", "Soup isn't recorded as done") as the plain
    sentence the reply should say. A fact about a move the user didn't ask for says nothing (unasked.py)."""
    from thirties_core.unasked import NOT_ASKED
    if str(fact or "").rstrip().endswith(NOT_ASKED):
        return ""
    fact = _ASIDE_RE.sub("", fact or "").strip().rstrip(".")
    if not fact or fact == "nothing":
        return "I couldn't move that just now, so nothing has moved yet."
    if fact.endswith(" isn't recorded as done"):
        return fact[: -len(" isn't recorded as done")] + " isn't marked done yet."
    if " is still on the plan" in fact or " is still on today's list" in fact:
        return f"{fact}; I couldn't take it off."
    if " is still " in fact:
        return f"{fact}; I couldn't move it."
    return f"{fact}."


def replace_sentences(reply: str, new_for: Callable[[str], Optional[str]]) -> Tuple[str, bool]:
    """The reply with each sentence ``new_for`` gives a replacement for replaced in place ("" drops it), lines and
    the rest kept; and whether anything was replaced."""
    changed = False
    lines = []
    for line in (reply or "").split("\n"):
        kept = []
        for part in _SENTENCE_RE.split(line):
            new = new_for(part) if part.strip() else None
            if new is None:
                kept.append(part)
            else:
                changed = True
                if new:
                    kept.append(new)
        lines.append(" ".join(kept).rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, changed


_PARTS_RE = re.compile(r",|;|\band\b|\bthen\b|\bplus\b", re.IGNORECASE)


def _true_part(sentence: str, failed: Sequence[str], others: Sequence[str], claim_re: "re.Pattern") -> str:
    """What's left of a sentence about several changes once the parts about the failed ones are out ("I moved
    the van to 6 and the dentist to 3" with the dentist refused: "I moved the van to 6."), or "" when no claim of
    its own is left."""
    from thirties_core.item_facts import mentions
    parts = _PARTS_RE.split(sentence)
    kept = [p for p in parts if not any(t and mentions(p, t, others) for t in failed)]
    if not kept or len(kept) == len(parts):
        return ""
    text = " and ".join(p.strip() for p in kept if p.strip()).rstrip(" .!,;")
    return f"{text}." if text and claim_re.search(text) else ""


def _title_of(fact: str) -> str:
    m = re.match(r"^(.+?) (?:is|isn't) ", _ASIDE_RE.sub("", fact or ""))
    return m.group(1) if m else ""


def say_in_place(reply: str, facts_of: Callable[[str], List[str]], claim_re: "re.Pattern",
                 titles: Sequence[str] = ()) -> Tuple[str, bool]:
    """The reply with each sentence the checks find false (``facts_of`` gives its facts) replaced by its true part
    and the plain sentences for what didn't change; whether anything was replaced."""
    from thirties_core.plain_corrections import in_app_words
    titles = list(titles)

    def new_for(sentence: str) -> Optional[str]:
        facts = facts_of(sentence)
        if not facts:
            return None
        lines = list(dict.fromkeys(in_app_words([x for x in (from_fact(f) for f in facts) if x], titles)))
        keep = _true_part(sentence, [_title_of(f) for f in facts], titles, claim_re)
        return " ".join(([keep] if keep else []) + lines)
    return replace_sentences(reply, new_for)


def say_failed(tools: Any, reply: str, items: Sequence[Tuple[str, str]], claim_re: "re.Pattern",
               others: Iterable[str] = ()) -> str:
    """For each item (title, tool) whose change failed, the plain sentence takes the claim's place: the sentence
    naming it (keeping its true part), or failing that a claim naming nothing (a blanket "done" over several), or
    failing that it follows the first claim on its line."""
    from thirties_core.item_facts import mentions
    others = list(others)
    claims = [s for line in (reply or "").split("\n") for s in _SENTENCE_RE.split(line)
              if s.strip() and claim_re.search(s) and not s.rstrip().endswith("?")]
    if not claims:
        return reply
    named = {s: [(t, tool) for t, tool in items if t and mentions(s, t, others)] for s in claims}
    unnamed = [(t, tool) for t, tool in items if not any((t, tool) in v for v in named.values())]
    blanket = next((s for s in claims if not any(mentions(s, o, others) for o in others)), None)
    new: Dict[str, str] = {}
    for sentence, mine in named.items():
        if not mine:
            continue
        lines = list(dict.fromkeys(still_line(tools, t, tool) for t, tool in mine))
        keep = _true_part(sentence, [t for t, _ in mine], others, claim_re)
        new[sentence] = " ".join(([keep] if keep else []) + lines)
    if unnamed:
        lines = list(dict.fromkeys(still_line(tools, t, tool) for t, tool in unnamed))
        if blanket is not None:
            new[blanket] = " ".join(lines)
        else:
            first = claims[0]
            new[first] = new.get(first, first) + " " + " ".join(lines)
    done: set = set()

    def new_for(sentence: str) -> Optional[str]:
        if sentence in new and sentence not in done:
            done.add(sentence)
            return new[sentence]
        return "" if sentence in new else None
    out, _ = replace_sentences(reply, new_for)
    return out


def check_text(lines: Sequence[str]) -> str:
    """The rewrite's instruction: the plain sentences to say, and that the draft was never seen."""
    lines = [line for line in lines if line]
    if not lines:  # only moves nobody asked for (unasked.py)
        return ("Leave out the moves the user didn't ask for; say nothing about them. Write the reply fresh: the "
                "user never saw this draft, so don't correct anything or mention an earlier message.")
    said = " ".join(f'"{line}"' for line in lines)
    return (f"Say it in your reply, plainly, as the plan has it: {said} Write the reply fresh: the user never saw "
            "this draft, so don't correct anything or mention an earlier message.")


__all__ = ["still_line", "from_fact", "replace_sentences", "say_in_place", "say_failed", "check_text", "where"]
