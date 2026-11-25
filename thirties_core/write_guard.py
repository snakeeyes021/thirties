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
  fine") are no done on their own. Beside a done said in the same message ("Glazed. Kiln left warming
  overnight."), whether they undo it is the model's reading: the done goes through with a note saying so
  (``going_note``); a done word in the same sentence wins over merely all right, with no note ("the bisque unload
  went fine, all wrapped"). Words saying plainly it isn't done are never a done ("not yet"), nor are words telling
  the app not to tick it ("leave the bowls unticked", "don't tick it", "nowhere near done"), nor words naming
  another item that shares a word with it ("the glaze order is sent" for Glaze recipe test, beside a listed Send
  the glaze order: done_fit.py).

A tick that follows a done log_outcome accepted on the plan this turn (its own checks read the user's words) is
written: the plan and Joplin agree. Python marks such a tick; the model can't.

Changes made outside a turn (the app's own controls, the background, this morning's housekeeping) aren't judged
here: they have their own rules.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

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
# told it's far from done, or plainly not done: "nowhere near done", "isn't done"
_NOT_DONE_SAID = (
    r"\b(?:nowhere near|far from|anything but|not quite|not even close to|not close to|not yet)\s+(?:being\s+)?"
    r"(?:done|finished|complete|completed|ready|over)\b|"
    r"\b(?:isn'?t|is not|aren'?t|are not|wasn'?t|was not|weren'?t|were not|hasn'?t been|haven'?t been|not)\s+"
    r"(?:yet\s+|really\s+|actually\s+|even\s+)?(?:done|finished|complete|completed|over)\b")
NEGATED_DONE_RE = re.compile(
    r"\b(?:leave|keep|let)\b[^.!?\n]{0,40}?\b(?:un(?:ticked|checked|marked|done|finished)|"
    r"not (?:yet )?(?:ticked|checked|marked|done|finished)|open|as (?:it is|is)|alone|for (?:now|later))\b|"
    r"\b(?:stays?|remains?|staying)\s+(?:un(?:ticked|checked|marked)|open)\b|"
    r"\bun(?:tick|check|mark)(?:s|ing)?\b|"
    r"\b(?:don'?t|dont|do not|never|no need to|not to|hold off(?: on)?|wait (?:on|before|to))\s+"
    r"(?!forget\b|hesitate\b)(?:\w+\s+){0,2}?(?:tick|mark|check|cross)(?:ing)?\b|"
    r"\bno (?:tick|check ?mark)\b|" + _NOT_DONE_SAID, re.IGNORECASE)
_NOT_DONE_SAID_RE = re.compile(_NOT_DONE_SAID, re.IGNORECASE)
_POINTER_RE = re.compile(r"\b(?:it|them|that|this|those|these|both|they|theyre|they'?re)\b", re.IGNORECASE)
# a tick put off, as it's asked in a question or said outright: "could we skip ticking the bins?", "can we not mark it
# yet?", "shall we wait before marking it off?". "Why not tick it?" puts the tick forward.
PUT_OFF_TICK_RE = re.compile(
    r"(?:\bskip(?:s|ping)?|(?<!why )\bnot|\bwithout|\bbefore|\bhold(?:ing)? off(?: on)?|"
    r"\bwait(?:ing)?(?: (?:on|before|with|to))?|\bput(?:ting)? off|\bdelay(?:ing)?|\bleave off)\s+"
    r"(?:\w+\s+){0,2}?(?:tick|mark|check|cross)(?:s|ing)?\b", re.IGNORECASE)


def told_not_to_tick(message: Optional[str], title: str, others=()) -> str:
    """Why the user's words say not to tick this item, naming it ("leave the bowls unticked", "can we hold off on
    ticking the bowls?"), or say it's far from done ("nowhere near done"): in a clause naming it, in a clause
    pointing back with "it" or "them" whose sentence names no other item (or names this one too), or, when no
    clause names it, in any clause naming no other item. A question says nothing here unless it puts the tick off.
    Asked not to tick "it", with no name, beside a done ("Tapers: dipped. No tick on that one till they harden."),
    which item they mean is the model's reading (``told_not_note``); with nothing said done ("leave it unticked"), it
    stops the done. "" when nothing says so."""
    stop, _ = _told_not(message, title, others)
    if stop:
        return (f"The user said not to tick {title}, so it isn't marked done. Leave it open; it's done when they "
                "say it is.")
    return ""


def told_not_note(message: Optional[str], title: str, others=()) -> str:
    """One plain line for the model when a done goes through though the words ask not to tick "it" without naming
    it ("Tapers: dipped. No tick on that one till they harden."): the items it may mean, so the model can check
    which. ""
    when there's nothing to say, or when a hard stop holds the done (``told_not_to_tick``)."""
    stop, unnamed = _told_not(message, title, others)
    if stop or not unnamed:
        return ""
    items = [title] + [o for o in others if o and o != title]
    meant = [t for t in items if _about(message, t, [x for x in items if x != t])] or [title]
    said = ", ".join(f'"{c.strip()}"' for c in unnamed)
    return (f"(The user's message also asks not to tick something it doesn't name ({said}): it may mean "
            f"{', '.join(meant)}. If it means {title}, correct it, or ask which.)")


def _told_not(message: Optional[str], title: str, others=()) -> tuple:
    """(whether the words say not to tick this item, naming it, or say it's far from done; the clauses asking not
    to tick something they don't name)."""
    if not message:
        return False, []
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
        return False, []

    def names_another(text: str) -> bool:
        return any(_about(text, o, [x for x in others if x != o] + [title]) for o in others)

    named = [c for c in clauses if _about(c, title, others)]
    free = [c for c in clauses if not names_another(c) and c not in named]
    # --- a pointer is about what its own sentence names: "the weft is still going, don't tick it" is the weft ---
    pointing = [c for c in free if _POINTER_RE.search(c) and not any(
        c.strip() in s and names_another(s) and not _about(s, title, others) for s in sentences)]
    unnamed = [c for c in (pointing if named else free) if says_not(c)]
    if any(says_not(c) for c in named) or any(c not in put_off and _NOT_DONE_SAID_RE.search(c) for c in unnamed):
        return True, []
    # --- a pointer's tick held off, with no done in any other clause: no basis for one ---
    from thirties_core.plain_done import says_it_happened
    if unnamed and not (says_it_happened(" ; ".join(c for c in clauses if not says_not(c))) or YES_RE.match(message)):
        return True, []
    return False, unnamed
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
                    and not _names_another_thing(part, title, others):
                return True
    return False


def _names_another_thing(part: str, title: str, others: Iterable[str] = ()) -> bool:
    """The stretch names a thing that isn't the item: a phrase with none of the item's words in it ("the dishes"),
    or one that shares a word with it and names another of the ``others`` (done_fit.named_instead)."""
    from thirties_core.done_fit import _is_its, _phrases, named_instead
    if any(not any(_is_its(w, title) for w in p) for p in _phrases(DONE_WORDS_RE.sub(" ", part))):
        return True
    return bool(named_instead(part, title, others))


def _other_items(db: Any, title: str) -> List[str]:
    """The items besides this one the words could name: those planned around now, and the open ones Thirties
    keeps from the lists."""
    import json
    from datetime import timedelta
    from thirties_core.astronomy import get_current_time
    from thirties_core.models import SpanStatus
    out: List[str] = []
    try:
        pool = json.loads(db.get_sync_meta("open_pool") or "{}")
        out += [str(v.get("title") or "") for v in pool.values() if isinstance(v, dict)]
    except Exception:  # no database, or nothing kept yet
        pass
    try:
        now = get_current_time()
        out += [s.title for s in db.get_spans_between(now - timedelta(hours=18), now + timedelta(hours=18))
                if s.status == SpanStatus.PLANNED]
    except Exception:
        pass
    return [t for t in dict.fromkeys(out) if t and t != title]


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
    """Why the user's words say this item isn't finished: what they say about it is that it's not done ("not yet",
    "the glaze isn't dry"), or that it was only thought done, should have happened, or was called off. When no
    clause names it (a reply like "not yet" to a question about it), the clauses that name no other item are about
    it. "" when no such thing is said. Words saying it's under way or merely all right are no reason here: whether
    they undo a done said beside them is the model's reading (``going_note``)."""
    return _state_reading(message, title, others)[0]


def going_note(message: Optional[str], title: str, others=()) -> str:
    """One plain line for the model when a done goes through though the words about the item also say it's under
    way or merely all right ("Glazed. Kiln left warming overnight."), so it can check the item really finished.
    "" when there's nothing to say, or when a hard stop holds the done (``state_not_done``)."""
    stop, going = _state_reading(message, title, others)
    if stop or not going:
        return ""
    return (f"(The user's message also says {title} is under way or fine: if it hasn't really finished, ask, or "
            "correct it.)")


def _state_reading(message: Optional[str], title: str, others=()) -> tuple:
    """(why the words say the item isn't finished, or "", whether they say it's under way or merely all right)."""
    if not message:
        return "", False
    # --- named in an exception part of a done, or as still open: never what the done is about; the rest is read
    # without those parts (done_exceptions.py) ---
    from thirties_core.done_exceptions import left_out, without_exceptions
    left = left_out(message, title, others)
    if left:
        return left, False
    message = without_exceptions(message)
    # --- end an exception part ---
    told_not = told_not_to_tick(message, title, others)
    if told_not:
        return told_not, False
    # --- a done said ahead of time: promised, hoped for, or once something happens (done_ahead.py) ---
    from thirties_core.done_ahead import said_ahead
    ahead = said_ahead(message, title, others)
    if ahead:
        return ahead, False
    # --- end said ahead ---
    # --- begun, or coming next, beside a done for something else: no done for it (said_for_it.py) ---
    from thirties_core.said_for_it import begun_or_next
    onward = begun_or_next(message, title, others)
    if onward:
        return onward, False
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
                    "open; it's done when they say it is."), False
    # --- end taken back ---
    clauses = [c for c in _CLAUSE_RE.split(told) if c and c.strip() and not _ACK_RE.match(c)]
    # --- an item named only as when something else stops is no done; what a clause says is about its head
    # ("working until the kiln check": the working) (not_about_it.py) ---
    from thirties_core.not_about_it import end_refusal, head, only_as_an_end
    if only_as_an_end(clauses, title, others):
        return end_refusal(title), False
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
                    "done. Leave it open; it's done when they say it is."), False
        if any(not _own_word(m.group(0), title) for m in _CALLED_OFF_RE.finditer(c)):
            return (f"The user said {title} was moved or called off, not that it happened, so it isn't marked "
                    "done. Leave it open, or move it if they asked; it's done when they say it is."), False
    # --- end didn't happen ---
    going = []
    for c in about:
        if _done_in(c, title):
            continue
        if _NOT_DONE_RE.search(c):
            return (f"The user said {title} isn't finished, so it isn't marked done. Leave it open; it's done when "
                    "they say it is."), False
        if _state_in(c, title):
            going.append(c)
    if not going:
        return "", False
    # --- under way or all right, and no done said of the item: no basis for one ("the varnish is fine, the dishes
    # are done": the dishes' done) ---
    if not any(_done_in(d, title) or _done_beside(d, told_sentences, title, others) for d in about):
        return (f"The user said {title} is going or all right, not that it's finished, so it isn't marked done. "
                "Leave it open; it's done when they say it is."), False
    # --- merely all right, with a done in the same sentence or one said of the item anywhere: the done wins, with
    # nothing to note ("it went fine, all wrapped", "ok the list has it open. the shawl IS done") ---
    return "", any(_under_way_in(c, title) or not (_done_beside(c, told_sentences, title, others)
                                                    or any(_done_in(d, title) for d in about)) for c in going)


def says_done(message: Optional[str], title: str, others=(), strict: bool = False) -> bool:
    """Whether the user's words say this item happened: a done word (everyday ones too: "squared away", "that's one
    less thing") or its verb in the past, a report that it happened ("I've written them up"), said about it (or in a
    clause naming no other item), or a plain yes; and nothing about it says it's not done (``state_not_done``).
    ``strict``: only done words and the item's own verb count (any past tense about the item counts otherwise), and a
    question says nothing ("how come the rent got moved?")."""
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
    if not turn or op.get("valid_until") or op.get("_twin") or op.get("_done_on_plan"):
        return ""  # outside a turn, this morning's housekeeping, the twin of a tick that went through, or a done
        # the plan accepted this turn
    if op.get("_moved_on") and op.get("op") == "remove" and _is_today(db, str(op.get("note_id") or "")):
        return ""  # a full day's move written to Joplin: the line went on to its new day (full_day.py)
    message = _said(turn)
    kind = op.get("op")
    title = str(op.get("title") or "")
    # the user's own drop of a leftover that lives only on Today (Before today's drop action) is the ask: filed from
    # the Today note, never from a home list, whatever words came with it
    drop_asked = bool(op.get("_drop_asked")) and _is_today(db, str(op.get("note_id") or ""))
    if kind == "file_dropped" and not op.get("ticked") and not drop_asked and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title} for good, so it isn't filed as dropped. Taking "
                "it off today leaves it on its own list.")
    if kind == "remove" and op.get("note_id") and not _is_today(db, str(op["note_id"])) \
            and not asks_to_drop(message):
        return (f"not written: the user didn't ask to drop {title}, so its line stays on its list. Taking it off "
                "today changes only today's plan and the Today note.")
    from thirties_core.whole_routine import answers_the_question
    if kind == "complete" and not says_done(message, title) and not answers_the_question(message):
        return (f"not written: the user's words don't say {title} is finished, so it isn't ticked. Something "
                "running, started or fine isn't done; it's done when they say it is.")
    from thirties_core.done_fit import named_instead
    instead = named_instead(message, title, listed=_other_items(db, title)) if kind == "complete" else []
    if instead:  # "the glaze order is sent" beside Send the glaze order: not this item
        return f"not written: the user's words name {', '.join(instead)}, not {title}, so it isn't ticked."
    # --- a count is never a name (done_counts.py); an untick only on the user's ask (reopen.py) ---
    from thirties_core.done_counts import counted_only
    if kind == "complete" and counted_only(message, title):  # "check those two off" for Fire two test tiles
        return (f"not written: the user's words only count items; they don't name {title}, so it isn't ticked.")
    if kind in ("untick", "unfile_done"):
        from thirties_core.reopen import untick_refused
        return untick_refused(message, title)
    # --- end a count, and an untick ---
    # --- a question, or a pointer at an item's parts, ticks nothing for the item (said_for_it.py) ---
    # A removal is the model's reading of the user's words, a question included (an exasperated "seriously??"):
    # the floors for it are the drop word above (Dropped, a home list); a line on Today comes off and can go back.
    from thirties_core.said_for_it import only_questions, parts_only
    if kind == "complete" and parts_only(message, title):
        return f"not written: the user's words point at what's under or in {title}, not at the item itself."
    if kind == "complete" and only_questions(message) and not DONE_WORDS_RE.search(message):
        return f"not written: the user only asked about {title}, so it isn't ticked."
    # --- end a question, or a pointer at parts ---
    return ""


__all__ = ["DROP_WORDS_RE", "DONE_WORDS_RE", "NEGATED_DONE_RE", "PUT_OFF_TICK_RE", "REPORTED_RE", "STATE_RE", "asks_to_drop", "says_done",
           "going_note", "state_not_done", "told_not_note", "told_not_to_tick", "refused"]
