"""A move the user asked for changes only the items their words cover.

Which item a move request means is the model's reading (change_asks.py, change_scope.py): the user rarely names an
item word for word, and the model's reading of "could the kiln thing go later" stands. But with that reading free,
simulated days showed the model taking a request about one item as leave to move a second one nobody named, "to
make room" for the first: asked to slide the kiln check an hour later, it also moved the paddle practice that sat
where the kiln check landed. The user had to notice and move it back.

Now each move in a turn is checked against what the user's words cover (``refused``):

- **named**: the request names the item, or a pointer word reaches it (change_scope._referred_to);
- **the model's reading**: the one item the request is about, by the model's own reading, when the words don't
  already name the item(s) it is about. One per move request; more when the words say there are more (a count,
  or a plural pointer);
- **a group**: words that cover several items at once ("everything after the kiln check", "the rest of the
  afternoon", "shift everything", "all of it", "make room") cover any item; a stated window with a clearing word
  ("clear 1:40 to 3:10", "nothing from 2:20 till 4:10") covers the items in it;
- **a tie**: an item the user tied to another before ("right after the kiln check", relations.py) may follow that
  item when it moves, or one the request names.

Anything else stays where it is, and the refusal tells the model so and that it can ask the user in one short
question. Sharing time with what moved, or with a fixed calendar event, is never by itself a reason to move an item.
Sliding what follows later (push_later) goes by the same words: each item it would move must be covered
(``push_refused``), and Thirties slides things along unasked only on group words (``push_said``).

The app's own controls and the background (nobody talking) are never held here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

_MOVE_VERB = (r"(?:mov\w*|push\w*|shift\w*|slid\w*|bump\w*|budg\w*|nudg\w*|scoot\w*|delay\w*|postpon\w*|"
              r"resched\w*|drag\w*)")
# words that cover several items at once: which ones is then the model's to say
GROUP_RE = re.compile(
    rf"\b(?:{_MOVE_VERB}\s+(?:\w+\s+){{0,2}}?(?:everything|the rest|it all|all of (?:it|them)|all (?:that|those|the "
    r"rest)|things|stuff|the others|what(?:'s| is)? (?:there|left|after|next|following))|"
    r"(?:everything|anything|whatever(?:'?s)?|what'?s|all) (?:else )?(?:after|past|from|between|before|until|till|left|"
    r"planned|that follows|following)|everything (?:else|back|later|earlier|along|down|over)|"
    r"(?:the )?rest of (?:the |my |this )?(?:day|morning|afternoon|evening|night|plan|schedule)|all of (?:it|them)|"
    r"(?:the|my) whole (?:day|morning|afternoon|evening|night|plan|schedule|block)|"
    r"make (?:room|space|(?:it all|everything|them all|things) fit)|whatever (?:you|it|we) (?:need|needs|have|has|"
    r"must|take|takes)|what follows|(?:work|plan|schedule) (?:\w+ ){0,2}?around|"
    r"re-?do|re-?plan|re-?arrange|re-?shuffle|re-?organi[sz]e|re-?work|re-?jig|shuffle|"
    r"(?:fix|sort(?: out)?|clean up|tidy(?: up)?|rethink|adjust|re-?build) (?:the|my|today'?s|this) (?:day|plan|"
    r"schedule|agenda|morning|afternoon|evening|timeline))\b", re.IGNORECASE)
# a request about more than one item: (how many, when it says)
_PLURAL_RE = re.compile(r"\b(?:both|either|them|they|those|these|each|two|three|four|five)\b", re.IGNORECASE)
_COUNTS = dict(zip("two three four five both either".split(), (2, 3, 4, 5, 2, 2)))
_TIME = r"(?:\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon|midnight)"
# a stretch of the day the user wants cleared: two times, and a word that clears it in the same sentence
_WINDOW_RE = re.compile(rf"(?:\b(?:between|from)\s+)?\b({_TIME})\s*(?:to|until|till|through|thru|and|-|–)\s*({_TIME})",
                        re.IGNORECASE)
_CLEAR_RE = re.compile(r"\b(?:clear(?:s|ed|ing)?|free(?:d)?(?: up)?|open up|empty|nothing|everything|anything|"
                       r"whatever|out of)\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[.;!?\n]+")


# --- what moved this turn: kept on the tools for the user's message it was for ---

def _record(pt: "PlanTools") -> List[str]:
    message = getattr(pt, "user_message", None)
    kept = getattr(pt, "_moves_this_turn", None)
    if not kept or kept[0] is not message:
        kept = (message, [])
        pt._moves_this_turn = kept
    return kept[1]


def moved(pt: "PlanTools", span: "EventSpan") -> None:
    """After a move of ``span`` the model made for the user's message went through."""
    if _talking(pt) is None:
        return
    ids = _record(pt)
    if span.id not in ids:
        ids.append(span.id)


def _talking(pt: "PlanTools") -> Optional[str]:
    """The user's message, when the change is the model's own call for it; else None."""
    message = getattr(pt, "user_message", None)
    if message is None or getattr(pt, "by_hand", False):
        return None
    from thirties_core.plan_log import is_model_call
    return message if is_model_call() else None


# --- what the user's words cover ---

def _texts(pt: "PlanTools", message: str) -> List[str]:
    """This message, and the user's last ones when this isn't a no (a request just before still counts)."""
    from thirties_core.change_asks import earlier_messages
    from thirties_core.done_words import NO_RE
    return [message] + ([] if NO_RE.match(message) else earlier_messages(pt))


def group_said(pt: "PlanTools", message: str) -> bool:
    return any(GROUP_RE.search(t) for t in _texts(pt, message))


def _clock(text: str, day_start: datetime) -> List[datetime]:
    """The times a clock word can be on the plan's day: both halves of the day when it doesn't say which."""
    from thirties_core.clock import ClockError, resolve_clock
    raw = re.sub(r"\s+", "", text.lower()).replace(".", "")
    if raw in ("noon", "midnight") or raw.endswith(("am", "pm")):
        tries = [raw]
    else:
        tries = [raw + "am", raw + "pm"]
    out = []
    for t in tries:
        try:
            out.append(resolve_clock(t, day_start))
        except ClockError:
            continue
    return out


def windows(pt: "PlanTools", message: str) -> List[Tuple[datetime, datetime]]:
    """The stretches of the day the user's words clear ("clear 1:40 to 3:10")."""
    day_start = pt.day_plan.day_start
    out = []
    for text in _texts(pt, message):
        for sentence in _SENTENCE_RE.split(text):
            if not _CLEAR_RE.search(sentence):
                continue
            for m in _WINDOW_RE.finditer(sentence):
                pairs = [(a, b) for a in _clock(m.group(1), day_start) for b in _clock(m.group(2), day_start)
                         if timedelta(0) < b - a <= timedelta(hours=12)]
                if pairs:
                    out.append(min(pairs, key=lambda p: (p[1] - p[0], p[0])))  # the shortest, then the first
    return out


def asked_to_clear(pt: "PlanTools") -> bool:
    """The user asked for a stretch of the day to be cleared: a move request for what's in it."""
    message = _talking(pt)
    return message is not None and bool(windows(pt, message))


def _in_window(span: "EventSpan", stretches: Sequence[Tuple[datetime, datetime]]) -> bool:
    return any(span.start_dt < b and span.end_dt > a for a, b in stretches)


def _plural(pt: "PlanTools", message: str) -> Optional[int]:
    """How many items the request is about, when its words say more than one (None: as many as it names)."""
    counts = []
    found = False
    for text in _texts(pt, message):
        for m in _PLURAL_RE.finditer(text):
            found = True
            word = m.group(0).lower()
            if word in _COUNTS:
                counts.append(_COUNTS[word])
    if not found:
        return 1
    return min(counts) if counts else None


def _requests(message: str) -> int:
    """How many move requests the message makes: its sentences that ask for a move (at least one)."""
    from thirties_core.change_scope import BULK_CHANGE_RE, MOVE_ASK_RE
    return max(1, sum(1 for s in _SENTENCE_RE.split(message) if MOVE_ASK_RE.search(s) or BULK_CHANGE_RE.search(s)))


def _anchors_only(pt: "PlanTools", message: str, titles: List[str]) -> Set[str]:
    """Items the message names only as where something goes ("after the kiln check"), not as what moves."""
    from thirties_core.done_reference import _named
    from thirties_core.item_facts import mentions
    from thirties_core.relations import TIE_RE
    tie_words = " ".join(m.group(3) for m in TIE_RE.finditer(message))
    if not tie_words:
        return set()
    rest = TIE_RE.sub(" ", message)
    return {t for t in titles if mentions(tie_words, t, titles)
            and not _named(rest, t, [o for o in titles if o != t])}


def about(pt: "PlanTools", message: str) -> List[str]:
    """The items the request names or points to (a calendar event too), other than as where something goes."""
    from thirties_core.change_scope import _referred_to, _titles
    spans = [s for s in pt.day_plan.spans if s.title and not getattr(s, "shown_only", False) and not s.is_envelope]
    titles = _titles(pt)
    anchors = _anchors_only(pt, message, titles)
    out = []
    for s in spans:
        title = pt._title(s)
        if title in anchors or title in out:
            continue
        try:
            if _referred_to(pt, message, title, (s.id, s.task_id)):
                out.append(title)
        except Exception:
            continue
    return out


def _tied_to_moved(pt: "PlanTools", span: "EventSpan", moved_ids: Iterable[str]) -> bool:
    """The user tied this item to one that moved this turn, or that the request names (relations.py): it may
    follow."""
    moved_ids = set(moved_ids)
    if not moved_ids or getattr(pt, "_state_db", None) is None:
        return False
    try:
        facts = pt._facts().get(span.id)
    except Exception:
        return False
    return bool((set(facts.after) | set(facts.before)) & moved_ids)


def refused(pt: "PlanTools", span: "EventSpan") -> Optional[str]:
    """Why the model can't move ``span`` for a move request this turn, or None when the user's words cover it."""
    message = _talking(pt)
    if message is None or span.is_envelope or span.source == "calendar":
        return None
    ids = _record(pt)
    if span.id in ids:
        return None
    stretches = windows(pt, message)
    if stretches:
        if _in_window(span, stretches):
            return None
    elif group_said(pt, message):
        return None
    from thirties_core.change_scope import _referred_to
    title = pt._title(span)
    if _referred_to(pt, message, title, (span.id, span.task_id)):
        return None
    named = [t for t in about(pt, message) if t != title]
    if _tied_to_moved(pt, span, list(ids) + [s.id for s in pt.day_plan.spans if pt._title(s) in named]):
        return None
    # --- the model's reading: the item the request is about, when the words don't already name it ---
    others = {pt._title(s) for s in pt.day_plan.spans if s.id in ids and s.id != span.id}
    taken = others | set(named)
    limit = _plural(pt, message)
    if limit is None or len(taken) < max(limit, _requests(message)):
        return None
    # --- end the model's reading ---
    which = ", ".join(sorted(taken)) or "another item"
    from thirties_core.unasked import refusal_note  # never announced as a failed move (unasked.py)
    return (f"{title} stays where it is: the user's request this turn is about {which}, and their words don't cover "
            f"{title} too (not named or pointed to, not tied to what moved, no words for a group or a stretch of the "
            f"day it's in). Sharing time with what moved, or with a calendar event, isn't a reason to move it: "
            f"leave it, and let what moved sit alongside (alongside=true). If room is really needed, ask the user in "
            f"one short question whether {title} should move." + refusal_note(title))


# --- sliding what follows later ---

def push_said(pt: "PlanTools") -> bool:
    """Thirties may slide what follows along by itself (no push_later from the model): the user's words cover a
    group. Nobody talking, or a change by hand: always."""
    message = _talking(pt)
    return message is None or group_said(pt, message)


def push_refused(pt: "PlanTools", anchor: "EventSpan", followers: Sequence["EventSpan"]) -> Optional[str]:
    """Why sliding ``followers`` later, out of ``anchor``'s way, isn't covered by the user's words, or None."""
    message = _talking(pt)
    if message is None or not followers or group_said(pt, message):
        return None
    stretches = windows(pt, message)
    from thirties_core.change_scope import _referred_to
    ids = list(_record(pt)) + [anchor.id]
    left = []
    for s in followers:
        title = pt._title(s)
        if _in_window(s, stretches) or _tied_to_moved(pt, s, ids) \
                or _referred_to(pt, message, title, (s.id, s.task_id)):
            continue
        left.append(title)
    if not left:
        return None
    names = ", ".join(left)
    return (f"Sliding what follows later would also move {names}, and the user's words this turn don't cover "
            f"{'it' if len(left) == 1 else 'them'}, so nothing slides. Move {pt._title(anchor)} without push_later "
            f"(it can sit alongside, alongside=true); if room is really needed, ask the user in one short question.")


__all__ = ["GROUP_RE", "about", "asked_to_clear", "group_said", "moved", "push_refused", "push_said", "refused", "windows"]
