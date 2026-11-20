"""Which item a removal the user asked for means is the model's to read; a removal nobody asked for doesn't happen.

People rarely name an item word for word ("the glaze bit and that kiln thing" for Glazing: celadon test tiles and
Kilnwash the shelves), and reading their words against the titles kept refusing what the model had read right,
until the user was told to spell the items out. So now:

- **Asked for** (``removal_asked``): the user's message asks for something to come off ("drop", "take ... off",
  "skip", "not doing", ...), or one of their last RECENT_MESSAGES messages did and this one isn't a no ("the two I
  mentioned are still there" after a request that didn't go through). Which items it means is the model's reading,
  and it stands.
- **Said how full the plan is** (``load_said``): "tonight is way too packed", "I need to lighten the evening". Which
  item comes off for it is the model's reading, among the items in the part of the day the words name, or the whole
  day when they name none ("too much today", "I'm swamped") (``load_covers``). A packed van or a cleared-out shelf
  says nothing about the plan.
- **Asked to keep it**: "keep the varnish", "put the oil change back where I had it", "drop the thwart but keep the
  varnish": the item named, or pointed back to, never comes off on those words (``kept``).
- **Talked about the item**: the message names it or points back to it (done_reference.check), even only to grumble
  about it ("why is the varnish coat still there, not happening"). Whether that means taking it off is the model's
  reading; the tool's answer carries a note when the words read more like a complaint about its place, so the
  model can put it back if that's what was meant (``removal_note``).
- **No basis**: nothing the user said this turn or just before asks for anything to come off, says how full the plan
  is, or names or points to the item. It stays. Neither does an item the user only said isn't due yet.

Taking something off the plan never ticks it or files it as dropped by itself: the user's lists have their own
guard (write_guard.py; off_today._off_list). The app's own controls and the background (nobody talking) are never
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

# talking about an item's place, or how it got there, without asking for it to go
COMPLAINT_RE = re.compile(r"\b(?:(?:why|how come) (?:did|would|is|are|was|were|has|have|it|the)\b|never (?:said|asked|told)|did(?:n'?t| not) "
                          r"(?:say|ask|tell|want)|not what i (?:said|asked|wrote)|put (?:\w+ ){0,4}back|back (?:to|where|"
                          r"the way|how)|where i (?:wrote|put|had|said|left)|the way i (?:wrote|had|said|laid)|"
                          r"leave (?:\w+ ){0,3}(?:there|alone|where|be|as)|keep (?:\w+ ){0,3}(?:there|where|as)|"
                          r"stay(?:s)? (?:where|there|put|as)|undo|revert)", re.IGNORECASE)


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


# the user saying the plan or the day is too full, or asking for less on it: grounds for the model to take something
# off what they said is too full. A full van or a cleared-out shelf is no such thing.
_STRETCH = (r"(?:today|tonight|(?:the |my |this |today'?s |tonight'?s )?(?:day|plan|schedule|agenda|calendar|"
            r"evening|morning|afternoon|night|plate))")
_FULL = (r"(?:too (?:much|many|full|packed|busy|crowded|crammed|long)|(?:over)?packed|overloaded|overbooked|"
         r"swamped|slammed|crammed|jammed|stuffed|full|busy|a lot)")
LOAD_SAID_RE = re.compile(
    rf"\b(?:{_STRETCH}(?:'s| is| are| looks| feels| seems| has| got| getting)?\s+(?:\w+\s+){{0,3}}?{_FULL}|"
    rf"too (?:much|many (?:things|items))(?: (?:on|for|in))? {_STRETCH}|too much (?:to do|going on)|"
    r"(?:i'?m|i am|we'?re|we are|feeling|i feel) (?:\w+ ){0,2}?(?:swamped|slammed|overloaded|overwhelmed|"
    r"stretched thin|overbooked)|"
    rf"lighten (?:up )?{_STRETCH}|lighten (?:the|my) load|thin (?:out )?{_STRETCH}|thin (?:it|things) out|"
    rf"clear (?:out |up )?{_STRETCH}|(?:less|fewer things) on {_STRETCH}|cut (?:things|it) back|"
    rf"cut back on (?:things|stuff|{_STRETCH})|"
    r"can'?t (?:fit|manage|handle|get through|do) (?:it all|everything|all (?:of )?(?:this|that|it|them)))\b",
    re.IGNORECASE)
# the stretch of the day load words name: (from hour, to hour)
_LOAD_PARTS = (("morning", 0, 12), ("afternoon", 12, 17), ("evening", 17, 30), ("tonight", 17, 30),
               ("night", 17, 30))


def load_said(pt: Any) -> bool:
    """The user says the plan or the day is too full or asks for less on it, this turn or just before."""
    return asked(pt, LOAD_SAID_RE)


def _load_hours(pt: Any) -> List[tuple]:
    """The stretches of the day the user's load words are about, as (from hour, to hour); the whole day when they
    name no part of it ("too much today", "I'm swamped")."""
    message = getattr(pt, "user_message", None)
    if not message:
        return []
    from thirties_core.done_words import NO_RE
    texts = [message] + ([] if NO_RE.match(message) else earlier_messages(pt))
    out = []
    for text in texts:
        for m in LOAD_SAID_RE.finditer(text):
            said = m.group(0).lower()
            parts = [(a, b) for name, a, b in _LOAD_PARTS if re.search(rf"\b{name}\b", said)]
            out += parts or [(0, 30)]
    return out


def load_covers(pt: Any, span: Optional["EventSpan"] = None) -> bool:
    """The user's load words cover this item: they name the part of the day it's in ("tonight is way too
    packed" for an item at 7:20 PM), or the whole day. With no item (a line on today's list with no time), only
    words about the whole day cover it."""
    hours = _load_hours(pt)
    if span is None or getattr(span, "start_dt", None) is None:
        return (0, 30) in hours
    day = getattr(getattr(pt, "day_plan", None), "target_date", None)
    at = span.start_dt.hour + (24 if day is not None and span.start_dt.date() > day else 0)
    return any(a <= at < b for a, b in hours)


def removal_grounds(pt: Any, span: Optional["EventSpan"] = None) -> bool:
    """The user's words give the model grounds to take this item off without naming it: they ask for something to
    come off, or say the part of the day it's in is too full (``load_covers``)."""
    return removal_asked(pt) or load_covers(pt, span)


# --- asked to keep it, or to put it back: never a removal ---
KEEP_RE = re.compile(
    r"\b(?:keep(?!\s+(?:\w+\s+){0,5}?(?:off|out|away)\b)|leave (?:\w+\s+){0,4}?(?:there|alone|where|be|as (?:it|is)|"
    r"on|in place)\b|stay(?:s)? (?:where|there|put|on)|(?:put|bring) (?:\w+\s+){0,5}?back|restore|"
    r"don'?t (?:drop|remove|take|skip|cancel|touch|lose))", re.IGNORECASE)


def kept(pt: Any, title: str, ids: Any = ()) -> str:
    """Why the user's words keep this item: they ask to keep it, leave it, or put it back ("put the oil change back
    where I had it", "drop the thwart but keep the varnish"), in words that name it or point back to it naming
    nothing else. "" when they don't."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or not message or not KEEP_RE.search(message):
        return ""
    from thirties_core.done_reference import _named, check
    from thirties_core.write_guard import _CLAUSE_RE, _POINTER_RE
    titles = []
    for s in getattr(getattr(pt, "day_plan", None), "spans", None) or []:
        try:
            titles.append(pt._title(s))
        except Exception:
            titles.append(s.title)
    others = [t for t in titles if t and t != title]
    for clause in (c for c in _CLAUSE_RE.split(message) if c and c.strip()):
        if not KEEP_RE.search(clause):
            continue
        if _named(clause, title, others):
            return _kept_why(title)
        if _POINTER_RE.search(clause) and not any(_named(clause, o, [x for x in others if x != o] + [title])
                                                  for o in others):
            try:
                held, _ = check(pt, title, [i for i in ids if i])
            except Exception:
                held = "?"
            if held is None:
                return _kept_why(title)
    return ""


def _kept_why(title: str) -> str:
    return (f"The user asked to keep {title} or put it back, so it stays on the plan. If they want it somewhere "
            "else, move it there.")
# --- end asked to keep it ---


def removal_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why this item can't come off the plan on the user's words this turn, or None."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return None
    # --- a question, or a pointer at an item's parts, takes nothing off (said_for_it.py) ---
    from thirties_core.said_for_it import removal_held
    held_here = removal_held(pt, pt._title(span))
    if held_here:
        return held_here
    # --- end a question, or a pointer at parts ---
    title = pt._title(span)
    keep = kept(pt, title, (span.id, span.task_id))  # asked to keep it or put it back: never a removal
    if keep:
        return keep
    if removal_grounds(pt, span):
        return None  # which items the request means is the model's reading, within what the words cover
    from thirties_core.done_reference import check
    held, _ = check(pt, title, [i for i in (span.id, span.task_id) if i])
    if held:
        return (f"The user hasn't asked for anything to come off the plan, so {title} stays. Take something off "
                "only when they ask; if you think they want it gone, ask them in your own words.")
    # --- "not yet" is about when, not whether: it stays (a simulated day lost an item the user said wasn't due yet) ---
    if NOT_YET_RE.search(message):
        return (f"The user said it isn't time for {title} yet, not that it's off, so it stays on the plan. Leave it "
                "where it is, or move it later if they say when.")
    # --- end "not yet" ---
    return None  # their words are about this item: what they mean for it is the model's reading


def removal_note(pt: "PlanTools", span: "EventSpan") -> str:
    """A note on a removal the model read from a complaint about the item's place rather than an ask to take it
    off. The removal stands; the note lets the model put it back if that was the meaning. "" when there's nothing
    to note."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or not message or removal_grounds(pt, span):
        return ""
    title = pt._title(span)
    if COMPLAINT_RE.search(message):
        return (f" (Note: the user's words are about where {title} was or how it got there. If they meant it to "
                "move rather than come off, put it where they want it.)")
    return ""


# the item's time hasn't come: "the kiln run isn't yet", "not time for the paddle yet", "not now, later"
NOT_YET_RE = re.compile(r"\bnot (?:\w+ ){0,3}yet\b|\b(?:isn'?t|is not|aren'?t|are not) (?:\w+ ){0,3}yet\b|"
                        r"\bnot (?:right )?now\b|\bnot time (?:for|to)\b|\btoo (?:early|soon) for\b", re.IGNORECASE)


__all__ = ["removal_refused", "removal_asked", "removal_grounds", "removal_note", "load_said", "load_covers", "kept",
           "KEEP_RE", "asked", "recent_said",
           "earlier_messages", "COMPLAINT_RE", "LOAD_SAID_RE", "RECENT_MESSAGES"]


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
