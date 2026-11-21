"""Only what the user asked about changes: a move, a new item or the usual hours need their words, like a removal.

Taking an item off already needs the user's words for it (change_asks.py). Other changes didn't, and simulated days
showed what that costs: asked to move one item, the model moved two more nobody named "to make it fit"; asked about
one thing, it shrank another and planned a second copy; a new item nobody mentioned went on the plan; told a bedtime,
it saved work hours as well. Each time the user had to notice and undo it.

Now, when a change is called for the user's message (the tool is the one the model called, not part of Thirties'
own planning: a day proposal, a routine, the morning set-up), it goes ahead only when the message refers to it:

- **moving an item** (update_event): the user asks for a move, in this message or one of their last few that this
  one isn't a no to ("push", "later", "swap", "put ... at", "redo the afternoon": ``move_asked``), or their words
  are about something on the plan at all (an item or a routine named or pointed to: a correction of where it sits
  asks for a move as much as a move word does, and whether it does is the model's reading), and which items
  that means is then the model's reading, as for a removal (change_asks.py), as far as the user's words cover:
  the item they name or point to, the one item the request is about, a group or a tie (move_scope.py); with no such
  words, the message names the item, points back to it, or gives a time for it in words that fit it
  (done_reference.check, stated_times.loosely_named);
- **a new item** (add_event): what the user's words call for is the model's reading, as long as they name it: its
  name shares a word with them that no planned item has ("Pick up the prescription" for "I have to collect my
  prescription at 4"), they ask to plan or fit something in, or they point back to it (``add_basis``). An add in a
  turn about other things ("move the varnish to 8:40") has no basis, and neither does a second copy of an item
  already planned, in its words or others, that the message doesn't name. A second session of a named item
  (again=true) is the model's reading; without it, adding what's already planned keeps it one item
  (change_asks.merge_readd);
- **the usual week** (set_week): work hours change when the message talks about work, bedtime and waking when it
  talks about sleep.

Refused, the model is told what to do instead: leave it, put the named item alongside, suggest it, or ask. The
app's own controls and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# Starting something now moves what it covers out of its way (PlanTools._make_room), but only this far past where
# it was: further than that, it stays alongside and the result says so (an item planned for the afternoon was sent
# to the night, unasked)
MAKE_ROOM_REACH = timedelta(hours=2)
# a change to the whole plan, or a stretch of it, said outright: which items is then the model's to say
BULK_CHANGE_RE = re.compile(
    r"\b(?:re-?do|re-?plan|re-?arrange|re-?shuffle|re-?organi[sz]e|re-?work|re-?jig|shuffle|move (?:things|stuff|it all)"
    r"(?: around)?|(?:the|my) whole (?:day|plan|schedule|morning|afternoon|evening)|(?:fix|sort(?: out)?|clean up|tidy"
    r"(?: up)?|redo|rethink|adjust|rejig|re-?build) (?:the|my|today'?s|this) (?:day|plan|schedule|agenda|morning|"
    r"afternoon|evening|timeline)|make (?:room|space|it (?:all )?fit|everything fit)|(?:fit|squeeze) (?:it|them|"
    r"everything|things) in|work (?:it )?around|plan around|whatever (?:works|fits)|wherever (?:it )?fits|"
    r"move (?:the )?(?:rest|others|everything)|push (?:the )?(?:rest|everything))\b", re.IGNORECASE)
# a yes to what was just offered ("sounds good", "ok do that")
AGREE_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|sounds (?:good|great|fine)|do (?:it|that)|go (?:ahead|for it)|"
                      r"please do|perfect|great|that works|fine|let'?s do (?:it|that))\b", re.IGNORECASE)
# the user asking Thirties to plan (a new item from their lists may then go on)
PLAN_ASK_RE = re.compile(
    r"\b(?:plan|lay (?:\w+ )?out|map (?:\w+ )?out|line (?:\w+ )?up|slot|fill|schedule|organi[sz]e|set up|sort out|"
    r"fit (?:in|it|them|what|some)|suggest|what should i|help me (?:with|plan|figure)|put together|build|find (?:me|"
    r"something|one|a)|pick|choose|recommend|something (?:to|for|easy|quick|fun))\b",
    re.IGNORECASE)
_WORK_RE = re.compile(r"\b(?:work\w*|job|shift|office|clock(?:ing)? (?:in|out)|day off|days off|off (?:at|on|"
                      r"today|tomorrow|fridays?|mondays?)|hours)\b", re.IGNORECASE)
_SLEEP_RE = re.compile(r"\b(?:sleep\w*|slept|bed\w*|wak\w*|woke|awake|up at|get(?:ting)? up|alarm|night\w*|"
                       r"turn in|lights out)\b", re.IGNORECASE)


def _talking(pt: "PlanTools", tool: str) -> Optional[str]:
    """The user's message, when this change is the model's own call of ``tool`` for it; else None."""
    message = getattr(pt, "user_message", None)
    if message is None or getattr(pt, "by_hand", False):
        return None
    from thirties_core.plan_log import is_model_call, outer_tool
    return message if is_model_call() and outer_tool() == tool else None


def _clauses(message: str) -> list:
    from thirties_core.given_times import _clauses as clauses
    return clauses(message)


def _titles(pt: "PlanTools") -> list:
    return [s.title for s in pt.day_plan.spans if s.title and not getattr(s, "shown_only", False)]


def _referred_to(pt: "PlanTools", message: str, title: str, ids: Any = ()) -> bool:
    """The message refers to the item: named (done_reference._named), a clause with a word of its name and no other
    item named outright (stated_times.loosely_named), pointed back to or asked for in bulk (done_reference.check), or
    a yes to whatever was offered when what was offered isn't known here."""
    from thirties_core.done_reference import _named, check, pointed_back
    from thirties_core.stated_times import loosely_named
    titles = _titles(pt)
    others = [t for t in titles if t != title]
    if _named(message, title, others) or any(re.search(rf"\b{re.escape(i)}\b", message) for i in ids if i):
        return True
    if any(loosely_named(clause, title, titles) for clause in _clauses(message)):
        return True
    held, _ = check(pt, title, [i for i in ids if i])
    if held is None:
        return True
    if AGREE_RE.search(message):
        prior = getattr(pt, "prior_exchange", None)
        return prior is None or title in pointed_back(pt, titles + [title])
    return False


def move_refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why update_event can't change this item for the user's message, or None."""
    message = _talking(pt, "update_event")
    if message is None or span.is_envelope or span.source == "calendar":
        return None  # work and sleep, and calendar events, have their own rules
    # --- which items the request means is the model's reading, within what the words cover (move_scope.py) ---
    from thirties_core.move_scope import asked_to_clear, refused, words_about_plan
    if move_asked(pt) or asked_to_clear(pt) or words_about_plan(pt, message):
        return refused(pt, span)
    # --- end within what the words cover ---
    title = pt._title(span)
    if _referred_to(pt, message, title, (span.id, span.task_id)):
        return None
    from thirties_core.unasked import refusal_note  # never announced as a failed move (unasked.py)
    return (f"The user hasn't asked for anything to move, so {title} stays where it is. If something they asked "
            f"for needs this time, put it alongside (alongside=true), or ask them before moving {title}."
            + refusal_note(title))


# the user asking for something to move: a move word, a later or earlier time, or a new place for it
MOVE_ASK_RE = re.compile(
    r"\b(?:mov(?:e|es|ed|ing)|push\w*|shift\w*|slid\w*|bump\w*|delay\w*|postpon\w*|resched\w*|swap\w*|switch\w*|"
    r"defer\w*|earlier|later|sooner|instead|rather|make room|put (?:\w+ ){0,6}?(?:at|to|after|before|back|on|in|"
    r"between|first|last)|(?:go|goes|do|have|start)\w* (?:\w+ ){0,4}?(?:at|after|before|by) (?:\d|noon|midnight)|"
    r"(?:run|runs|running|ran) (?:late|over|long|behind)|behind)\b", re.IGNORECASE)


def move_asked(pt: Any) -> bool:
    """The user asked for something to move, this turn or just before (change_asks.asked)."""
    from thirties_core.change_asks import asked
    return asked(pt, MOVE_ASK_RE) or asked(pt, BULK_CHANGE_RE)


def add_refused(pt: "PlanTools", title: str) -> Optional[str]:
    """Why add_event can't put this new item on for the user's message, or None. What the words ask to add is the
    model's reading, as long as they name it (``add_basis``); a second copy of what's planned, however it's worded,
    needs the user to name it."""
    message = _talking(pt, "add_event")
    if message is None:
        return None
    # --- a copy of an item already planned, in its words or others ("Paddle varnish" for Varnish the paddle) ---
    copies = pt._planned_copies(pt.day_plan, str(title)) + reworded_copies(pt, str(title))
    if copies:
        if not _referred_to(pt, message, title) and not any(_referred_to(pt, message, pt._title(c)) for c in copies):
            return (f"{title} is already on the plan and the user didn't ask for another, so no second copy is "
                    "added. To change when it is, move it with update_event.")
        return None  # named: a second session the model read (again=true), or the one copy moves (merge_readd)
    # --- end a copy ---
    if add_basis(pt, message, title) or _referred_to(pt, message, title):
        return None
    return (f"The user's words this turn are about other things and don't ask for {title}, so it isn't added. "
            "Mention it in your reply if it matters, and add it when they say so.")


# words for changing or ticking what's there, which name nothing new
_CHANGE_WORDS_RE = re.compile(r"(?:mov|push|shift|slid|bump|delay|postpon|resched|swap|switch|defer|drop|remov|skip|"
                              r"cancel|finish|start|tick|mark|plan|schedul)\w*|earlier|later|sooner|instead|rather|"
                              r"redo|time", re.IGNORECASE)


def _own_words(pt: "PlanTools", text: str) -> set:
    """The naming words of the text that belong to no item already planned (what's new in it), as stems."""
    from thirties_core.done_fit import _breaks, _is_its
    from thirties_core.item_facts import _stem
    planned = _titles(pt)
    words = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", str(text or "").lower()))
    return {_stem(w) for w in words if len(w) > 2 and not _breaks(w) and not _CHANGE_WORDS_RE.fullmatch(w)
            and not any(_is_its(w, t) for t in planned)}


def add_basis(pt: "PlanTools", message: str, title: str) -> bool:
    """The user's words name a new item: the model's name for it shares a word with them that no planned item has
    (a word only a planned item has names that item, not a new one), or they ask to plan or fit something in. An add
    in a turn about something else ("move the varnish to 8:40", "the thwart is done") has no basis."""
    if BULK_CHANGE_RE.search(message) or PLAN_ASK_RE.search(message):
        return True
    return bool(_own_words(pt, title) & _own_words(pt, message))


def reworded_copies(pt: "PlanTools", title: str) -> list:
    """Items already planned today in other words that are the same words ("Paddle varnish" and Varnish the
    paddle): only for the model's own adds; the plan's exact copies are PlanTools._planned_copies."""
    if _talking(pt, "add_event") is None:
        return []
    key = _word_key(title)
    if not key:
        return []
    low = str(title or "").strip().lower()
    from thirties_core.models import SpanStatus
    return [s for s in pt.day_plan.spans_in_view() if s.title and s.status == SpanStatus.PLANNED
            and not s.is_envelope
            and s.source != "calendar" and s.title.strip().lower() != low and _word_key(s.title) == key]


def _word_key(title: str) -> frozenset:
    from thirties_core.item_facts import match_words
    return frozenset(w[:-1] if w.endswith("e") and len(w) > 3 else w for w in match_words(title))


def week_refused(pt: "PlanTools", work: bool, sleep: bool) -> Optional[str]:
    """Why set_week can't save these usual hours for the user's message, or None."""
    message = _talking(pt, "set_week")
    if message is None:
        return None
    if work and not _WORK_RE.search(message):
        return ("The user's message doesn't talk about their work hours, so those stay as they are: save only what "
                "they said (bedtime or wake), without work_start, work_end or no_work.")
    if sleep and not _SLEEP_RE.search(message):
        return ("The user's message doesn't talk about bedtime or waking, so those stay as they are: save only the "
                "work hours they gave, without bedtime or wake.")
    return None


__all__ = ["move_refused", "move_asked", "add_refused", "week_refused", "MOVE_ASK_RE", "BULK_CHANGE_RE", "PLAN_ASK_RE",
           "AGREE_RE", "MAKE_ROOM_REACH"]
