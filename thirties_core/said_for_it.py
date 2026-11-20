"""What the user's words do for one item: a done only when said, a removal only when asked, never by pointing past it.

- **Begun or next is no done.** "the mugs are trimmed, on to the gunwales", "mugs done, gunwales up next",
  "finished the mugs. Now the gunwales": the done is the mugs'; the gunwales have only begun, or come next. A
  stretch about an item that starts with an onward word ("on to", "moving on to", "time for", "now", "starting",
  "working on", "about to", "heading into") or that says it comes next, with no done word of its own, is never a
  done for it, whatever else the message says (``begun_or_next``).
- **A plain question changes nothing.** An item named only in questions that only ask ("is the clay bins thing
  done?", "did I already restock the bins?") is neither ticked nor taken off for it; a removal said only inside such
  a question ("which would you take off?") isn't asked for. A question that asks for a change is an ask, and what
  it means is the model's reading: worded as a request ("could you take the bins off?"), as a suggestion or a
  request for leave ("shouldn't we drop the bins today?", "can the bins go?"), or as an objection to the item still
  being there ("why are the bins still on?"). A question asking an opinion ("should the bins come off?") only asks
  (``asks_for_change``, ``asked_only``, ``removal_in_question``).
- **Pointing at an item's parts isn't pointing at the item.** "what are the two clays under the clay bins? get rid
  of those": the pointer is about what's under, in or listed inside the item, not the item. Nothing is taken off
  or ticked on such words; an item named outright still is (``parts_only``).
- **The user's own done.** During a turn a tick needs the user's words to say it happened, or a reply to a
  question about it (``no_done_said``); something finished by itself at its end needs the user to have asked for
  that (``auto_done_asked``).

The app's own controls (by hand) and the background (nobody talking) aren't judged here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

# --- begun or next ---
# a word that opens a stretch about what the user moves on to
_ONWARD_RE = re.compile(
    r"\b(?:on ?to|moving on|move on|now doing|now on|now|next up|up next|time for|starting|start on|"
    r"started on|getting (?:going|started) on|working on|about to|heading (?:into|in to|to|over to|off to)|"
    r"off to|diving into|digging into|turning to|switching to)\b", re.IGNORECASE)
# said after the item: it comes next
_NEXT_RE = re.compile(r"\bnext\b|\bup next\b", re.IGNORECASE)


def _clauses(message: str) -> List[str]:
    from thirties_core.started import _SENTENCE_RE, _asks
    from thirties_core.write_guard import _CLAUSE_RE
    told = " ".join(s for s in _SENTENCE_RE.findall(message or "") if not _asks(s))
    return [c for c in _CLAUSE_RE.split(told) if c and c.strip()]


def begun_or_next(message: Optional[str], title: str, others: Iterable[str] = ()) -> str:
    """Why the user's words say this item has only begun or comes next, not that it's done; "" when they don't.
    Read only where the words name the item: a pointer ("on to the next one") says nothing about which."""
    if not message:
        return ""
    from thirties_core.not_about_it import without_display_now
    from thirties_core.write_guard import _about, _done_in
    others = [o for o in others if o and o != title]
    # a "now" about what the app shows ("the time it says now") is no onward word (not_about_it.py)
    for clause in _clauses(without_display_now(re.sub(r"[‘’`]", "'", message))):
        marks = list(_ONWARD_RE.finditer(clause))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(clause)
            stretch = clause[m.start():end]
            if _about(stretch, title, others) and not _done_in(stretch, title):
                return _why(title)
        if _NEXT_RE.search(clause) and _about(clause, title, others) and not _done_in(clause, title):
            return _why(title)
    return ""


def _why(title: str) -> str:
    return (f"The user said they're starting {title}, or that it comes next, not that it's finished, so it isn't "
            "marked done. Record it with doing_now if they're on it; it's done when they say it is.")
# --- end begun or next ---


# --- a question changes nothing ---
_WH_RE = re.compile(r"^\W*(?:what|what's|whats|which|why|how|how's|where|where's|when|who|whose|wait,? why)\b",
                    re.IGNORECASE)
_ASKING_RE = re.compile(r"^\W*(?:is|isn'?t|are|aren'?t|was|wasn'?t|were|did|didn'?t|do|don'?t|does|doesn'?t|"
                        r"should|shall|has|have|hasn'?t|haven'?t|any|anything|so)\b", re.IGNORECASE)
# a request worded as a question
_POLITE_RE = re.compile(r"^\W*(?:(?:can|could|would|will|won'?t)\s+(?:you|u|ya)\b|please\b)|\b(?:please|pls|plz)\b",
                        re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")


# a question that puts a change forward: a suggestion ("why not", "how about"), an objection to the item still
# being there ("why is the varnish coat still on?"), or wondering whether it's still wanted
_PROPOSES_RE = re.compile(
    r"\b(?:why not|why don'?t (?:we|you|i)|how about|what about|what if (?:we|i|you)|mind if|"
    r"is it (?:ok|okay|fine|alright|all right) (?:if|to)|any reason (?:not to|to keep)|"
    r"do (?:we|i) (?:still |really |even )+(?:need|have|want)|(?:why|how come)\b[^.!?\n]*\bstill\b)",
    re.IGNORECASE)
# a question opened by a modal that asks for leave or puts something forward ("shouldn't we", "can't we", "can the
# paddle", "could I", "shall we"); "should the bins come off?" asks for an opinion, and only asks ...
_MODAL_RE = re.compile(r"^\W*(?:shouldn'?t|can'?t|cannot|couldn'?t|won'?t|wouldn'?t|(?:can|could|may|shall)(?=\s+(?:we|i|"
                       r"it|that|this|they|those|these|the|my|our)\b))\b", re.IGNORECASE)
# ... with a change in it: off, gone, moved, put somewhere, ticked
_CHANGE_RE = re.compile(
    r"\b(?:go|gone|off|drop\w*|remov\w*|skip\w*|cut|ditch\w*|scrap\w*|cancel\w*|nix|lose|get rid|clear\w*|"
    r"move\w*|push\w*|shift\w*|bump\w*|slide|swap\w*|put|add|fit|mark\w*|tick\w*|cross\w*|log|done|"
    r"finished|count\w*)\b", re.IGNORECASE)


def asks_for_change(sentence: str) -> bool:
    """A question that asks for a change, not one that only asks: a suggestion, an objection to the item still
    being there, or a question asking leave for a change ("shouldn't we drop the bins?", "can the bins go?"). One
    asking an opinion ("should the bins come off?") only asks. Which change it means is the model's reading."""
    s = (sentence or "").strip()
    return bool(s) and bool(_PROPOSES_RE.search(s) or (_MODAL_RE.match(s) and _CHANGE_RE.search(s)))


def is_asking(sentence: str) -> bool:
    """A sentence that only asks something, not one asking for something: a wh-question (even with "would you" in
    it), or one ending in a question mark that isn't a request ("could you take it off?") or a question that asks
    for a change (``asks_for_change``)."""
    s = sentence.strip()
    if not s or asks_for_change(s):
        return False
    if _WH_RE.match(s):
        return True
    return s.endswith("?") and (bool(_ASKING_RE.match(s)) or not _POLITE_RE.search(s))


def asked_only(message: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """The item is named in the message, and only inside questions."""
    if not message:
        return False
    from thirties_core.done_reference import _named
    others = [o for o in others if o and o != title]
    sentences = [s for s in _SENTENCE_RE.findall(message) if s.strip()]
    naming = [s for s in sentences if _named(s, title, others)]
    if not naming or not all(is_asking(s) for s in naming):
        return False
    # a statement beside the question that says it happened, pointing back ("why isn't it ticked? I did it at
    # 12:40"), is the user's done
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import _done_in
    return not any(not is_asking(s) and (_done_in(s, title) or FINISHED_RE.search(s))
                   and not any(_named(s, o, [x for x in others if x != o] + [title]) for o in others)
                   for s in sentences)


def only_questions(message: Optional[str]) -> bool:
    """Everything the message says is a question (words like "ok" aside)."""
    if not message:
        return False
    from thirties_core.turn_guards import _NEUTRAL_RE
    said = [s for s in _SENTENCE_RE.findall(message) if s.strip() and not _NEUTRAL_RE.match(s.strip())]
    return bool(said) and all(is_asking(s) for s in said)


def removal_in_question(message: Optional[str]) -> bool:
    """The words asking for something to come off are only inside questions ("which would you take off?")."""
    if not message:
        return False
    from thirties_core.change_asks import _removal_words
    words = _removal_words()
    asking = [s for s in _SENTENCE_RE.findall(message) if s.strip() and words.search(s)]
    return bool(asking) and all(is_asking(s) for s in asking)
# --- end a question changes nothing ---


# --- pointing at an item's parts ---
# a pointer that stands for something said before
_POINTER_RE = re.compile(r"\b(?:them|those|these|they|it|that|this|both|the two|those two|these two|ones)\b",
                         re.IGNORECASE)
# talking about what's under, in or listed inside an item
_PARTS_RE = re.compile(
    r"\b(?:under(?:neath)?|inside|within|beneath|below|nested|tucked|listed|contains?|containing|in (?:it|there|"
    r"them)|parts?|pieces?|lines?|steps?|sub[- ]?(?:items?|tasks?|points?)|entries|bits|bullets?|children|"
    r"what'?s (?:in|on))\b", re.IGNORECASE)


def parts_only(message: Optional[str], title: str, shown: str = "", titles: Iterable[str] = ()) -> bool:
    """True when a pointer ("get rid of those") stands for lines nested beneath this item, not for the item: every
    stretch that names it speaks of its parts. Read from this message, or, if no item is named here, from the reply
    shown just before."""
    if not message:
        return False
    from thirties_core.done_reference import _named
    from thirties_core.write_guard import _CLAUSE_RE
    titles = [t for t in titles if t]
    others = [t for t in titles if t != title]
    clauses = [c for s in _SENTENCE_RE.findall(message) for c in _CLAUSE_RE.split(s) if c and c.strip()]
    pointing = [c for c in clauses if _POINTER_RE.search(c) and not any(_named(c, t, others + [title])
                                                                         for t in [title] + others)]
    if not pointing:
        return False
    for text in (message, shown):
        stretches = [c for s in _SENTENCE_RE.findall(text or "") for c in _CLAUSE_RE.split(s) if c and c.strip()]
        naming = [c for c in stretches if _named(c, title, others)]
        if naming:
            return all(_about_parts(c, title) for c in naming)
        if any(_named(c, t, [x for x in titles if x != t]) for c in stretches for t in others):
            return False  # another item is what's being talked about
    return False


def _about_parts(text: str, title: str) -> bool:
    """The stretch talks about what's under or in the item: a parts word that isn't one of the item's own."""
    from thirties_core.item_facts import name_words
    own = name_words(title)
    return any(not (name_words(m.group(0)) & own) for m in _PARTS_RE.finditer(text))


def removal_held(pt: Any, title: str) -> str:
    """Why an item can't come off on the user's words this turn: they only asked a question, the words asking for
    it are inside a question, or they point at what's under or in it. "" when nothing here holds it."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    if only_questions(message) or removal_in_question(message):
        return (f"The user only asked about taking things off; a question changes nothing, so {title} stays. "
                "Answer them; take it off when they ask for it.")
    titles = []
    for s in getattr(getattr(pt, "day_plan", None), "spans", None) or []:
        try:
            titles.append(pt._title(s))
        except Exception:
            titles.append(s.title)
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if parts_only(message, title, shown, list(dict.fromkeys([title] + titles))):
        return parts_refusal(title)
    return ""


def parts_refusal(title: str) -> str:
    return (f"The user's words point at what's under or in {title}, not at {title} itself, so nothing is changed "
            "for it. Answer what they asked; if they want something taken off, ask which item.")
# --- end pointing at an item's parts ---


# --- the user's own done ---
# asking for a tick in so many words
_TICK_ASK_RE = re.compile(r"\b(?:mark|tick|check|cross|log)\w*\b", re.IGNORECASE)


# a stretch of time said from its start to its end ("from 1:40 to 4:10", "1:40-4:10")
_SPAN_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s*(?:to|-|–|until|till|til)\s*"
                      r"\d{1,2}(?::\d{2})?\b", re.IGNORECASE)


def no_done_said(pt: Any, title: str, others: Iterable[str], words: Optional[str], spanned: bool = False) -> str:
    """Why a done in this turn can't go ahead because nothing the user said is a done for it: no done word (the
    everyday ones count: "squared away", "it's in"), no past tense or report that it happened, no yes about it, no
    ask for a tick, and no reply to a question about whether it's done. When the words report something finished,
    the model's reading of which item stands. "" when it can."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    from thirties_core.done_words import _last_question
    from thirties_core.write_guard import says_done
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if _last_question(shown):
        return ""  # a reply to a question about which item, or whether it's done: the question carries the done
    from thirties_core.done_reference import _named
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import _MISTAKEN_RE, _POINTER_RE, REPORTED_RE, YES_RE, _done_in
    others = [o for o in others if o and o != title]
    for text in dict.fromkeys(t for t in (words, message) if t):
        told = [s for s in _SENTENCE_RE.findall(text) if s.strip() and not is_asking(s)]
        if told and (says_done(" ".join(told), title, others) or _TICK_ASK_RE.search(" ".join(told))):
            return ""
        # a done said once for several things in one sentence ("the mugs and the gunwales are done"): the sentence
        # naming it carries it (what only began, or comes next, was refused before this). A report without a done
        # word counts where it names the item or points back to it; nothing counts once it's taken back.
        for s in told:
            named = _named(s, title, others)
            about = named or not any(_named(s, o, [x for x in others if x != o] + [title]) for o in others)
            reported = bool(REPORTED_RE.search(s)) and (named or bool(_POINTER_RE.search(s)))
            if about and not _MISTAKEN_RE.search(s) and (_done_in(s, title) or FINISHED_RE.search(s) or reported
                                                         or YES_RE.match(s)):
                return ""
    if _TICK_ASK_RE.search(message) and not only_questions(message):
        return ""
    from thirties_core.work_sessions import reading
    if reading(message, title, others) == "session":
        return ""  # time on it, said: recorded as a session, never a tick (work_sessions.py)
    if spanned and _SPAN_RE.search(message):
        return ""  # when it happened, said from start to end ("the birdhouse from 1:40 to 4:10"): a report
    return (f"The user's words don't say {title} is finished, so it isn't marked done. It's done when they say it "
            "is; if it isn't clear, ask.")


def held_done(pt: Any, title: str, others: Iterable[str], words: Optional[str], event: Any = None,
              spanned: bool = False) -> str:
    """Why a done in this turn can't go ahead (log_outcome): the item is only asked about, the words point at its
    parts, or nothing said is a done for it. A done the user asked for in an earlier message isn't held by a
    question (done_words.earlier_request). "" when it can go ahead."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    from thirties_core.done_words import earlier_request
    others = [o for o in others if o and o != title]
    if earlier_request(pt, {"event": event if event not in (None, "") else title, "status": "done"}):
        return ""
    if asked_only(message, title, others):
        return (f"The user only asked about {title}; a question changes nothing, so it isn't marked done. Answer "
                "them; it's done when they say it is.")
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if parts_only(message, title, shown, [title] + others):
        return parts_refusal(title)
    return no_done_said(pt, title, others, words, spanned)


# asking for the item to be marked done by itself at its end
_AUTO_ASK_RE = re.compile(
    r"\b(?:mark|tick|check|cross|log|count|call|set)\w*\b[^.!?\n]{0,50}?\b(?:done|off|finished|complete)\b|"
    r"\b(?:done|finished) by itself\b|\bby itself\b|\bautomatically\b|\bauto[- ]?(?:done|tick|complete)\b",
    re.IGNORECASE)


def auto_done_asked(pt: Any) -> bool:
    """Whether the user's words this turn ask for an item to be marked done by itself at its end. With nobody
    talking, or the app's own controls, the caller's flag stands."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return True
    return any(_AUTO_ASK_RE.search(s) for s in _SENTENCE_RE.findall(message) if not is_asking(s))
# --- end the user's own done ---


__all__ = ["asked_only", "asks_for_change", "auto_done_asked", "begun_or_next", "is_asking", "no_done_said", "only_questions",
           "parts_only", "parts_refusal", "removal_in_question"]
