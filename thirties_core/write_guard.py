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
    # knocked out, said of a thing ("knocked out the thwart", "knocked it out"), never of the user ("knocked out by
    # a headache")
    r"one less thing|knocked (?:(?:it|that|them|those|this|the(?: \w+){1,3}) out|"
    r"out (?=(?:the|my|that|this|those|these|it|them|all)\b))|squared away|sorted out|"
    # dealt with, but not still to be or being dealt with, nor done badly ("dealt with it badly")
    r"(?<!to be )(?<!be )(?<!being )(?<!need )(?<!needs )(?<!get )(?<!gets )(?<!getting )"
    r"dealt with(?![^.!?\n]{0,30}\b(?:badly|poorly|wrong(?:ly)?|later)\b)|"
    r"(?:all|it'?s|that'?s|we'?re|i'?m) wrapped|that'?s a wrap|(?:it'?s|that'?s|is) in the bag|"
    r"nailed (?:it|that|them)|(?:it'?s|that'?s|they'?re|is|are|now) out of the way(?!\s+of\b)|"
    # off my plate when it is, or got there; wanting to get it off my plate is a plan
    r"(?:(?:it'?s|its|that'?s|they'?re|is|are|was|were|now)\s+|got (?:[\w']+ ){0,4}?)off my (?:plate|list|back)|"
    r"(?:crossed|ticked) (?:it|that|them|those) off|put to bed|finished up|over and done)\b|"
    r"[\u2713\u2714\u2611\u2705]", re.IGNORECASE)
# "the kiln permit is in", said as the end of what's said about it: handed in. Only for something that's handed in or
# sent off (a form, a permit, an application, an order), or an item that's about sending it (Mail the glaze samples);
# "the loaf is in" has only gone in the oven.
_IN_RE = re.compile(r"(?:\b(?:it'?s|its|that'?s|they'?re|is|are)|(?<=\w)'(?:s|re))\s+in\s*[.!]*\s*$", re.IGNORECASE)
_HANDED_IN_RE = re.compile(
    r"\b(?:permits?|forms?|applications?|orders?|claims?|paperwork|submissions?|entr(?:y|ies)|essays?|assignments?|"
    r"requests?|renewals?|returns?|ballots?|timesheets?|bids?|proposals?|invoices?|cheques?|appeals?|"
    r"nominations?|registrations?|enrol?ments?)\b", re.IGNORECASE)
_SENDING_RE = re.compile(r"\b(?:mail|post|send|submit|hand|file|lodge|return|deliver|apply|enter|register|enrol|enroll)\b",
                         re.IGNORECASE)
# a report that something happened, in the perfect ("I've written the glaze notes", "the parcel has gone") or an
# everyday irregular past: the model's reading of a done in words like these stands. Not said as could, would or
# will have; "I've started", "it's left" and "I've planned" aren't reports of a finish, and neither is giving up on
# it, putting it off or grumbling about it ("I've given up on it", "it has bugged me", "I've skipped it").
from thirties_core.started import NOT_A_FINISH  # noqa: E402  (started.py imports nothing of this module's)
# Gone wrong or taking long is no report either ("the milk's gone off", "it's taken forever").
_PARTICIPLES = (r"written|sent|gone(?!\s+(?:off|missing|wrong|bad|astray|sour|stale|mouldy|moldy|flat|dead|quiet)\b)|"
                r"given(?!\s+(?:up|in)\b)|done|seen|spoken|bought|brought|built|sold|told|won|swept|hung|wound|driven|"
                r"thrown|taken(?!\s+(?:ages|forever|long|so long|too long|longer|a while|ill|sick)\b)|"
                "made|eaten|drunk|paid|laid|fed|found|held|kept|lent|met|ridden|"
                "shaken|sewn|sown|sung|spun|struck|swum|torn|woven|worn|dug|ground|bound|fought|taught|caught|sought|"
                "stood|swung")
_NOT_REPORTED = tuple(dict.fromkeys(NOT_A_FINISH + (
    "asked", "wondered", "guessed", "worried", "listed", "added", "moved", "pushed", "postponed", "decided", "meant",
    "hoped", "tried", "used", "delayed")))
# Nor what should, would or could have happened, or what the user wishes had ("should've mailed it", "wish I had
# sent it", "if I had sent it").
REPORTED_RE = re.compile(
    r"(?<!would )(?<!could )(?<!should )(?<!might )(?<!must )(?<!will )(?<!'ll )(?<!'d )"
    r"(?<!would)(?<!could)(?<!should)(?<!might)(?<!must)"
    r"(?<!wish i )(?<!wish we )(?<!if i )(?<!if we )(?<!only i )(?<!only we )"
    r"\b(?:'ve|'s|have|has|had)\s+(?:just\s+|already\s+|finally\s+|now\s+|all\s+)?(?:been\s+)?"
    r"(?:" + _PARTICIPLES + r"|(?!(?:" + "|".join(_NOT_REPORTED) + r")\b)[a-z]{3,}ed)\b|"
    r"\b(?:wrote|spoke|built|sold|drove|gave(?!\s+(?:up|in)\b)|threw|swept|dug|wove|sewed|fought|taught|caught|"
    r"swung|hung|wound|bound|ground|tore|wore|rode|shook|sang|swam|spun|struck|bought|brought|ate|drank|lent|met|"
    r"won)\b", re.IGNORECASE)
# said, then taken back: "the permit is in, or so I thought", "I thought it was done", "I thought I'd mailed it".
# "I thought it would take longer" takes nothing back.
_MISTAKEN_RE = re.compile(
    r"\b(?:or so i (?:thought|was told|hoped)|so i thought|i thought so|"
    r"i thought (?:it|that|they|i|we|the (?:[\w']+ ){0,4}?[\w']+)(?:'s|'d|'re|\s+(?:was|were|is|are|had|has|have|did))"
    r"(?:\s+(?:already|all|been|just|finally))*\s+(?:done|finished|complete\w*|in|over|sorted|ticked|marked|off|"
    r"sent|paid|dealt with|taken care of|(?!(?:" + "|".join(NOT_A_FINISH) + r")\b)[a-z]{3,}ed)|"
    r"turns out (?:it|they|that)(?:'s| is| was| are| were)? ?n[o']t|apparently not)\b",
    re.IGNORECASE)
# what didn't happen: should, would or could have, or wished it had ("I should've mailed it", "wish I had sent it")
_UNREAL_RE = re.compile(r"\b(?:should|would|could|might|must)(?:'ve|\s+have|\s+of)\b|"
                        r"\b(?:wish|if only)\s+(?:i|we)(?:'d|\s+had)\b|\bif (?:i|we)(?:'d|\s+had)\s+\w+ed\b",
                        re.IGNORECASE)
# moved or called off: it didn't happen ("the visit got rescheduled", "it's been cancelled")
_CALLED_OFF_RE = re.compile(r"\b(?:re-?scheduled|postponed|cancell?ed|called off|put off|pushed back|rebooked|"
                            r"rained off|rain-?checked|bumped)\b", re.IGNORECASE)
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
# a tick put off, as it's asked in a question or said outright: "could we skip ticking the bins?", "can we not mark it
# yet?", "shall we wait before marking it off?". "Why not tick it?" puts the tick forward.
PUT_OFF_TICK_RE = re.compile(
    r"(?:\bskip(?:s|ping)?|(?<!why )\bnot|\bwithout|\bbefore|\bhold(?:ing)? off(?: on)?|"
    r"\bwait(?:ing)?(?: (?:on|before|with|to))?|\bput(?:ting)? off|\bdelay(?:ing)?|\bleave off)\s+"
    r"(?:\w+\s+){0,2}?(?:tick|mark|check|cross)(?:s|ing)?\b", re.IGNORECASE)


def told_not_to_tick(message: Optional[str], title: str, others=()) -> str:
    """Why the user's words say not to tick this item ("leave the bowls unticked", "don't tick it", "nowhere near
    done", "can we hold off on ticking it?"), said about it: in a clause naming it, in a clause pointing back with
    "it" or "them" that names no other item, or, when no clause names it, in any clause naming no other item. A
    question says nothing here unless it puts the tick off. "" when nothing says so."""
    if not message:
        return ""
    from thirties_core.started import _SENTENCE_RE, _asks
    others = [o for o in others if o and o != title]
    sentences = _SENTENCE_RE.findall(message)
    told = " ".join(t for t in sentences if not _asks(t))
    asked = " ".join(t for t in sentences if _asks(t) and PUT_OFF_TICK_RE.search(t))
    clauses = [c for c in _CLAUSE_RE.split(told) if c and c.strip()]
    put_off = [c for c in _CLAUSE_RE.split(asked) if c and c.strip() and PUT_OFF_TICK_RE.search(c)]
    clauses += put_off

    def says_not(c: str) -> bool:  # in a question, only a tick put off says so ("why isn't it done?" only asks)
        return bool(PUT_OFF_TICK_RE.search(c)) if c in put_off else bool(NEGATED_DONE_RE.search(c))

    if not any(says_not(c) for c in clauses):
        return ""
    named = [c for c in clauses if _about(c, title, others)]
    free = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)
            and c not in named]
    about = named + ([c for c in free if _POINTER_RE.search(c)] if named else free)
    if any(says_not(c) for c in about):
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
    """A done word in the same sentence as this clause, in a stretch about this item or naming no other one. A
    stretch that names another thing, planned or not ("the dishes are done"), is that thing's done, never this
    item's, whatever items are known here."""
    for sentence in sentences:
        if clause.strip() not in sentence:
            continue
        for part in _CLAUSE_RE.split(sentence):
            if part and part.strip() and _done_in(part, title) and not any(
                    _about(part, o, [x for x in others if x != o] + [title]) for o in others) \
                    and not _names_another_thing(part, title):
                return True
    return False


def _names_another_thing(part: str, title: str) -> bool:
    """The stretch names a thing that isn't the item: a phrase with none of the item's words in it ("the dishes"),
    or one that shares a word with it but names something else (done_fit.names_something_else)."""
    from thirties_core.done_fit import _is_its, _phrases, names_something_else
    if any(not any(_is_its(w, title) for w in p) for p in _phrases(DONE_WORDS_RE.sub(" ", part))):
        return True
    return bool(names_something_else(part, title))


def _done_in(clause: str, title: str) -> bool:
    return bool(DONE_WORDS_RE.search(clause) or _past_of_title(clause, title) or _handed_in(clause, title))


def _handed_in(clause: str, title: str) -> bool:
    """ "The grant form is in", "it's in" for Mail the grant form: handed in, for something that's handed in or an
    item about sending it."""
    return bool(_IN_RE.search(clause) and (_HANDED_IN_RE.search(f"{clause} {title}") or _SENDING_RE.search(title)))


def _own_word(word: str, title: str) -> bool:
    """The word is one of the item's own ("cancelled" for Cancel the sander rental)."""
    said = re.sub(r"[^a-z]", "", word.lower())
    return any(said.startswith(w[:4]) for w in re.findall(r"[a-z]+", str(title or "").lower()) if len(w) >= 4)


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
    from thirties_core.started import _SENTENCE_RE, _asks, _without_record_words, without_caused
    others = [o for o in others if o and o != title]
    # a question, or the app's own status quoted ("why does it still say 'not marked'?"), says nothing about it; a
    # done the user is only getting done ("getting the fence fixed") is no done word
    told_sentences = [t for t in _SENTENCE_RE.findall(without_caused(_without_record_words(message))) if not _asks(t)]
    told = " ".join(told_sentences)
    # --- said, then taken back ("the permit is in, or so I thought"): about it, or naming no other item ---
    for s in told_sentences:
        if _MISTAKEN_RE.search(s) and (_about(s, title, others) or not any(
                _about(s, o, [x for x in others if x != o] + [title]) for o in others)):
            return (f"The user said they only thought {title} was finished, so it isn't marked done. Leave it "
                    "open; it's done when they say it is.")
    # --- end taken back ---
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
    # --- what should or could have happened, or was moved or called off, didn't happen ---
    for c in about:
        if _UNREAL_RE.search(c):
            return (f"The user said {title} should or could have happened, not that it did, so it isn't marked "
                    "done. Leave it open; it's done when they say it is.")
        if any(not _own_word(m.group(0), title) for m in _CALLED_OFF_RE.finditer(c)):
            return (f"The user said {title} was moved or called off, not that it happened, so it isn't marked "
                    "done. Leave it open, or move it if they asked; it's done when they say it is.")
    # --- end didn't happen ---
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
    from thirties_core.started import FINISHED_RE, _SENTENCE_RE, _asks, not_finished, without_caused
    others = [o for o in others if o and o != title]
    if not_finished(message, title, others) in ("begun", "not") or state_not_done(message, title, others):
        return False
    message = without_caused(message)  # "getting the fence fixed": what the user is getting done isn't done
    if strict:
        message = " ".join(t for t in _SENTENCE_RE.findall(message) if not _asks(t))
    else:  # a done word inside a question asked outright is asked about, never said (said_for_it.asked_outright)
        from thirties_core.said_for_it import asked_outright, tick_asked
        if tick_asked(message, title, others):  # "can we mark the varnish off?": read as on the plan
            return True
        message = " ".join(t for t in _SENTENCE_RE.findall(message) if not asked_outright(t))
    clauses = [c for c in _CLAUSE_RE.split(message) if c and c.strip()]
    if any(_about(c, title, others) and (_done_in(c, title) or (not strict and (FINISHED_RE.search(c)
                                                                                or REPORTED_RE.search(c))))
           for c in clauses):
        return True
    free = [c for c in clauses if not any(_about(c, o, [x for x in others if x != o] + [title]) for o in others)]
    # a report in a clause that doesn't name the item counts only when it points back ("I've mailed it")
    return any(_done_in(c, title) or (not strict and REPORTED_RE.search(c) and _POINTER_RE.search(c))
               for c in free) or bool(YES_RE.match(message))


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
    # a question about the item still being on ("why is it still on?") asks for a change only when it names it
    if kind in ("remove", "file_dropped") and (only_questions(message, [title])
                                                or removal_in_question(message, [title])):
        return f"not written: the user only asked about taking {title} off, so its line stays."
    # --- end a question, or a pointer at parts ---
    return ""


__all__ = ["DROP_WORDS_RE", "DONE_WORDS_RE", "NEGATED_DONE_RE", "PUT_OFF_TICK_RE", "REPORTED_RE", "STATE_RE", "asks_to_drop", "says_done",
           "state_not_done", "told_not_to_tick", "refused"]
