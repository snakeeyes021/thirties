"""What the user's words do for one item: a done only when said, never by pointing past it.

- **Begun or next is no done.** "the mugs are trimmed, on to the gunwales", "mugs done, gunwales up next",
  "finished the mugs. Now the gunwales": the done is the mugs'; the gunwales have only begun, or come next. A
  stretch about an item that starts with an onward word ("on to", "moving on to", "time for", "now", "starting",
  "working on", "about to", "heading into") or that says it comes next, with no done word of its own, is never a
  done for it, whatever else the message says (``begun_or_next``).
- **A plain question changes nothing.** An item named only in questions that only ask ("is the clay bins thing
  done?", "did I already restock the bins?") isn't ticked for it. A question that asks for a change is an ask, and
  what it means is the model's reading: worded as a request ("bins off the plan, please?"), as a suggestion or a
  request for leave ("shouldn't we drop the bins today?", "can the bins go?"), or as an objection to the item still
  being there ("why are the bins still on?"). A question asking an opinion ("should the bins come off?"), or one
  pushing back on a change ("why not keep the bins on today?", "wouldn't pushing the bins overload Friday?"), only
  asks (``asks_for_change``, ``asked_only``). Taking something off is always the model's reading, a question
  included: no wording here holds a removal.
- **Pointing at an item's parts isn't pointing at the item.** "what are the two clays under the clay bins? mark
  those off": the pointer is about what's under, in or listed inside the item, not the item. Nothing is ticked on
  such words; an item named outright still is (``parts_only``).
- **The user's own done.** During a turn a tick needs something the user told this turn to be a done: a done word,
  a past or a report that something happened, a yes, an ask for a tick, or a reply to the app's question about
  whether something is done, or which (``no_done_said``, ``told_done``). Which item that done is for is the
  model's reading, never Python's. Python stops a tick only of an item the words name as not done: left out
  of the done or still open (done_exceptions.py); not yet, still going or only started (write_guard.state_not_done
  and started.not_finished); told not to tick it; only asked about; its parts pointed at; or answered no to the
  app's question about it. Something finished by itself at its end needs the user to have asked for that
  (``auto_done_asked``).

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


# a change the app can make, said in a question: off, gone, moved, put somewhere, ticked. Getting something done,
# or its waiting, isn't one ("can I get the bins done by 4?", "can the bins wait till the shelves are done?")
_CHANGE = (r"(?:drop\w*|remov\w*|skip\w*|ditch\w*|scrap\w*|cancel\w*|nix|lose|get rid of|clear\w*|mov(?:e|ed|ing)|"
           r"push\w*|shift\w*|bump\w*|slide|swap\w*|postpon\w*|resched\w*|take (?:\w+ ){0,5}?off|"
           r"put (?:\w+ ){0,5}?(?:on|at|off|back|in|after|before|later|earlier)|add|mark\w*|tick\w*|log|"
           r"(?:cross|check)\w* (?:\w+ ){0,5}?off|go(?=\s*[?.!]*\s*$|\s+(?:at|to|later|earlier|after|before|"
           r"tomorrow|tonight|instead|elsewhere|somewhere)\b))")
_CHANGE_RE = re.compile(rf"\b{_CHANGE}", re.IGNORECASE)
# a question that puts a change forward: a suggestion with a change in it ("why not move it", "how about we lose
# it"), or wondering whether it's still wanted. "Why not?" alone, "what about the bins?" and a suggestion to keep it
# ("why not keep the bins on today?") only ask.
_SUGGESTS_RE = re.compile(r"\b(?:why not\s+\w|why don'?t (?:we|you|i)|(?:how|what) about (?:we|you|i|if|\w+ing)\b|"
                          r"what if (?:we|i|you))", re.IGNORECASE)
_KEEP_RE = re.compile(r"\b(?:keep\w*|leave|leaving|stay\w*|stick with|hold on to|hang on to)\b", re.IGNORECASE)
_PROPOSES_RE = re.compile(
    rf"\b(?:mind if (?:i|we) (?:just )?{_CHANGE}|is it (?:ok|okay|fine|alright|all right) (?:if|to)\b[^.!?\n]*\b"
    rf"{_CHANGE}|any reason (?:not to|to keep)|do (?:we|i) (?:still |really |even )+(?:need|want))", re.IGNORECASE)
# an objection to the item still being on the plan ("why is the varnish coat still on today?", "why is the varnish
# still on?"). "Why is the kiln still hot?" and "why are the bowls still in the kiln?" only ask.
_ON_THE_PLAN_RE = re.compile(
    r"(?:why|how come)\b[^.!?\n]*\bstill (?:(?:on|in)\s+(?:today|tonight|the (?:plan|list|schedule|calendar|day)|"
    r"my (?:plan|list|schedule|calendar|day)|today'?s|tonight'?s)\b|planned|scheduled|listed|showing|"
    r"on(?=\s*[?.!]*\s*$|\s+(?:there|here)\b)|there|here)", re.IGNORECASE)
# a question opened by a modal that asks for leave or puts something forward ("shouldn't we", "can't we", "can the
# paddle", "could I", "shall we", "wouldn't it be better to"), with a change in it; "should the bins come off?" asks
# for an opinion, and "wouldn't pushing the bins overload Friday?" worries about one: both only ask
_MODAL_RE = re.compile(r"^\W*(?:shouldn'?t|can'?t|cannot|couldn'?t|(?:won'?t|wouldn'?t) it be (?:better|easier|best|"
                       r"smarter|simpler|wiser|nicer)|(?:can|could|may|shall)(?=\s+(?:we|i|you|it|that|this|they|"
                       r"those|these|the|my|our)\b))\b", re.IGNORECASE)


def asks_for_change(sentence: str, planned: Optional[Iterable[str]] = None) -> bool:
    """A question that asks the app for a change, not one that only asks: a suggestion, an objection to the item
    still being on the plan, or a question asking leave for a change ("shouldn't we drop the bins?", "can the bins
    go?", "can we move the bins to 4?"). One asking an opinion ("should the bins come off?"), or whether something
    can get done ("can I get the bins done by 4?"), only asks. Which change it means is the model's reading.
    ``planned``: the items on today's plan; an objection to something still being on asks for a change only when it
    names one of them ("why is the oven still on?" only asks)."""
    s = (sentence or "").strip()
    if not s:
        return False
    if _PROPOSES_RE.search(s) or (_MODAL_RE.match(s) and _CHANGE_RE.search(s)):
        return True
    if _SUGGESTS_RE.search(s) and _CHANGE_RE.search(s) and not _KEEP_RE.search(s):
        return True
    if _ON_THE_PLAN_RE.search(s):
        if planned is None:
            return True
        from thirties_core.done_reference import _named
        titles = [t for t in planned if t]
        return any(_named(s, t, [x for x in titles if x != t]) for t in titles)
    return False


# a question asked outright: a question word or a modal opens it, and it ends in a question mark. A done word in it
# is asked about, never said ("shouldn't the bins be done by now?"); "did it at 4, why isn't it ticked?" opens with
# what the user did.
_OPENS_QUESTION_RE = re.compile(
    r"^\W*(?:what|what'?s|which|why|how|how'?s|where|where'?s|when|who|is|isn'?t|are|aren'?t|was|wasn'?t|were|"
    r"did(?!\s+(?:it|that|them|those|this)\b)|didn'?t|do|don'?t|does|doesn'?t|should|shouldn'?t|shall|has|hasn'?t|"
    r"have|haven'?t|can|can'?t|cannot|could|couldn'?t|may|might|will|won'?t|would|wouldn'?t)\b", re.IGNORECASE)


def asked_outright(sentence: str) -> bool:
    """The sentence is a question asked outright (``_OPENS_QUESTION_RE``): what's in it is asked, not told."""
    s = (sentence or "").strip()
    return s.endswith("?") and bool(_OPENS_QUESTION_RE.match(s))


def is_asking(sentence: str, planned: Optional[Iterable[str]] = None) -> bool:
    """A sentence that only asks something, not one asking for something: a wh-question (even with "would you" in
    it), or one ending in a question mark that isn't a request ("could you take it off?") or a question that asks
    for a change (``asks_for_change``, with today's ``planned`` items when known)."""
    s = sentence.strip()
    if not s or asks_for_change(s, planned):
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
    # 12:40"), is the user's done, and so is one the question goes on to tell ("why isn't it ticked, I did it")
    from thirties_core.started import FINISHED_RE
    from thirties_core.write_guard import _done_in
    return not any(((not is_asking(s) and (_done_in(s, title) or FINISHED_RE.search(s))) or told_done(s, title))
                   and not any(_named(s, o, [x for x in others if x != o] + [title]) for o in others)
                   for s in sentences)


def only_questions(message: Optional[str], planned: Optional[Iterable[str]] = None) -> bool:
    """Everything the message says is a question (words like "ok" aside)."""
    if not message:
        return False
    from thirties_core.turn_guards import _NEUTRAL_RE
    planned = None if planned is None else list(planned)
    said = [s for s in _SENTENCE_RE.findall(message) if s.strip() and not _NEUTRAL_RE.match(s.strip())]
    return bool(said) and all(is_asking(s, planned) for s in said)


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


def parts_refusal(title: str) -> str:
    return (f"The user's words point at what's under or in {title}, not at {title} itself, so nothing is changed "
            "for it. Answer what they asked; if they want something taken off, ask which item.")
# --- end pointing at an item's parts ---


# --- the user's own done ---
# asking for a tick in so many words: "tick the varnish coat", "mark the glaze bins off", "check off the lid
# wiring", "cross the bins off", "put the bins down as done". "Check the wiring" and "mark essays" are the item's
# own verb, and "check if the bins are done" asks something.
_BETWEEN = r"(?:\s+(?!(?:if|whether|that|is|are|was|were|has|have|had|get|gets|got)\b)[\w']+){0,6}?"
_TICK_ASK_RE = re.compile(
    rf"\btick(?:s|ing)?\b(?!\s+(?:box|tock|bite)\b)|"
    rf"\b(?:mark|check|cross)(?:s|ing)?\b{_BETWEEN}\s+(?:off|done|complete|completed|finished)\b|"
    rf"\b(?:put|set|mark|log|record|count|list|enter|note)\b{_BETWEEN}\s+(?:down\s+)?(?:as|to)\s+"
    r"(?:done|finished|complete|completed)\b", re.IGNORECASE)
_DOWN_AS_RE = re.compile(r"\bdown\s+(?=(?:as|to)\b)", re.IGNORECASE)


def without_ask_words(text: str) -> str:
    """The text with the "down" of a tick ask taken out ("put the bins down as done" reads "put the bins as done"):
    it belongs to the ask, not to the item's name. "The down jacket" keeps its word."""
    for m in _TICK_ASK_RE.finditer(text or ""):
        text = text.replace(m.group(0), _DOWN_AS_RE.sub("", m.group(0)))
    return text


# the tick is for later, or for the user to remember: "remind me to mark the bins off", "I'll tick the varnish at 8:40"
_LATER_TICK_RE = re.compile(r"\b(?:remind(?:er)?|don'?t let me forget|i'?ll|i will|we'?ll|going to|gonna)\b",
                            re.IGNORECASE)


def tick_asked(message: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """The user asks for this item to be ticked: a sentence asking for a tick ("mark the varnish off", "can we
    tick the varnish?", "could you put the varnish down as done?"), not one that only asks, that names the item,
    or points back naming nothing else ("mark it done"). A tick put off ("could we skip ticking it?", "can we not
    mark it yet?") or left for later ("remind me to mark the bins off") isn't asked for, and neither is the item's own
    verb ("check the kiln wiring" for Check kiln wiring). The plan and the list read it alike
    (write_guard.says_done)."""
    if not message:
        return False
    from thirties_core.write_guard import NEGATED_DONE_RE, PUT_OFF_TICK_RE, _about, _names_another_thing
    others = [o for o in others if o and o != title]
    own = set(re.findall(r"[a-z]+", str(title or "").lower()))
    for s in _SENTENCE_RE.findall(message):
        if not s.strip() or is_asking(s):
            continue
        asks = [m for m in _TICK_ASK_RE.finditer(s)
                if not (m.group(0).lower().startswith("tick") and "tick" in own)
                and not _LATER_TICK_RE.search(s[:m.start()])]
        if not asks:
            continue
        # "why don't we tick it?" puts the tick forward; "don't tick it" and "can we hold off on ticking?" don't
        if NEGATED_DONE_RE.search(re.sub(r"\bwhy don'?t\b", "why", s, flags=re.IGNORECASE)) \
                or PUT_OFF_TICK_RE.search(s):
            continue
        s = without_ask_words(s)
        if any(_about(s, o, [x for x in others if x != o] + [title]) for o in others) and not _about(s, title, others):
            continue  # a tick asked for another item
        if not _names_another_thing(s, title, others):
            return True
    return False


# a stretch of time said from its start to its end ("from 1:40 to 4:10", "1:40-4:10")
_SPAN_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\s*(?:to|-|–|until|till|til)\s*"
                      r"\d{1,2}(?::\d{2})?\b", re.IGNORECASE)


def asks_for_a_tick(message: Optional[str]) -> bool:
    """A told sentence asks for a tick ("mark it off", "can we tick those?", "put them down as done"): not one put
    off ("don't tick it yet", "can we hold off on ticking?") or left for later ("remind me to mark it off"). Which
    item it's for is the model's reading."""
    if not message:
        return False
    from thirties_core.write_guard import NEGATED_DONE_RE, PUT_OFF_TICK_RE
    for s in _SENTENCE_RE.findall(message):
        if not s.strip() or is_asking(s):
            continue
        if not any(not _LATER_TICK_RE.search(s[:m.start()]) for m in _TICK_ASK_RE.finditer(s)):
            continue
        if NEGATED_DONE_RE.search(re.sub(r"\bwhy don'?t\b", "why", s, flags=re.IGNORECASE)) \
                or PUT_OFF_TICK_RE.search(s):
            continue
        return True
    return False


# something that happened to the user, not a thing they did ("knocked out by a headache", "held up by the courier")
_DONE_TO_THEM_RE = re.compile(r"\b[a-z]{3,}ed\s+(?:out\s+|up\s+|over\s+)?by\b", re.IGNORECASE)
# a word that leaves something out of a denied done: what it leaves out is done ("did none save for the log")
_EXCEPT_RE = re.compile(r"\b(?:except|apart from|aside from|other than|but|besides|save for)\b", re.IGNORECASE)


def told_done(message: Optional[str], title: str = "", yes: bool = False) -> bool:
    """Whether anything the user's words tell this turn is a done: a done word (the everyday ones count: "squared
    away", "wrapped it up"), a past or a report that something happened, or an ask for a tick; with ``yes``, a
    plain yes too. A question only asks, so a done word in it says nothing, even in one asking for a change ("can I
    get the bins done by 4?"); a sentence that opens as a question and goes on to tell ("why isn't it ticked, I
    did it at 2:48") tells in the clauses after it. Nor do a done denied in its clause ("the glaze test isn't done")
    say one, while the app's own status said beside a done ("not ticked") denies nothing; nor what's only getting
    done ("getting the fence fixed"), what happened to the user ("knocked out by a headache"), what should or
    could have happened, or what's taken back ("or so I thought"). Which item the done is for is the model's
    reading: ``title`` only lets the item's own verb in the past ("lodged" for Lodge the permit) and a hand-in ("the
    form's in") count too."""
    if not message:
        return False
    from thirties_core.done_exceptions import _DENIED_RE
    from thirties_core.plain_done import says_it_happened
    from thirties_core.started import _without_record_words, without_caused
    from thirties_core.write_guard import _CLAUSE_RE, _MISTAKEN_RE, _UNREAL_RE, REPORTED_RE, YES_RE, _done_in
    for s in _SENTENCE_RE.findall(without_caused(_without_record_words(message))):
        if not s.strip() or _MISTAKEN_RE.search(s):
            continue
        clauses = [c for c in _CLAUSE_RE.split(_DONE_TO_THEM_RE.sub(" ", s)) if c and c.strip()]
        if is_asking(s) or asked_outright(s) or asks_for_change(s):
            clauses = [c for c in clauses[1:] if not _opens_a_question(c)]  # what it goes on to tell
        elif yes and YES_RE.match(s):
            return True
        for c in clauses:
            if _UNREAL_RE.search(c) or (_DENIED_RE.search(c) and not _EXCEPT_RE.search(c)):
                continue
            if says_it_happened(c) or REPORTED_RE.search(c) or (title and _done_in(c, title)):
                return True
    return asks_for_a_tick(message)


# a clause that opens a question of its own; "when" and "where" open a clause about the done as often ("when I
# finished it")
_CLAUSE_ASKS_RE = re.compile(
    r"^(?:what|what'?s|which|why|how come|how|who|whose|is|isn'?t|are|aren'?t|was|wasn'?t|were|"
    r"did(?!\s+(?:it|that|them|those|this)\b)|didn'?t|do|don'?t|does|doesn'?t|has|hasn'?t|have|haven'?t|can|can'?t|"
    r"could|couldn'?t|will|won'?t|would|wouldn'?t|should|shouldn'?t|shall)\b", re.IGNORECASE)


def _opens_a_question(clause: str) -> bool:
    """The clause opens a question of its own ("or did I finish it", "what about the log")."""
    c = re.sub(r"^\W*(?:and|but|or|so|then|while)\b", "", clause or "", flags=re.IGNORECASE).strip()
    return bool(_CLAUSE_ASKS_RE.match(c))


_TICK_WORD_RE = re.compile(r"\b(?:tick|mark|check|cross)\w*\b", re.IGNORECASE)
# every one of them: an answer to which
_ALL_RE = re.compile(r"\b(?:all|everything|every\s+one|each|the\s+(?:whole\s+)?lot|whole)\b", re.IGNORECASE)


def done_question(shown: Optional[str]) -> str:
    """The last question in the message the user was shown, when it asks whether something is done, which item, or
    whether to tick it ("Did you finish the glaze test?", "Which one?", "Shall I tick it off?"); "" otherwise."""
    from thirties_core.done_words import _last_question
    asked = _last_question(shown or "")
    if asked:
        return asked
    last = re.findall(r"[^.!?\n]+\?", shown or "")
    return last[-1] if last and _TICK_WORD_RE.search(last[-1]) else ""


def unanswered_which(pt: Any, title: str) -> str:
    """Why no tick goes ahead when the last reply asked which of several open items the user finished and their
    words name none of them, point at none by place or count, and only say it's done ("done!", "that one's
    sorted"): nothing says which. "" otherwise."""
    message = getattr(pt, "user_message", None)
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if getattr(pt, "by_hand", False) or not message or "?" not in (shown or ""):
        return ""
    from thirties_core.done_counts import pointed_count
    from thirties_core.done_reference import (BOTH_RE, BULK_RE, ORDINAL_RE, PLURAL_RE, _listed_alone,
                                              _open_items_shown, _titles)
    from thirties_core.done_words import _hits
    from thirties_core.plain_done import _open_titles, _which_words, says_it_happened
    if not says_it_happened(message) or ORDINAL_RE.search(message) or BOTH_RE.search(message) \
            or PLURAL_RE.search(message) or BULK_RE.search(message) or _ALL_RE.search(message) \
            or pointed_count(message):
        return ""
    titles = list(dict.fromkeys(_titles(pt) + [title]))
    shown_open = _open_items_shown(pt, shown, titles)
    listed = [t for t in dict.fromkeys(_open_titles(pt) + shown_open) if _listed_alone(shown, t)]
    which = _which_words(message)
    if len(listed) < 2 or any(_hits(which, t) for t in titles + shown_open):
        return ""
    from thirties_core.done_words import asked_about
    asked = done_question(shown)
    if asked and len(asked_about(asked, titles + shown_open, titles)) == 1:
        return ""  # the question was about one item: the reply answers it, whatever else the reply listed
    return (f"The user's words don't say which item they finished, and the last reply asked them, so {title} isn't "
            "ticked. Ask again which one, in one short question.")


# a question asking which item, not whether ("Which one?", "what did you finish?")
_WHICH_ASKED_RE = re.compile(r"\b(?:which|what)\b", re.IGNORECASE)


def _asks_which(pt: Any, asked: str) -> bool:
    """The question asks which item: a which or what, or a choice between two or more items it names. A question
    naming one item asks whether."""
    if _WHICH_ASKED_RE.search(asked):
        return True
    if not re.search(r"\bor\b", asked, re.IGNORECASE):
        return False
    from thirties_core.done_reference import _open_items_shown, _titles
    from thirties_core.done_words import asked_about
    titles = _titles(pt)
    return len(asked_about(asked, titles + _open_items_shown(pt, asked, titles), titles)) >= 2


def _reply_carries_done(pt: Any, message: str) -> bool:
    """The words reply to the app's question about a done (``done_question``), and the question carries the done:
    any reply but a no to whether something is done ("yep", "go ahead"); to which item, one that says which (names
    an item, picks one by its place, or takes in several: "the first one", "both", "all of them"). A no that names
    the item it means instead ("no, the kiln log") answers which."""
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    asked = done_question(shown)
    if not asked:
        return False
    from thirties_core.done_words import NO_RE
    named = _names_an_item(pt, message)
    if NO_RE.match(message) and not named:
        return False
    if not _asks_which(pt, asked):
        return True
    from thirties_core.done_counts import pointed_count
    from thirties_core.done_reference import BOTH_RE, BULK_RE, ORDINAL_RE, PLURAL_RE
    return named or any(r.search(message) for r in (ORDINAL_RE, BOTH_RE, PLURAL_RE, BULK_RE, _ALL_RE)) \
        or bool(pointed_count(message))


# an item said not to happen, besides the skip and removal words: missed, out of time, too tired
_SKIPPED_RE = re.compile(r"\b(?:miss(?:ed|ing)|ran out of time|run out of time|out of time|no time|too tired|"
                         r"too wiped|bail(?:ed|ing)?|give (?:it|that|them) a miss|pass(?:ed|ing)? on)\b", re.IGNORECASE)


def told_skip(message: Optional[str]) -> bool:
    """Whether anything the user's words tell this turn is a skip: a skip or removal word ("skipping", "not doing",
    "forget it"), something not happening or not done ("didn't get to", "never happened"), put off ("can wait",
    "later", "tomorrow": done_exceptions.py), or missed or out of time. Which items it is for is
    the model's reading. A question only asks."""
    if not message:
        return False
    from thirties_core.change_asks import _removal_words
    from thirties_core.done_exceptions import _OPEN_RE
    from thirties_core.started import NOT_YET_RE
    words = _removal_words()
    return any(words.search(s) or NOT_YET_RE.search(s) or _OPEN_RE.search(s) or _SKIPPED_RE.search(s)
               for s in _SENTENCE_RE.findall(message) if s.strip() and not is_asking(s))


def _names_an_item(pt: Any, message: str) -> bool:
    """True when the message names a title on the plan, or an open list entry shown in the last reply."""
    from thirties_core.done_reference import _named, _open_items_shown, _titles
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    titles = _titles(pt)
    pool = list(dict.fromkeys(titles + _open_items_shown(pt, shown, titles)))
    return any(_named(message, t, [x for x in pool if x != t]) for t in pool)


def no_done_said(pt: Any, title: str, others: Iterable[str], words: Optional[str], spanned: bool = False) -> str:
    """Why a done in this turn can't go ahead: nothing the user said this turn is a done (``told_done``), and their
    words aren't a reply to the app's question about whether something is done, or which. A no to such a question
    carries no done, unless it names the item it means instead ("no, the kiln log"). Which item a done is for is
    the model's reading: when anything they told is a done, the model's choice stands. "" when it can go ahead."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    if _reply_carries_done(pt, message):
        return ""  # a reply to a question about which item, or whether it's done: the question carries the done
    if any(told_done(text, title) for text in dict.fromkeys(t for t in (words, message) if t)):
        return ""
    others = [o for o in others if o and o != title]
    from thirties_core.work_sessions import reading
    if reading(message, title, others) == "session":
        return ""  # time on it, said: recorded as a session, never a tick (work_sessions.py)
    if spanned and _SPAN_RE.search(message):
        return ""  # when it happened, said from start to end ("the birdhouse from 1:40 to 4:10"): a report
    return (f"Nothing the user said this turn is a done, so {title} isn't marked done. It's done when they say it "
            "is; if it isn't clear, ask.")


def said_no(pt: Any, title: str) -> str:
    """Why a done can't go ahead when the user said no to the app's question about this item ("Is the kiln log
    filled in?" "no"), without naming it again: "" otherwise."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or not message:
        return ""
    from thirties_core.done_words import NO_RE, asked_about
    _, shown = getattr(pt, "prior_exchange", None) or ("", "")
    if not shown or not NO_RE.match(message):
        return ""
    from thirties_core.done_reference import _named, _titles
    titles = list(dict.fromkeys(_titles(pt) + [title]))
    if title not in asked_about(shown, titles, titles) or _named(message, title, [t for t in titles if t != title]):
        return ""
    return (f"The user said no to the question about {title}, so it isn't marked done. It's done when they say it "
            "is.")


def held_done(pt: Any, title: str, others: Iterable[str], words: Optional[str], event: Any = None,
              spanned: bool = False) -> str:
    """Why a done in this turn can't go ahead (log_outcome): the item is only asked about, the words point at its
    parts, a reply to the app's question leaves it out ("both, except the bailers"), the user said no to the app's
    question about it, the app asked which and the words don't say (``unanswered_which``), or nothing said is a
    done. A done the user asked for in an earlier message isn't held by a question (done_words.earlier_request). ""
    when it can go ahead."""
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
    if done_question(shown):  # a reply carries the question's done: what it leaves out isn't done (done_exceptions.py)
        from thirties_core.done_exceptions import left_out
        left = left_out(message, title, others, answered=True)
        if left:
            return left
    return said_no(pt, title) or unanswered_which(pt, title) or no_done_said(pt, title, others, words, spanned)


def done_refused(pt: Any, title: str, others: Optional[Iterable[str]] = None, event: Any = None) -> str:
    """Why a done of this item in this turn stops, for the done paths besides log_outcome (which reads the same
    things one by one): the user's words name it as not done (started.not_finished, write_guard.state_not_done),
    or ``held_done`` holds it. "" when it can go ahead, and when nobody is talking."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return ""
    if others is None:
        from thirties_core.done_reference import _titles
        others = _titles(pt)
    others = [o for o in others if o and o != title]
    from thirties_core.started import not_finished
    from thirties_core.write_guard import state_not_done
    said = not_finished(message, title, others)
    if said == "begun":
        return (f"The user said {title} has started, not that it's finished, so it isn't done yet. Mark it done "
                "when they say it's over.")
    if said == "not":
        return f"The user said {title} hasn't happened, so it isn't done. Leave it as it is."
    return state_not_done(message, title, others) or held_done(pt, title, others, message, event)


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


__all__ = ["asked_only", "asks_for_a_tick", "asks_for_change", "auto_done_asked", "begun_or_next", "done_question",
           "done_refused", "held_done", "is_asking", "no_done_said", "only_questions", "parts_only", "parts_refusal",
           "said_no", "told_done", "told_skip", "unanswered_which"]
