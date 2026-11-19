"""A done taken back: only the item the user names, and then for real.

"Nope, the paddles still need their wax: reopen them", "I never got to the paddles; please untick them": the
user asks for a write. The item goes back to planned and its Joplin line is unticked (ListTools.reopen_item),
whichever way the model records it: by reopen_item, or by log_outcome with the item reported as open ("open",
"planned", "not done").

During a turn it needs the user's words, as a done does:

- **The ask**: words to untick, reopen, put back or mark open, or words saying the item didn't happen (a "not",
  a "never", a mistake owned up to).
- **The item**: a clause naming it or giving its id; or a clause with a pointer and no other item in it, when the
  last message the user saw (or their previous one) named the item.

Nothing else is reopened. The app's own controls and the background aren't judged here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable, List, Optional

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

# what log_outcome is sometimes given for "open again"
REOPEN_STATUSES = {"open", "planned", "not done", "not_done", "notdone", "undone", "undo", "reopen", "reopened",
                   "unticked", "untick", "unchecked", "unmarked", "todo", "to do", "to_do", "pending", "not started"}

# an ask to take a done back, or words saying it didn't happen
ASK_RE = re.compile(
    r"\bun(?:tick|check|mark|do|done)\w*\b|\bre-?open\w*\b|\bput\b[^.!?\n]{0,40}?\bback\b|"
    r"\bback (?:on|to|as|onto)\b|\bmark\w*\b[^.!?\n]{0,30}?\b(?:open|not done|undone|unfinished|to ?do|planned)\b|"
    r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|isn'?t|aren'?t|wasn'?t|weren'?t|ain'?t)\b|n't\b|"
    r"\bby mistake\b|\bmistake\b|\bwrong(?:ly)?\b|\bshouldn'?t have\b|\btake\b[^.!?\n]{0,20}?\bback\b|"
    r"\bstill (?:to do|open|needs?|has to)\b|\bopen again\b", re.IGNORECASE)


def is_reopen_status(status) -> bool:
    return str(status or "").strip().lower().replace("-", " ") in REOPEN_STATUSES


def held(pt: "PlanTools", names: Iterable[str], ids: Iterable[str] = ()) -> str:
    """Why this reopen can't go ahead on the user's words this turn; "" when it can (or nobody is talking)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    names = [n for n in dict.fromkeys(names) if n]
    shown = names[0] if names else "that item"
    refusal = (f"not reopened: the user's words don't ask for {shown} to be opened again. Only an item they name "
               "and say isn't done, or ask to untick or put back, is reopened; ask them if that's what they meant.")
    if not names:
        return refusal
    from thirties_core.done_counts import without_counts
    from thirties_core.done_reference import MOST_POINTED, POINTER_RE, _named, _titles, pointed_back
    from thirties_core.started import _SENTENCE_RE, _WH_RE
    from thirties_core.write_guard import _CLAUSE_RE
    titles = list(dict.fromkeys(_titles(pt) + names))
    others = [t for t in titles if t not in names]
    text = re.sub(r"[‘’`]", "'", message)
    ids = [i for i in ids if i]
    if any(re.search(rf"\b{re.escape(i)}\b", text) for i in ids) and ASK_RE.search(text):
        return ""
    # a question asking why or what isn't an ask ("why is it unticked?"); "can you untick it?" is
    sentences = [s for s in _SENTENCE_RE.findall(text)
                 if s.strip() and not (_WH_RE.match(s) and s.rstrip().endswith("?"))]
    clauses = [c for s in sentences for c in _CLAUSE_RE.split(s) if c and c.strip()]

    def names_mine(c: str) -> bool:
        return any(_named(without_counts(c), n, titles) for n in names)

    def names_other(c: str) -> bool:
        return any(_named(without_counts(c), o, titles) for o in others)

    mine: List[str] = [c for c in clauses if names_mine(c)]
    if mine:
        # a clause pointing back right after one naming it ("..., put them back") is about it too
        mine += [c for c in clauses if POINTER_RE.search(c) and not names_other(c)]
    else:
        pointed = pointed_back(pt, titles)
        if any(n in pointed for n in names) and len(set(pointed)) <= MOST_POINTED and not any(
                names_other(c) for c in clauses):
            mine = [c for c in clauses if POINTER_RE.search(c) or not names_other(c)]
    if any(ASK_RE.search(c) for c in mine):
        return ""
    return refusal


def untick_refused(message: Optional[str], title: str) -> str:
    """For the last check before Joplin: an untick in a turn needs an ask to reopen (or a not-done) in the user's
    words, about this item or pointing back with nothing else named. "" when it can be written."""
    if not message:
        return ""
    from thirties_core.item_facts import match_words
    from thirties_core.write_guard import _CLAUSE_RE, _POINTER_RE, _about
    text = re.sub(r"[‘’`]", "'", message)
    clauses = [c for c in _CLAUSE_RE.split(text) if c and c.strip()]
    asks = [c for c in clauses if ASK_RE.search(c)]
    if asks and (any(_about(c, title, []) for c in clauses)
                 or (any(_POINTER_RE.search(c) for c in clauses) and not _names_anything(text, match_words))):
        return ""
    return (f"not written: the user's words don't ask for {title} to be opened again, so its line stays ticked.")


# words of a reopen ask that name no item
_ASK_WORDS = frozenset(
    # asking for it
    "reopen open again untick uncheck unmark undo undone put back take mark marked tick ticked list "
    # saying it didn't happen
    "never not no nope yet still done finish finished mistake wrong wrongly oops sorry did do made was were "
    "didnt didn havent haven isnt isn arent aren wasnt wasn "
    # pointing, and asking nicely
    "it that this them they those these one please pls plz actually really".split())


def _names_anything(text: str, match_words) -> bool:
    from thirties_core.item_facts import _stem
    return bool(match_words(text) - {_stem(w) for w in _ASK_WORDS} - _ASK_WORDS)


__all__ = ["ASK_RE", "REOPEN_STATUSES", "held", "is_reopen_status", "untick_refused"]
