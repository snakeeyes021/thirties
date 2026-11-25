"""Which item a removal the user asked for means is the model's to read; a removal nobody asked for doesn't happen.

People rarely name an item word for word ("the glaze bit and that kiln thing" for Glazing: celadon test tiles and
Kilnwash the shelves), and reading their words against the titles kept refusing what the model had read right,
until the user was told to spell the items out. So now:

- **Asked for** (``removal_asked``): the user's message asks for something to come off ("drop", "take ... off",
  "skip", "not doing", ...), or one of their last RECENT_MESSAGES messages did and this one isn't a no ("the two I
  mentioned are still there" after a request that didn't go through). Which items it means is the model's reading,
  and it stands.
- **Asked to keep it**: "keep the varnish", "put the oil change back where I had it", "revert the varnish", "drop
  the thwart but keep the varnish and the sanding": the item named, or pointed back to, in a clause asking to keep
  it, leave it or have its earlier place back (or in the list such a clause starts), never comes off on those
  words. A keep said in one of the recent messages a removal ask still counts from holds too, unless this message
  names the item (``kept``).
- **Talked about the item**: the message names it or points back to it (done_reference.check), even only to grumble
  about it ("why is the varnish coat still there, not happening"). Whether that means taking it off is the model's
  reading; the tool's answer carries a note when the words read more like a complaint about its place, so the
  model can put it back if that's what was meant (``removal_note``).
- **No basis**, on a later day's plan: nothing the user said this turn or just before asks for anything to come
  off, or names or points to the item. It stays, whatever they say about how full the day is ("tonight is way too
  packed" names no item): which item to offer up is then a question for the user. Today's plan and list can be
  put back, so there the model's reading stands, and the answer says what came off.

Taking something off the plan never ticks it or files it as dropped by itself: the user's lists have their own
guard (write_guard.py; off_today.leave_list). The app's own controls and the background (nobody talking) are never
held here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# how many of the user's messages before this one a request still counts for
RECENT_MESSAGES = 2

# asking for an item's earlier place back: "back to 8 like I had it", "the way I had it", "revert it", "undo that"
_RESTORE = (r"back (?:to|where|the way|how)|where i (?:wrote|put|had|said|left)|the way i (?:wrote|had|said|laid)|"
            r"undo|revert")
# talking about an item's place, or how it got there, without asking for it to go
COMPLAINT_RE = re.compile(r"\b(?:(?:why|how come) (?:did|would|is|are|was|were|has|have|it|the)\b|never (?:said|asked|told)|did(?:n'?t| not) "
                          r"(?:say|ask|tell|want)|not what i (?:said|asked|wrote)|put (?:\w+ ){0,4}back|"
                          rf"{_RESTORE}|"
                          r"leave (?:\w+ ){0,3}(?:there|alone|where|be|as)|keep (?:\w+ ){0,3}(?:there|where|as)|"
                          r"stay(?:s)? (?:where|there|put|as))", re.IGNORECASE)


def recent_said(messages: Any) -> List[str]:
    """The user's last RECENT_MESSAGES visible messages before this turn, newest first."""
    out = []
    for m in reversed(list(messages or [])):
        text = str(m.get("content") or "")
        if m.get("role") == "user" and text.strip() and not m.get("hidden") and not text.startswith("[System check"):
            out.append(text)
            if len(out) >= RECENT_MESSAGES:
                break
    return out


def earlier_messages(pt: Any) -> List[str]:
    """What the user said just before this turn: the recent messages the conversation set, else the last one."""
    recent = getattr(pt, "recent_said", None)
    if recent:
        return list(recent)[:RECENT_MESSAGES]
    said, _ = getattr(pt, "prior_exchange", None) or ("", "")
    return [said] if said else []


def asked(pt: Any, words: Any) -> bool:
    """The user asks for this kind of change (``words`` a pattern for it): in this message, or in a recent one when
    this message isn't a no."""
    message = getattr(pt, "user_message", None)
    if not message:
        return False
    if words.search(message):
        return True
    from thirties_core.done_words import NO_RE
    return not NO_RE.match(message) and any(words.search(m) for m in earlier_messages(pt))


def _removal_words() -> Any:
    from thirties_core.leave_today import SKIP_ASK_RE
    from thirties_core.plan_tools import REMOVE_WORDS_RE
    return re.compile(f"(?:{REMOVE_WORDS_RE.pattern})|(?:{SKIP_ASK_RE.pattern})", re.IGNORECASE)


def removal_asked(pt: Any) -> bool:
    """The user asked for something to come off the plan, this turn or just before."""
    return asked(pt, _removal_words())


# --- asked to keep it, or to put it back: never a removal ---
KEEP_RE = re.compile(
    r"\b(?:keep(?!\s+(?:\w+\s+){0,5}?(?:off|out|away)\b)|leave (?:\w+\s+){0,4}?(?:there|alone|where|be|as (?:it|is)|"
    r"on|in place)\b|stay(?:s)? (?:where|there|put|on)|(?:put|bring) (?:\w+\s+){0,5}?back|restore|"
    rf"don'?t (?:drop|remove|take|skip|cancel|touch|lose)|{_RESTORE})", re.IGNORECASE)


def kept(pt: Any, title: str, ids: Any = ()) -> str:
    """Why the user's words keep this item: they ask to keep it, leave it, or have its earlier place back ("put the
    oil change back where I had it", "revert the varnish", "drop the thwart but keep the varnish and the sanding"),
    in words that name it or point back to it naming nothing else. A keep in one of the recent messages a removal
    ask still counts from (``earlier_messages``) holds too, until a message after it names the item. "" when they
    don't."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or not message:
        return ""
    titles = []
    for s in getattr(getattr(pt, "day_plan", None), "spans", None) or []:
        try:
            titles.append(pt._title(s))
        except Exception:
            titles.append(s.title)
    others = [t for t in titles if t and t != title]
    said = _kept_in(pt, message, title, others, ids)
    for earlier in earlier_messages(pt):  # newest first
        if said is not None:
            break
        said = _kept_in(pt, earlier, title, others, ())
    return _kept_why(title) if said else ""


def _kept_in(pt: Any, text: str, title: str, others: List[str], ids: Any) -> Optional[bool]:
    """True when the text asks to keep the item; False when it names the item otherwise (it speaks for it, so an
    older keep no longer holds); None when it says nothing about it. Each clause is read on its own, and a keep
    carries on through the list it starts ("keep the varnish and the sanding"), up to a clause with other words in
    it ("keep the varnish and take the sanding out"). A pointer ("keep it") counts only when ``ids`` are given: in
    this turn's message, read with done_reference.check."""
    from thirties_core.done_reference import _named, check
    from thirties_core.write_guard import _CLAUSE_RE, _POINTER_RE
    named_here = False
    keeping = False
    for clause in (c for c in _CLAUSE_RE.split(text or "") if c and c.strip()):
        named = _named(clause, title, others)
        named_here = named_here or named
        if KEEP_RE.search(clause):
            keeping = True
        elif not (keeping and _only_items(clause, [title] + others)):
            keeping = False
            continue
        if named:
            return True
        if ids and KEEP_RE.search(clause) and _POINTER_RE.search(clause) \
                and not any(_named(clause, o, [x for x in others if x != o] + [title]) for o in others):
            try:
                held, _ = check(pt, title, [i for i in ids if i])
            except Exception:
                held = "?"
            if held is None:
                return True
    return False if named_here else None


def _only_items(clause: str, titles: List[str]) -> bool:
    """The clause is only another entry in a list: every word in it is an item's word or a word that goes with a
    name ("the sanding", "my thwart too"). One with any other word in it ("take the sanding out") says something
    else."""
    from thirties_core.done_fit import _is_its
    words = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", clause.lower()))
    return bool(words) and all(w in _WITH_A_NAME or any(_is_its(w, t) for t in titles) for w in words)


# the words that go with an entry in a list of items
_WITH_A_NAME = {"the", "a", "an", "my", "our", "that", "this", "those", "these", "too", "also", "both", "as", "well"}


def _kept_why(title: str) -> str:
    return (f"The user asked to keep {title} or put it back, so it stays on the plan. If they want it somewhere "
            "else, move it there.")
# --- end asked to keep it ---


def removal_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why this item can't come off the plan on the user's words this turn, or None. Only the user's ask to keep it
    or put it back holds it here; whether their words (a question, a grumble, a "not yet") take it off is the
    model's reading. A removal with no basis at all from a later day is held before this
    (plan_tools.remove_event)."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None
    title = pt._title(span)
    keep = kept(pt, title, (span.id, span.task_id))  # asked to keep it or put it back: never a removal
    if keep:
        return keep
    return None  # which items their words mean, and what they mean for each, is the model's reading


def removal_note(pt: "PlanTools", span: "EventSpan") -> str:
    """A note on a removal the model read from a complaint about the item's place rather than an ask to take it
    off. The removal stands; the note lets the model put it back if that was the meaning. "" when there's nothing
    to note."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or not message or removal_asked(pt):
        return ""
    title = pt._title(span)
    if COMPLAINT_RE.search(message):
        return (f" (Note: the user's words are about where {title} was or how it got there. If they meant it to "
                "move rather than come off, put it where they want it.)")
    return ""


__all__ = ["removal_refused", "removal_asked", "removal_note", "kept", "KEEP_RE", "asked", "recent_said",
           "earlier_messages", "COMPLAINT_RE", "RECENT_MESSAGES"]


def merge_readd(pt: "PlanTools", copies: Any, start: Any, end: Any = None, duration: Any = None) -> Optional[str]:
    """Adding an item the day already has planned, without again=true: it stays one item. When the user's words
    this turn name it and ask for another time, the one copy moves there (re-adding it at the time the user wants
    is moving it); at the time it already has, there's nothing to add. None when it isn't that clear (two copies,
    or the user didn't name it): the caller refuses with how to move it."""
    from thirties_core.clock import thirty_of
    from thirties_core.item_facts import mentions
    from thirties_core.plan_tools import ToolError  # the tools' own refusal (assistant_tools.ToolError)
    copies = list(copies or [])
    if len(copies) != 1:
        return None
    span = copies[0]
    message = getattr(pt, "user_message", None)
    title = pt._title(span)
    others = [pt._title(s) for s in pt.day_plan.spans if s.id != span.id]
    try:
        wanted = pt._start(start) if start not in (None, "") else None
    except Exception:
        return None
    if wanted is None:
        return None
    if thirty_of(wanted) == thirty_of(span.start_dt):
        raise ToolError(f"{pt.describe(span)} is already planned [id {span.id}], at that time: there's nothing to add, "
                        "and it stays one item.")
    if getattr(pt, "by_hand", False) or message is None or not mentions(message, title, others):
        return None
    args = {"start": start}
    if end not in (None, ""):
        args["end"] = end
    elif duration not in (None, ""):
        args["duration"] = duration
    out = pt.update_event(span.id, **args)
    return f"{out} (One copy of it was on the plan already; that copy moved, so there's still one.)"


__all__ += ["merge_readd"]
