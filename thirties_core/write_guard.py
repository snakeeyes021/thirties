"""The last check before Joplin is written during a conversation: the user's own words have to say it.

Two kinds of change can't be undone by just taking the plan back, so the user's message for the turn has to ask
for them in plain words:

- **Filed as dropped, or gone from its own list.** Filing an item in the month's Dropped note, or removing its line
  from one of the user's lists (any note but Today), needs a drop word: "drop it", "I'm not doing it", "forget it",
  "cross it off for good", "delete it", "never going to". Taking something off today ("take that off", "skip it",
  "not today") only changes today's plan and the Today note; the item stays to do on its list.
- **Ticked done.** A tick needs the user saying it happened: a done word ("done", "finished", "paid", "sent",
  "all set", and everyday ones like "squared away", "knocked it out", "the permit is in"), the item's own verb in
  the past ("renewed" for Renew the kiln permit), a report that it happened ("I've written them up"), or a plain
  yes to a question.
  Words that say something is under way or merely all right ("the kiln is firing", "it's running", "the glaze is
  fine") are never a done, for the item they're said about, however loosely the words point to it; a done word in
  the same sentence wins over merely all right ("the bisque unload went fine, all wrapped"). Neither are
  words telling the app not to tick it ("leave the bowls unticked", "don't tick it", "nowhere near done"), nor
  words naming something else that shares a word with it ("the glaze order is sent" for Glaze recipe test:
  done_fit.py).

Changes made outside a turn (the app's own controls, the background, this morning's housekeeping) aren't judged
here: they have their own rules.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# the user asking for an item to be let go for good
DROP_WORDS_RE = re.compile(
    r"\bdrop(?:s|ped|ping)?\b(?!\s+(?:off|by|in|round|over)\b)|"
    r"\b(?:i'?m|i am|we'?re|we are)\s+not\s+(?:doing|gonna do|going to do)\s+(?:it|that|this|them|those|the\b[^.!?\n]{0,30})"
    r"(?![^.!?\n]{0,20}\b(?:today|tonight|now|yet|this (?:morning|afternoon|evening)))|"
    r"\bforget (?:about )?(?:it|that|this|them|those)\b(?![^.!?\n]{0,12}\b(?:for )?(?:today|tonight|now)\b)|"
    r"\b(?:for good|for ever|forever|permanently|altogether|never (?:going to |gonna )?(?:do|doing)\b)\b|"
    r"\b(?:delete|archive|scrap|ditch|get rid of|let (?:it|that|them) go|no longer (?:need|want|doing))\b",
    re.IGNORECASE)

# done in so many words
DONE_WORDS_RE = re.compile(
    r"\b(?:done|finished|finish(?:ed)? (?:it|that|them)|complete[ds]?|paid|sent|submitted|mailed|posted|returned|"
    r"bought|handed in|dropped off|picked up|signed|renewed|booked|fixed|repaired|cleaned|ticked|checked off|"
    r"crossed off|all set|taken care of|took care of|sorted|wrapped up|over with|handled|did (?:it|that|them)|"
    r"got (?:it|that|them) done|"
    # the everyday ways of saying it's finished
    r"one less thing|knocked (?:it |that |them |those |\w+ )?out|squared away|dealt with|sorted out|"
    r"(?:all|it'?s|that'?s|we'?re|i'?m) wrapped|that'?s a wrap|(?:it'?s|that'?s|is) in the bag|"
    r"nailed (?:it|that|them)|(?:it'?s|that'?s|they'?re|is|are|now) out of the way|off my (?:plate|list|back)|"
    r"(?:crossed|ticked) (?:it|that|them|those) off|put to bed|finished up|over and done)\b|"
    # "the kiln permit is in", said as the end of what's said about it
    r"\b(?:it'?s|its|that'?s|they'?re|is|are) in\s*[.!]*\s*$|[\u2713\u2714\u2611\u2705]", re.IGNORECASE)
# a report that something happened, in the perfect ("I've written the glaze notes", "the parcel has gone") or an
# everyday irregular past: the model's reading of a done in words like these stands. Not said as could, would or
# will have; "I've started", "it's left" and "I've planned" aren't reports of a finish.
_PARTICIPLES = ("written|sent|gone|given|done|seen|spoken|bought|brought|built|sold|told|won|swept|hung|wound|driven|"
                "thrown|taken|made|eaten|drunk|paid|laid|fed|found|held|kept|lent|met|ridden|shaken|sewn|sown|sung|"
                "spun|stuck|struck|swum|torn|woven|worn|dug|ground|bound|fought|taught|caught|sought|stood|swung")
_NOT_REPORTED = ("started", "scheduled", "planned", "needed", "wanted", "supposed", "tired", "bored", "kicked", "turned",
                 "asked", "wondered", "guessed", "worried", "listed", "added", "moved", "pushed", "postponed", "decided",
                 "meant", "hoped", "tried", "used", "delayed")
REPORTED_RE = re.compile(
    r"(?<!would )(?<!could )(?<!should )(?<!might )(?<!must )(?<!will )(?<!'ll )(?<!'d )"
    r"\b(?:'ve|'s|have|has|had)\s+(?:just\s+|already\s+|finally\s+|now\s+|all\s+)?(?:been\s+)?"
    r"(?:" + _PARTICIPLES + r"|(?!(?:" + "|".join(_NOT_REPORTED) + r")\b)[a-z]{3,}ed)\b|"
    r"\b(?:wrote|spoke|built|sold|drove|gave|threw|swept|dug|wove|sewed|fought|taught|caught|swung|hung|wound|"
    r"bound|ground|tore|wore|rode|shook|sang|swam|spun|struck|stuck|bought|brought|ate|drank|lent|met|won)\b",
    re.IGNORECASE)
# a plain yes, answering a question the user was asked
YES_RE = re.compile(r"^\W*(?:yes|yep|yeah|yup|yea|correct|right|exactly|that one|that'?s (?:it|the one)|sure)\b",
                    re.IGNORECASE)
# under way, or merely all right: never a done
STATE_RE = re.compile(
    r"\b(?:running|going|firing|started|starting|underway|under way|in progress|on its way|heating|warming|"
    r"cooling|soaking|drying|loading|working|fine|ok|okay|alright|all right|good)\b|"
    r"\bon\s*$", re.IGNORECASE)  # "the kiln is on", said at the end; "worked on the railing" is something else
_NOT_DONE_RE = re.compile(r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|isn'?t|wasn'?t|yet to)\b|n't\b", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[.;!?\n,]+|\b(?:and|but|so|then|while)\b", re.IGNORECASE)
# past tenses that aren't the thing happening
_NOT_PAST = {"started", "scheduled", "planned", "needed", "wanted", "supposed", "tired", "bored", "kicked", "turned",
             "asked", "wondered", "guessed", "worried", "listed", "added", "moved", "pushed", "postponed"}
_IRREGULAR = {"buy": "bought", "make": "made", "write": "wrote", "take": "took", "get": "got", "do": "did",
              "go": "went", "send": "sent", "pay": "paid", "bring": "brought", "throw": "threw", "find": "found",
              "sell": "sold", "tell": "told", "sweep": "swept", "build": "built", "lend": "lent", "spend": "spent", "hang": "hung", "wind": "wound", "drive": "drove", "give": "gave", "ring": "rang", "throw": "threw", "pick": "picked", "put": "put", "set": "set", "run": "ran"}


def _said(turn: str) -> str:
    """The user's words from the turn's record ("[app] words / more words")."""
    return re.sub(r"^\[[^\]]*\]\s*", "", str(turn or "")).strip()


def asks_to_drop(message: Optional[str]) -> bool:
    """The user asks for something to be let go for good, not just taken off today."""
    return bool(message and DROP_WORDS_RE.search(message))


def _past_of_title(message: str, title: str) -> bool:
    """A word of the message is one of the item's own words in the past ("renewed" for Renew the kiln permit)."""
    from thirties_core.done_words import kin
    from thirties_core.item_facts import _stem
    words = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", title.lower()))
    said = re.findall(r"[a-z]+", re.sub(r"[‘’'`]", "", message.lower()))
    for w in words:
        if _IRREGULAR.get(w) in said:
            return True
        if len(w) > 2 and any(p in said and p not in _NOT_PAST for p in _regular_past(w)):
            return True  # "lodged" for Lodge the permit, "filed" for File the forms
    stems = {_stem(w) for w in words if len(w) > 2}
    for s in said:
        if len(s) >= 5 and s.endswith("ed") and s not in _NOT_PAST:
            if any(kin(t, (_stem(s), s)) for t in stems):
                return True
    return False


def _regular_past(word: str) -> set:
    """The past of an everyday verb, said the regular way ("lodge" → "lodged", "file" → "filed", "tidy" →
    "tidied", "plan" → "planned")."""
    out = {word + "ed"}
    if word.endswith("e"):
        out.add(word + "d")
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        out.add(word[:-1] + "ied")
    if len(word) <= 4 and word[-1] not in "aeiouwxy" and word[-2:-1] in "aeiou":
        out.add(word + word[-1] + "ed")
    return out


def _about(clause: str, title: str, others: list) -> bool:
    """The clause names the item: as started reads it, or loosely (done_words.named). Loose words only point."""
    from thirties_core.done_words import named
    from thirties_core.started import _names
    return _names(clause, title, others) or named(clause, title, others)


# --- a word another item shares, and an acknowledgement, in a done ---
# a clause that only acknowledges ("ok", "okay so", "alright then"): a way into the message, never a word about an item
_ACK_RE = re.compile(r"^\W*(?:ok|okay|k|alright|all right)(?:\W+(?:so|then|cool|great|lol|haha|thanks|thx))*\W*$",
                     re.IGNORECASE)


def _fits_level(clause: str, title: str, others: list) -> bool:
    """The clause names the item by words another item shares just as much ("glaze run" beside Glaze run and Glaze
    run review call): it's about this item as much as the other, so what it says counts for this one."""
    from thirties_core.item_facts import tied_with
    return bool(tied_with(clause, title, others))
# --- end a word another item shares, and an acknowledgement ---


# --- told not to tick it: no done, whatever done word is near the item's name ---
# Asked to leave it unticked or open, not to tick or mark it, to wait with the tick, or to untick it; or told it's far
# from done. The app's own status described in a complaint (still not ticked, though it happened) is no such thing.
NEGATED_DONE_RE = re.compile(
    r"\b(?:leave|keep|let)\b[^.!?\n]{0,40}?\b(?:un(?:ticked|checked|marked|done|finished)|"
    r"not (?:yet )?(?:ticked|checked|marked|done|finished)|open|as (?:it is|is)|alone|for (?:now|later))\b|"
    r"\b(?:stays?|remains?|staying)\s+(?:un(?:ticked|checked|marked)|open)\b|"
    r"\bun(?:tick|check|mark)(?:s|ing)?\b|"
    r"\b(?:don'?t|dont|do not|never|no need to|not to|hold off(?: on)?|wait (?:on|before|to))\s+"
    r"(?!forget\b|hesitate\b)(?:\w+\s+){0,2}?(?:tick|mark|check|cross)(?:ing)?\b|"
    r"\bno (?:tick|check ?mark)\b|"
    r"\b(?:nowhere near|far from|anything but|not quite|not even close to|not close to|not yet)\s+(?:being\s+)?"
    r"(?:done|finished|complete|completed|ready|over)\b|"
    # a plain "isn't done" / "not finished": said of the item, never a done for it
    r"\b(?:isn'?t|is not|aren'?t|are not|wasn'?t|was not|weren'?t|were not|hasn'?t been|haven'?t been|not)\s+"
    r"(?:yet\s+|really\s+|actually\s+|even\s+)?(?:done|finished|complete|completed|over)\b", re.IGNORECASE)
_POINTER_RE = re.compile(r"\b(?:it|them|that|this|those|these|both|they|theyre|they'?re)\b", re.IGNORECASE)


def told_not_to_tick(message: Optional[str], title: str, others=()) -> str:
    """Why the user's words say not to tick this item ("leave the bowls unticked", "don't tick it", "nowhere near
    done"), said about it: in a clause naming it, in a clause pointing back with "it" or "them" that names no other
    item, or, when no clause names it, in any clause naming no other item. "" when nothing says so."""
    if not message:
        return ""
    from thirties_core.started import _SENTENCE_RE, _asks
    others = [o for o in others if o and o != title]
    told = " ".join(t for t in _SENTENCE_RE.findall(message) if not _asks(t))
    clauses = [c for c in _CLAUSE_RE.split(told) if c and c.strip()]
    if not any(NEGATED_DONE_RE.search(c) for c in clauses):
        return ""
    named = [c for c in clauses if _about(c, title, others)]
    free = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)
            and c not in named]
    about = named + ([c for c in free if _POINTER_RE.search(c)] if named else free)
    if any(NEGATED_DONE_RE.search(c) for c in about):
        return (f"The user said not to tick {title}, so it isn't marked done. Leave it open; it's done when they "
                "say it is.")
    return ""
# --- end told not to tick it ---


def _state_in(clause: str, title: str) -> bool:
    """The clause says something is under way or all right, in a word that isn't one of the item's own ("firing"
    in Lodge kiln firing permit names it)."""
    own = set(re.findall(r"[a-z]+", str(title or "").lower()))
    return any(not (set(re.findall(r"[a-z]+", m.group(0).lower())) & own) for m in STATE_RE.finditer(clause))


# merely all right: how it went, which a done beside it settles ("it went fine, all wrapped")
_ALL_RIGHT_RE = re.compile(r"^(?:fine|ok|okay|alright|all right|good)$", re.IGNORECASE)


def _under_way_in(clause: str, title: str) -> bool:
    """The clause says something is under way ("firing", "running", "going"), in a word that isn't the item's own."""
    own = set(re.findall(r"[a-z]+", str(title or "").lower()))
    return any(not _ALL_RIGHT_RE.match(m.group(0).strip()) and not (set(re.findall(r"[a-z]+", m.group(0).lower())) & own)
               for m in STATE_RE.finditer(clause))


def _done_beside(clause: str, sentences: list, title: str, others: list) -> bool:
    """A done word in the same sentence as this clause, in a stretch about this item or naming no other one."""
    for sentence in sentences:
        if clause.strip() not in sentence:
            continue
        for part in _CLAUSE_RE.split(sentence):
            if part and part.strip() and _done_in(part, title) and not any(
                    _about(part, o, [x for x in others if x != o] + [title]) for o in others):
                return True
    return False


def _done_in(clause: str, title: str) -> bool:
    return bool(DONE_WORDS_RE.search(clause) or _past_of_title(clause, title))


def state_not_done(message: Optional[str], title: str, others=()) -> str:
    """Why the user's words say this item isn't finished: what they say about it is that it's under way, merely all
    right, or not done ("the kiln is firing", "the glaze is fine", "kiln's on"). When no clause names it (a reply
    like "it's running" to a question about it), the clauses that name no other item are about it. "" when no
    such thing is said. However loosely the words point to the item, they never make a done of these."""
    if not message:
        return ""
    told_not = told_not_to_tick(message, title, others)
    if told_not:
        return told_not
    # --- a done said ahead of time: promised, hoped for, or once something happens (done_ahead.py) ---
    from thirties_core.done_ahead import said_ahead
    ahead = said_ahead(message, title, others)
    if ahead:
        return ahead
    # --- end said ahead ---
    # --- begun, or coming next, beside a done for something else: no done for it (said_for_it.py) ---
    from thirties_core.said_for_it import begun_or_next
    onward = begun_or_next(message, title, others)
    if onward:
        return onward
    # --- end begun or next ---
    from thirties_core.started import _SENTENCE_RE, _asks, _without_record_words
    others = [o for o in others if o and o != title]
    # a question, or the app's own status quoted ("why does it still say 'not marked'?"), says nothing about it
    told_sentences = [t for t in _SENTENCE_RE.findall(_without_record_words(message)) if not _asks(t)]
    told = " ".join(told_sentences)
    clauses = [c for c in _CLAUSE_RE.split(told) if c and c.strip() and not _ACK_RE.match(c)]
    # --- an item named only as when something else stops is no done; what a clause says is about its head
    # ("working until the kiln check": the working) (not_about_it.py) ---
    from thirties_core.not_about_it import end_refusal, head, only_as_an_end
    if only_as_an_end(clauses, title, others):
        return end_refusal(title)
    about = [c for c in clauses if _about(head(c), title, others) or _fits_level(head(c), title, others)]
    # --- end when something else stops ---
    if not about:
        about = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)]
        # --- talk to the app beside a done ("quit checking on that one") says nothing about it (plain_done.py) ---
        from thirties_core.plain_done import without_talk_to_the_app
        about = without_talk_to_the_app(about, told)
        # --- end talk to the app ---
    for c in about:
        if not _done_in(c, title) and (_state_in(c, title) or _NOT_DONE_RE.search(c)):
            # --- merely all right, with a done in the same sentence: the done wins ("it went fine, all wrapped") ---
            if not _NOT_DONE_RE.search(c) and not _under_way_in(c, title) \
                    and _done_beside(c, told_sentences, title, others):
                continue
            # --- end the done wins ---
            return (f"The user said {title} is going or all right, not that it's finished, so it isn't marked done. "
                    "Leave it open; it's done when they say it is.")
    return ""


def says_done(message: Optional[str], title: str, others=(), strict: bool = False) -> bool:
    """Whether the user's words say this item happened: a done word (everyday ones too: "squared away", "that's one
    less thing") or its verb in the past, a report that it happened ("I've written them up"), said about it (or in a
    clause naming no other item), or a plain yes; and nothing about it says it's only under way, merely all right,
    or not done. ``strict``: only done words and the item's own verb count (any past tense about the item counts
    otherwise), and a question says nothing ("how come the rent got moved?")."""
    if not message:
        return False
    from thirties_core.started import FINISHED_RE, _SENTENCE_RE, _asks, not_finished
    others = [o for o in others if o and o != title]
    if not_finished(message, title, others) in ("begun", "not") or state_not_done(message, title, others):
        return False
    if strict:
        message = " ".join(t for t in _SENTENCE_RE.findall(message) if not _asks(t))
    clauses = [c for c in _CLAUSE_RE.split(message) if c and c.strip()]
    if any(_about(c, title, others) and (_done_in(c, title) or (not strict and (FINISHED_RE.search(c)
                                                                                or REPORTED_RE.search(c))))
           for c in clauses):
        return True
    free = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)]
    return any(_done_in(c, title) or (not strict and REPORTED_RE.search(c)) for c in free) \
        or bool(YES_RE.match(message))


def _is_today(db: Any, note_id: str) -> bool:
    try:
        from thirties_core.lists import ListCatalog
        return ListCatalog(db).role(note_id) == "today"
    except Exception:
        return False


def refused(op: Dict[str, Any], turn: str, db: Any) -> str:
    """Why this change can't be written on the user's words this turn; "" when it can (or isn't judged here)."""
    if not turn or op.get("valid_until") or op.get("_twin"):
        return ""  # outside a turn, this morning's housekeeping, or the twin of a tick that went through
    message = _said(turn)
    kind = op.get("op")
    title = str(op.get("title") or "")
    if kind == "file_dropped" and not op.get("ticked") and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title} for good, so it isn't filed as dropped. Taking "
                "it off today leaves it on its own list.")
    if kind == "remove" and op.get("note_id") and not _is_today(db, str(op["note_id"])) \
            and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title}, so its line stays on its list. Taking it off "
                "today changes only today's plan and the Today note.")
    if kind == "complete" and not says_done(message, title):
        return (f"not written: the user's words don't say {title} is finished, so it isn't ticked. Something "
                "running, started or fine isn't done; it's done when they say it is.")
    from thirties_core.done_fit import names_something_else
    if kind == "complete" and names_something_else(message, title):  # "the glaze order is sent": not this item
        return (f"not written: the user's words name something other than {title}, so it isn't ticked.")
    # --- a count is never a name (done_counts.py); an untick only on the user's ask (reopen.py) ---
    from thirties_core.done_counts import counted_only
    if kind == "complete" and counted_only(message, title):  # "check those two off" for Fire two test tiles
        return (f"not written: the user's words only count items; they don't name {title}, so it isn't ticked.")
    if kind in ("untick", "unfile_done"):
        from thirties_core.reopen import untick_refused
        return untick_refused(message, title)
    # --- end a count, and an untick ---
    # --- a question, or a pointer at an item's parts, writes nothing for the item (said_for_it.py) ---
    from thirties_core.said_for_it import only_questions, parts_only, removal_in_question
    if kind in ("complete", "remove", "file_dropped") and parts_only(message, title):
        return f"not written: the user's words point at what's under or in {title}, not at the item itself."
    if kind == "complete" and only_questions(message) and not DONE_WORDS_RE.search(message):
        return f"not written: the user only asked about {title}, so it isn't ticked."
    if kind in ("remove", "file_dropped") and (only_questions(message) or removal_in_question(message)):
        return f"not written: the user only asked about taking {title} off, so its line stays."
    # --- end a question, or a pointer at parts ---
    return ""


__all__ = ["DROP_WORDS_RE", "DONE_WORDS_RE", "NEGATED_DONE_RE", "REPORTED_RE", "STATE_RE", "asks_to_drop", "says_done",
           "state_not_done", "told_not_to_tick", "refused"]
