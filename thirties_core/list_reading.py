"""How a list note is read: what each line is, what its name and notes are, and what state it's in.

People write lists with little markup. Meaning comes from where a line sits (its parent line, its heading) and
from plain words after a separator, so the parser reads every line the same way for every part of Thirties (the
day's options, the morning plan, the plans for a week or a month, what's next, Before today, the item lists the
model reads, and matching what the user says was done). One reading, in this order:

A. Kind of line
   - A checkbox line with text is an **entry**. Plain bullets, paragraphs and headings are **context**. An empty
     checkbox is a **placeholder** and is skipped.
   - A checkbox line under another one that states a condition or a fact ("Requires the new key", "Needs a
     signature", "The landlord says it can wait") is a **condition** on its parent, not a task. Ticked means the
     condition is met; it never closes the parent.
   - A line ending in a colon is a **group** label; a question with lines under it ("Which one do I keep?") is a
     **question**, and the lines under it are **options**: a decision to offer, not tasks.
   - A line that is only a link is a **link**: shown by its site, never scheduled.
   Only entries are ever offered or scheduled.

B. Name and notes
   - Parentheses stay in the name ("Cable (short)" and "Cable (long)" are two items) unless they hold a note:
     a dependency ("(after the paint dries)"), a fact ("(not in stock)"), a price, a doubt ("(both?)"), or, on a
     ticked line, the reason it was dropped.
   - A trailing " - X" (or " -- X") is split off only when X is a known kind: a state or blocker, a drop word, a
     time estimate (a whole number of minutes, a fraction of an hour, "N+", "N+N", with "(hours)" if said), a
     price, a when, or a known owner. Otherwise it's part of the name ("Router - guest network"), and so is a tail
     that only starts like a note: a title ("Book club - No Small Matter"), a drop word with words after it ("Film
     night - Skip the Interval"), or what the item is about ("Garden gate - broken spring"). A deadline in
     parentheses ("(before Friday)") is a when, not something it waits for. A leading
     weekday, date, "Due <day>" or order marker ("FIRST - ") is a note too.
   - The display name is the name without its notes. The item's identity (its id, its exact link to the line)
     stays the full original line, so a note added or changed never breaks the link.

C. Nesting
   - An entry means its parents plus its own name (item_path.py names it so; matching and the model get it too).
   - A parent with open entries under it that are still to do (or a label with such entries) is a **container**:
     its children are planned, never the parent, and it shows its progress as "n of m done". A link, a question,
     or a dropped, paused or optional line under it doesn't count: with only those, the parent is offered itself.
   - Ticks never follow each other: a parent with every child ticked stays open, and a ticked parent never closes
     its children. Open lines under a ticked parent are **leftovers**: kept, never pushed, asked about once.

D. States
   - A drop word ("- cancelled", "(not worth it)" on a ticked line) or a dropped section means **dropped**:
     filed to Dropped, not Done.
   - A heading (or a bold or "Label:" line) sets the state of everything under it: current, long-running, on hold,
     closed, dropped, undecided (offered as a decision), later as a conversation, or kept for reference. "Blocked by
     X" headings block everything under them.
   - A blocker keeps an entry out of "do now". A blocker that only holds up the last step (sending, posting,
     delivering) doesn't hold back the work before it: the entry can still be prepared.

E. Context in the note body
   Sections of the body below the list that match an entry by name are evidence of progress: not started, started
   (a heading or greeting only), drafting (some text), ready (signed off, or marked ready). Shown when relevant,
   never used to tick anything. A section matching no entry is mentioned once as a possible missing item.

F. Writing back
   A line whose text is an item id or another bare id-like token is never written into a note (``refused_text``).

Menus and routines are read as before: menu choices and routine steps are entries like any other.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --- kinds of line ---------------------------------------------------------------------------------------------

ENTRY, CONDITION, GROUP, QUESTION, LINK, PLACEHOLDER = "entry", "condition", "group", "question", "link", "placeholder"

# --- states ----------------------------------------------------------------------------------------------------

CURRENT, LONG_RUNNING, ON_HOLD, CLOSED, DROPPED = "current", "long_running", "on_hold", "closed", "dropped"
UNDECIDED, LATER_TALK, REFERENCE, LEFTOVER, OPTION = "undecided", "later_talk", "reference", "leftover", "option"
# States an entry can be in and still be offered or planned ("" is a plain open entry).
OFFERED_STATES = ("", CURRENT, LONG_RUNNING)
STATE_WORDS = {
    CURRENT: "current", LONG_RUNNING: "long-running", ON_HOLD: "on hold", CLOSED: "closed", DROPPED: "dropped",
    UNDECIDED: "undecided: offer it as a decision, not a task", LATER_TALK: "later, as a conversation",
    REFERENCE: "kept for reference", LEFTOVER: "left open under a finished item", OPTION: "an option to choose",
}

_URL = re.compile(r"^<?(https?://\S+|www\.\S+)>?$", re.IGNORECASE)
_MD_LINK_ONLY = re.compile(r"^\[[^\]]*\]\((https?://[^)\s]+)\)$", re.IGNORECASE)

# A checkbox child that states a condition or a fact about its parent, rather than a step.
_CONDITION_START = re.compile(
    r"^(requires?|required|needs?|need to|depends on|dependent on|waiting (on|for)|blocked (by|on)|"
    r"it'?s|it is|it was|there is|there are|there'?s|there were|only if|unless|as long as)\b", re.IGNORECASE)
_SOMEONE_SAYS = re.compile(r"^[A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,2} (says|said|thinks|shows|show|reports)\b")
# conditions that hold the parent up until they're met (ticked); the rest are facts
_REQUIREMENT = re.compile(r"^(requires?|required|needs?|need to|depends on|dependent on|waiting (on|for)|"
                          r"blocked (by|on)|only if|unless|as long as)\b", re.IGNORECASE)

# --- notes on an entry ------------------------------------------------------------------------------------------

DROP_WORDS = re.compile(r"^(moot|nope|abandoned|cancell?ed|dropped|not needed|no longer needed|never ?mind|"
                        r"won'?t do|not doing( it)?|skip(ped)?|scrapped|obsolete)\b", re.IGNORECASE)
# a drop reason on a ticked line, in parentheses: "(too pricey)", "(not worth it)"
_DROP_REASON = re.compile(r"^(too\b|not worth|no longer|not needed|moot|nope|abandoned|cancell?ed|dropped|"
                          r"scrapped|gave up|not free|never mind)", re.IGNORECASE)
STATE_NOTE = re.compile(r"^(missing\b|no\b|wait(ing)?\b|on hold\b|blocked\b|stuck\b|pending\b|out of\b|"
                        r"lost\b|broken\b|need(s)?\b|paused\b|parked\b|in progress\b|started\b|half ?done\b|"
                        r"almost done\b|have it\b|got it\b|ordered\b|shipped\b|sent\b|ready\b)", re.IGNORECASE)
# blockers among the state notes (the rest just say where it stands)
_BLOCKING = re.compile(r"^(missing\b|no\b|wait(ing)?\b|on hold\b|blocked\b|stuck\b|pending\b|out of\b|lost\b|"
                       r"broken\b|need(s)?\b|paused\b|parked\b)", re.IGNORECASE)
# what a blocker holds up when it's only needed for the last step: getting the thing to someone
LAST_STEP = re.compile(r"\b(address|addresses|stamps?|postage|shipping|courier|label|mail(ing)?|post(ing)?|"
                       r"send(ing)?|deliver(y|ing)?|drop[- ]off|hand[- ]in|submit(ting)?|upload(ing)?)\b",
                       re.IGNORECASE)

_ESTIMATE = re.compile(r"^(?:~\s*)?(\?|\d+(?:\.\d+)?(?:\s*/\s*\d+)?(?:\s*\+\s*(?:\d+(?:\.\d+)?)?)?)"
                       r"\s*(\(?\s*(?:h|hr|hrs|hour|hours|m|min|mins|minutes?)\s*\)?)?$", re.IGNORECASE)
_PRICE = re.compile(r"^~?\s*(?:\$|€|£)\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:each|ea|total))?$|^~?\s*\d[\d,]*(?:\.\d+)?"
                    r"\s*(?:\$|€|£|dollars|usd|eur)$", re.IGNORECASE)
_WEEKDAY = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?"
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_WHEN = re.compile(
    r"^(?:" + _WEEKDAY + r"(?:\s+(?:morning|afternoon|evening|night))?"
    r"|(?:by|before|after|until|till|til|due)\s+.+"
    r"|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?"
    r"|\d{1,2}(?::\d{2})\s*(?:am|pm|a\.m\.|p\.m\.)?|\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)"
    r"|\d{4}-\d{2}-\d{2}"
    r"|today|tonight|tomorrow|this (?:week|weekend|month|morning|afternoon|evening)|next (?:week|month)"
    r"|end of (?:the )?(?:week|month|year|" + _MONTH + r")|(?:this|next) " + _WEEKDAY + r")$", re.IGNORECASE)
_LEADING_WHEN = re.compile(r"^(" + _WEEKDAY + r"|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?|\d{4}-\d{2}-\d{2}|"
                           r"due\s+\S+(?:\s+\S+)?|today|tomorrow|tonight)\s+-{1,2}\s+(.+)$", re.IGNORECASE)
_LEADING_ORDER = re.compile(r"^(-{2,}\s*[A-Z][A-Z ,'!?]*[A-Z!?]|(?:FIRST|SECOND|THIRD|NEXT|LAST|START HERE|THEN))"
                            r"\s*-{1,3}\s+(.+)$")
_YEAR = re.compile(r"^(19|20)\d\d$")  # "Title - 1987" is a year in the name, not an estimate
_OWNER_WORDS = re.compile(r"^(me|mine|myself)$", re.IGNORECASE)
_PAREN = re.compile(r"\s*\(([^()]*)\)")
_DEPENDENCY = re.compile(r"^(depends on|after|before|until|till|til|wait(ing)?( for| on| til| till| until)?|"
                         r"once|when)(?![\w-])", re.IGNORECASE)
_DEADLINE = re.compile(r"^(?:before|by|until|till|til|due)\s+(.+)$", re.IGNORECASE)  # "(before Friday)": a when
# a whole word, not part of a hyphenated one ("forget-me-not" is a name)
_FACT = re.compile(r"(?<![\w-])(but|not|missing|show as|shows as|isn'?t|aren'?t|no longer)(?![\w-])", re.IGNORECASE)
# state words that, with more words after them, describe what the item is about ("broken spring"), not where it is
_ABOUT_WORDS = ("broken", "lost")
_DROP_TAIL = re.compile(r"(for now|now|any ?more|after all)\W*", re.IGNORECASE)
_PLACE = re.compile(r"^(at|from|@)\s+\S", re.IGNORECASE)
_UPDATE = re.compile(r"\s+-{3,}\s+(.+)$")


@dataclass
class LineReading:
    """One line, read: what kind it is, its name, and the notes split off it."""
    kind: str
    name: str
    notes: List[Dict[str, str]] = field(default_factory=list)  # {"kind": ..., "text": ...}

    def first(self, kind: str) -> str:
        return next((n["text"] for n in self.notes if n["kind"] == kind), "")

    @property
    def dropped(self) -> str:
        """The drop word or reason, "" when it isn't dropped."""
        return self.first("dropped")

    @property
    def blocker(self) -> str:
        return next((n["text"] for n in self.notes if n["kind"] in ("blocker", "dependency")), "")

    @property
    def estimate_minutes(self) -> int:
        text = self.first("estimate")
        return estimate_minutes(text) if text else 0


def estimate_minutes(text: str) -> int:
    """A written estimate in minutes: a bare whole number is minutes ("45"), a fraction is a part of an hour
    ("1/4" → 15), "N+" is at least N, "N+M" is both, "(hours)" or "h" turns it into hours; "?" is unknown (0)."""
    m = _ESTIMATE.match(str(text or "").strip())
    if not m or m.group(1) == "?":
        return 0
    raw, unit = m.group(1).replace(" ", ""), (m.group(2) or "").strip("() ").lower()
    hours = unit.startswith("h")
    total = 0.0
    for part in [p for p in raw.split("+") if p]:
        if "/" in part:
            a, b = part.split("/", 1)
            try:
                total += 60.0 * float(a) / float(b) if float(b) else 0.0
            except ValueError:
                return 0
            continue
        try:
            total += float(part) * (60.0 if hours else 1.0)
        except ValueError:
            return 0
    return int(round(total))


def _is_drop(x: str) -> bool:
    """A drop word that stands alone ("cancelled", "scrapped: too dear"), not the first word of a name
    ("Skip the Interval")."""
    m = DROP_WORDS.match(x)
    if not m or x[m.end():m.end() + 1] == "-":
        return False
    rest = x[m.end():].strip()
    return not rest or rest[0] in ",:;(—–-" or bool(_DROP_TAIL.fullmatch(rest))


def _state_kind(x: str) -> Optional[str]:
    """"blocker" or "state" for a tail that says where the item stands ("waiting on posts", "no reply yet"); None
    when the words only start like one: a title ("No Small Matter"), a hyphenated word ("no-knead"), or what the
    item is about ("broken spring")."""
    m = STATE_NOTE.match(x)
    if not m or x[m.end():m.end() + 1] == "-":
        return None
    rest = x[m.end():].strip()
    if rest:
        if rest[0].isupper() and not x.isupper():
            return None
        if m.group(1).strip().lower() in _ABOUT_WORDS:
            return None
    return "blocker" if _BLOCKING.match(x) else "state"


def _note_kind(text: str, owners: Iterable[str] = ()) -> Optional[str]:
    """The kind of a " - X" tail, or None when it's part of the name."""
    x = text.strip().strip(".")
    if not x:
        return None
    if _is_drop(x):
        return "dropped"
    if _ESTIMATE.match(x) and not _YEAR.match(x):
        return "estimate"
    if _PRICE.match(x):
        return "price"
    if _WHEN.match(x):
        return "when"
    state = _state_kind(x)
    if state:
        return state
    low = x.lower()
    if _OWNER_WORDS.match(x) or low in {o.strip().lower() for o in owners if o and o.strip()}:
        return "owner"
    return None


def _paren_kind(inner: str, ticked: bool) -> Optional[str]:
    """The kind of a parenthesis that's a note, or None when it's part of the name."""
    x = inner.strip()
    if not x:
        return None
    if ticked and _DROP_REASON.match(x):
        return "dropped"
    if not ticked and _is_drop(x):
        return "dropped"
    deadline = _DEADLINE.match(x)
    if deadline and _WHEN.match(deadline.group(1).strip()):
        return "when"
    if _DEPENDENCY.match(x):
        return "dependency"
    if _PRICE.match(x) or x.startswith(("~$", "$", "€", "£")):
        return "price"
    if x.endswith("?"):
        return "doubt"
    if _FACT.search(x):
        return "fact"
    if _PLACE.match(x):
        return "place"
    if _ESTIMATE.match(x) and re.search(r"[a-z]", x, re.IGNORECASE):  # "(20 min)", not "(2)": a count is a name
        return "estimate"
    return None


def is_link(text: str) -> bool:
    t = str(text or "").strip()
    return bool(_URL.match(t) or _MD_LINK_ONLY.match(t))


def link_site(text: str) -> str:
    """A link shown by its site: "shop.example.com"."""
    t = str(text or "").strip().strip("<>")
    m = _MD_LINK_ONLY.match(t)
    url = m.group(1) if m else t
    host = re.sub(r"^(https?://)?(www\.)?", "", url, flags=re.IGNORECASE).split("/")[0].split("?")[0]
    return host or url


def is_condition(text: str) -> bool:
    """A checkbox line that states a condition or a fact about its parent (only read so when it's nested)."""
    t = str(text or "").strip()
    if _CONDITION_START.match(t) or _SOMEONE_SAYS.match(t):
        return True
    words = t.split()
    return len(words) >= 9 and t.endswith(".")  # a full sentence describing a situation


def is_requirement(text: str) -> bool:
    """A condition that holds its parent up until it's ticked ("Requires ...", "Needs ...")."""
    return bool(_REQUIREMENT.match(str(text or "").strip()))


def read_text(text: str, ticked: bool = False, nested: bool = False, has_children: bool = False,
              owners: Iterable[str] = ()) -> LineReading:
    """Read one checkbox line's text (B, and the kind from A that a line can tell on its own)."""
    raw = str(text or "").strip()
    if not raw:
        return LineReading(PLACEHOLDER, "")
    if is_link(raw):
        return LineReading(LINK, link_site(raw))
    if nested and is_condition(raw):
        return LineReading(CONDITION, raw)
    notes: List[Dict[str, str]] = []
    name = raw
    upd = _UPDATE.search(name)  # "... --- an update added later"
    if upd:
        notes.append({"kind": "update", "text": upd.group(1).strip()})
        name = name[:upd.start()].rstrip()
    for pattern, kind in ((_LEADING_ORDER, "order"), (_LEADING_WHEN, "when")):
        m = pattern.match(name)
        if m:
            notes.append({"kind": kind, "text": m.group(1).strip(" -")})
            name = m.group(2).strip()
    while True:  # from the end, while each is a known kind: a " - X" / " -- X", or a note in parentheses
        m = re.match(r"^(.*\S)\s+-{1,2}\s+([^-\s].*?)\s*$", name)
        kind = _note_kind(m.group(2), owners) if m else None
        if kind is not None:
            notes.append({"kind": kind, "text": m.group(2).strip()})
            name = m.group(1).rstrip()
            continue
        p = re.search(r"\s*\(([^()]*)\)\s*$", name)  # "- 1/6 (after the frost)": the note, then the estimate
        kind = _paren_kind(p.group(1), ticked) if p and p.start() > 0 else None
        if kind is None:
            break
        notes.append({"kind": kind, "text": p.group(1).strip()})
        name = name[:p.start()].rstrip()
    kept = []
    for m in list(_PAREN.finditer(name)):
        kind = _paren_kind(m.group(1), ticked)
        if kind is not None:
            notes.append({"kind": kind, "text": m.group(1).strip()})
            kept.append(m)
    for m in reversed(kept):
        name = (name[:m.start()] + name[m.end():])
    name = re.sub(r"\s{2,}", " ", name).strip().rstrip(" -") or raw
    if name.endswith(":"):
        return LineReading(GROUP, name.rstrip(":").strip() or name, notes)
    if name.endswith("?") and has_children:
        return LineReading(QUESTION, name, notes)
    return LineReading(ENTRY, name, notes)


# --- sections ---------------------------------------------------------------------------------------------------

_SECTION_STATES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^(blocked|waiting)\s+(by|on|for)\b", re.IGNORECASE), "blocked"),
    (re.compile(r"\b(moot|abandoned|dropped|cancell?ed|nope|won'?t do|not doing|scrapped)\b", re.IGNORECASE),
     DROPPED),
    (re.compile(r"^(done|completed?|finished|archived?|listened|watched|closed)\b", re.IGNORECASE), CLOSED),
    (re.compile(r"\b(old|older|previous|for reference|reference|history)\b", re.IGNORECASE),
     REFERENCE),
    (re.compile(r"\b(discuss|talk about|to talk|talk (?:it |this |that )?over|bring up|conversation)\b",
                re.IGNORECASE), LATER_TALK),
    (re.compile(r"\b(on hold|paused|parked|probation|hold off|backburner|back burner|icebox)\b", re.IGNORECASE),
     ON_HOLD),
    (re.compile(r"(\?\s*$|\bundecided\b|\bconsidering\b|\bmaybe\b|\bwishlist\b)", re.IGNORECASE), UNDECIDED),
    (re.compile(r"\b(running|ongoing|long[- ]term|long[- ]running|continuous)\b", re.IGNORECASE), LONG_RUNNING),
    (re.compile(r"^(active|current|short[- ]term|today|this week|now|in progress|doing|up next|next up)\b",
                re.IGNORECASE), CURRENT),
]


def section_state(name: str) -> Tuple[str, str]:
    """(state, blocker) a heading or label sets for what's under it: ("dropped", ""), ("", "the new lease") for
    "Blocked by the new lease", ("", "") for a heading that only names a topic."""
    text = re.sub(r"[*_#`]", "", str(name or "")).strip().rstrip(":").strip()
    if not text:
        return "", ""
    for pattern, state in _SECTION_STATES:
        m = pattern.search(text)
        if m:
            if state == "blocked":
                return "", text[m.end():].strip(" :-") or text
            return state, ""
    return "", ""


_BOLD_LABEL = re.compile(r"^\s{0,3}\*\*\s*-?\s*(.+?)\s*\*\*\s*:?\s*$")
_COLON_LABEL = re.compile(r"^\s{0,3}([A-Za-z][^:]{0,40}):\s*$")
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*(?:\s*\*){2,}|_{3,}|#)\s*$")
_BULLET = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])\s+(.*)$")


# --- a whole note -----------------------------------------------------------------------------------------------

@dataclass
class Line:
    """One checklist line of a note, read in its place."""
    index: int                        # its place among the note's checklist lines (open and ticked), as ids follow
    text: str                         # the line's own words, as written (its identity)
    ticked: bool
    indent: int
    parent: Optional[int]             # the checklist line it's nested under
    heading: str                      # the nearest "#" heading above it
    reading: LineReading
    path: List[str] = field(default_factory=list)  # parent names, top first (the heading not included)
    state: str = ""
    blocker: str = ""
    blocker_last_step: bool = False   # the blocker only holds up the last step (sending), not the work before it
    children: List[int] = field(default_factory=list)
    conditions: List[Dict[str, Any]] = field(default_factory=list)  # {"text", "met", "holds"}
    context: List[str] = field(default_factory=list)                # plain lines nested under it
    progress: str = ""                # from the body: not_started / started / drafting / ready

    @property
    def kind(self) -> str:
        return self.reading.kind

    @property
    def name(self) -> str:
        return self.reading.name


@dataclass
class BodySection:
    """A stretch of the note's body below its list: a heading or greeting line and the text under it."""
    head: str
    lines: List[str]

    @property
    def progress(self) -> str:
        return section_progress(self)


@dataclass
class NoteReading:
    lines: List[Line]
    sections: List[BodySection] = field(default_factory=list)
    unmatched: List[BodySection] = field(default_factory=list)  # body sections no entry matches

    def entry_children(self, line: Line) -> List[Line]:
        return [self.lines[i] for i in line.children if self.lines[i].kind not in (CONDITION, PLACEHOLDER)]

    def _open_work(self, line: Line) -> bool:
        """An open line that is still to do: an entry in a live state, or a label with such an entry under it."""
        if line.ticked or line.state not in OFFERED_STATES:
            return False
        if line.kind == ENTRY:
            return True
        return line.kind == GROUP and any(self._open_work(self.lines[i]) for i in line.children)

    def counts(self, line: Line) -> Tuple[int, int]:
        """(done, all) of the work directly under a line: entries ticked (not dropped), and those plus the open
        ones still to do. Links, questions and their choices, and lines in a state that isn't offered (cancelled,
        on hold...) don't count, so a parent with only those beneath it is no container: it's offered itself."""
        kids = self.entry_children(line)
        done = sum(1 for k in kids if k.ticked and k.kind == ENTRY and k.state != DROPPED)
        return done, done + sum(1 for k in kids if self._open_work(k))


def _indent(prefix: str) -> int:
    return len(prefix.replace("\t", "    "))


def read_note(body: str, someday_sections: Sequence[str] = (), owners: Iterable[str] = ()) -> NoteReading:
    """Every checklist line of a note, read in its place (A to D), and the body's sections matched to them (E)."""
    from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX
    raw_lines = (body or "").splitlines()
    found: List[Tuple[int, str, bool, int, str, str, str]] = []  # (row, text, ticked, indent, heading, state, blocker)
    context_rows: List[Tuple[int, int, str]] = []  # (row, indent, text) of plain bullets
    heading, label_state, label_blocker = "", ("", ""), ""
    head_state: Tuple[str, str] = ("", "")
    for row, line in enumerate(raw_lines):
        h = HEADING_REGEX.match(line)
        if h:
            heading = h.group(1).strip()
            head_state = section_state(heading)
            label_state = ("", "")
            continue
        m = CHECKBOX_REGEX.match(line)
        ticked = False
        if not m:
            m = DONE_CHECKBOX_REGEX.match(line)
            ticked = bool(m)
        if m:
            state = label_state[0] or head_state[0]
            blocker = label_state[1] or head_state[1]
            found.append((row, m.group(2).strip(), ticked, _indent(m.group(1)), heading, state, blocker))
            continue
        bold = _BOLD_LABEL.match(line) or (_COLON_LABEL.match(line) if not _BULLET.match(line) else None)
        if bold:
            got = section_state(bold.group(1))
            if got != ("", ""):
                label_state = got
            continue
        if _RULE.match(line) and line.strip() in ("---", "* * *", "***", "___"):
            label_state = ("", "")
            continue
        b = _BULLET.match(line)
        if b and b.group(2).strip():
            context_rows.append((row, _indent(b.group(1)), b.group(2).strip()))
    # structure
    has_child = [False] * len(found)
    stack: List[Tuple[int, int]] = []  # (indent, index)
    parents: List[Optional[int]] = []
    for i, (_, _, _, ind, head, _, _) in enumerate(found):
        if i and found[i - 1][4] != head:
            stack = []
        while stack and stack[-1][0] >= ind:
            stack.pop()
        parents.append(stack[-1][1] if stack else None)
        if stack:
            has_child[stack[-1][1]] = True
        stack.append((ind, i))
    lines: List[Line] = []
    for i, (row, text, ticked, ind, head, state, blocker) in enumerate(found):
        reading = read_text(text, ticked=ticked, nested=parents[i] is not None, has_children=has_child[i],
                            owners=owners)
        lines.append(Line(i, text, ticked, ind, parents[i], head, reading, state=state, blocker=blocker))
    for ln in lines:
        if ln.parent is not None:
            lines[ln.parent].children.append(ln.index)
    # plain lines nested under a checklist line are notes on it
    rows = [f[0] for f in found]
    for row, ind, text in context_rows:
        before = [i for i, r in enumerate(rows) if r < row]
        if before:
            owner = lines[before[-1]]
            while owner is not None and owner.indent >= ind:
                owner = lines[owner.parent] if owner.parent is not None else None
            if owner is not None:
                owner.context.append(text)
    # conditions on their parents
    for ln in lines:
        if ln.kind == CONDITION and ln.parent is not None:
            lines[ln.parent].conditions.append({"text": ln.text, "met": ln.ticked, "holds": is_requirement(ln.text)})
    # path, state, blocker (top down: parents come first)
    for ln in lines:
        par = lines[ln.parent] if ln.parent is not None else None
        ln.path = (par.path + [par.name]) if par is not None else []
        ln.state = _state_of(ln, par)
        own = ln.reading.blocker
        held = next((c["text"] for c in ln.conditions if c["holds"] and not c["met"]), "")
        inherited = par.blocker if par is not None and par.state not in (DROPPED, CLOSED) else ""
        ln.blocker = own or held or ln.blocker or inherited
        ln.blocker_last_step = bool(ln.blocker) and bool(LAST_STEP.search(ln.blocker))
    reading = NoteReading(lines)
    _read_body(reading, raw_lines, rows)
    return reading


def _state_of(ln: Line, par: Optional[Line]) -> str:
    """D: what the line itself says first (dropped), then where it sits."""
    if ln.reading.dropped:
        return DROPPED
    if par is not None:
        if par.state in (DROPPED, CLOSED, REFERENCE):
            return par.state
        if par.ticked or par.state == LEFTOVER:
            return LEFTOVER
        if par.kind == QUESTION:
            return OPTION
        if par.state and par.state not in (OPTION,):
            return ln.state or par.state
    return ln.state


# --- E: the body below the list ---------------------------------------------------------------------------------

_SIGN_OFF = re.compile(r"^(best|cheers|thanks|many thanks|regards|kind regards|warm regards|warmly|sincerely|"
                       r"yours|yours truly|all the best|love|take care|see you)\b[^.]{0,20}[,!.]?\s*$", re.IGNORECASE)
_READY_MARK = re.compile(r"^[\[(]?\s*(ready( to (send|go|post|print))?|final( version)?|finished|done)\s*[\])]?[.!]?$",
                         re.IGNORECASE)
_GREETING = re.compile(r"^(hi|hello|hey|dear|to|greetings|good (morning|afternoon|evening))\b", re.IGNORECASE)


def section_progress(sec: BodySection) -> str:
    """How far a section's text has got: "started" (a heading or greeting only), "drafting" (text, no sign-off),
    "ready" (signed off, or marked ready)."""
    text = [x.strip() for x in sec.lines if x.strip()]
    if any(_READY_MARK.match(x) for x in text):
        return "ready"
    for k, x in enumerate(text):
        if _SIGN_OFF.match(x) and k > 0:
            return "ready"
    body = [x for x in text if not _GREETING.match(x)]
    return "drafting" if body else "started"


def _read_body(reading: NoteReading, raw_lines: List[str], rows: List[int]) -> None:
    """The body's sections below the last checklist line, matched to entries by name."""
    from thirties_core.joplin_engine import HEADING_REGEX
    start = (rows[-1] + 1) if rows else 0
    sections: List[BodySection] = []
    cur: Optional[BodySection] = None
    pending_rule = False
    for line in raw_lines[start:]:
        h = HEADING_REGEX.match(line)
        if h:
            cur = BodySection(h.group(1).strip(), [])
            sections.append(cur)
            pending_rule = False
            continue
        if _RULE.match(line):
            pending_rule, cur = True, None
            continue
        if not line.strip():
            continue
        if cur is None and (pending_rule or _GREETING.match(line.strip())):
            cur = BodySection(line.strip(), [line])
            sections.append(cur)
            pending_rule = False
            continue
        if cur is not None:
            cur.lines.append(line)
    reading.sections = sections
    entries = [ln for ln in reading.lines if ln.kind == ENTRY]
    if not sections or not entries:
        return
    matched: Dict[int, BodySection] = {}
    unmatched: List[BodySection] = []
    for sec in sections:
        hit = _match_section(sec.head, entries)
        if hit is None:
            if section_state(sec.head) == ("", "") and any(x.strip() for x in sec.lines):
                unmatched.append(sec)
            continue
        if hit.index not in matched:
            matched[hit.index] = sec
    # a section that names no item is a possible missing item only in a note whose body is written item by item
    # (other sections there match items); a note with a "Notes" heading under its list has nothing missing
    reading.unmatched = unmatched if matched else []
    with_sections = {e.parent for e in entries if e.index in matched}
    for ln in entries:
        sec = matched.get(ln.index)
        if sec is not None:
            ln.progress = section_progress(sec)
        elif ln.parent in with_sections:
            ln.progress = "not_started"  # items beside it have their sections; it has none yet


def _words(text: str) -> set:
    from thirties_core.item_facts import match_words
    return {w.rstrip("s") for w in match_words(text) if len(w) > 1}


def _match_section(head: str, entries: List[Line]) -> Optional[Line]:
    """The one entry a section's heading or greeting names: every distinctive word of its name, or at least two
    and half of them (one shared word, "Book" in "Book the hall", is no match), and more than any other entry."""
    said = _words(head)
    if not said:
        return None
    scored = []
    for ln in entries:
        mine = _words(ln.name)
        if not mine:
            continue
        common = len(mine & said)
        if common and (common == len(mine) or (common >= 2 and 2 * common >= len(mine))):
            scored.append((common == len(mine), common, ln))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None  # two entries fit as well: which one it is can't be told
    return scored[0][2]


# --- the items Thirties keeps ----------------------------------------------------------------------------------

def apply_line(task: Any, ln: Line, reading: NoteReading) -> None:
    """Copy a line's reading onto its TaskItem."""
    task.line_kind = ln.kind
    task.name = ln.name
    task.notes = [dict(n) for n in ln.reading.notes] + [
        {"kind": "condition", "text": c["text"], "met": "yes" if c["met"] else "no"} for c in ln.conditions] + [
        {"kind": "context", "text": c} for c in ln.context]
    task.state = ln.state
    task.blocker = ln.blocker
    task.blocker_last_step = ln.blocker_last_step
    task.path = list(ln.path)
    done, total = reading.counts(ln)
    task.children_done, task.children_total = done, total
    task.progress = ln.progress
    minutes = ln.reading.estimate_minutes
    task.estimate_minutes = minutes
    if minutes:
        task.estimated_thirties = max(1, math.ceil(minutes / 30))


def live(t: Any) -> bool:
    """An entry in a state that's still to do (a container included): not a label, a link, an option, or something
    dropped, closed, on hold, undecided, for later talk, kept for reference or left over."""
    kind = getattr(t, "line_kind", "") or ENTRY
    return kind == ENTRY and getattr(t, "state", "") in OFFERED_STATES


def is_container(t: Any) -> bool:
    """A parent with open entries under it: its children are planned, never the parent."""
    return getattr(t, "children_total", 0) > getattr(t, "children_done", 0)


def offerable(t: Any) -> bool:
    """Whether an item may be offered or planned at all: an entry (not a label, a link or a condition), in a state
    that's still live, and not a container. Items from elsewhere (media, todo notes) are entries."""
    return live(t) and not is_container(t)


def do_now(t: Any) -> bool:
    """Offerable, and nothing it's waiting for holds it back today. A blocker on the last step only (sending)
    leaves the work before it free."""
    return offerable(t) and (not getattr(t, "blocker", "") or bool(getattr(t, "blocker_last_step", False)))


def name_of(t: Any) -> str:
    return str(getattr(t, "name", "") or getattr(t, "title", "") or "").strip()


def match_text(t: Any) -> str:
    """What an item means, for matching what the user says: its parents and its own name."""
    return " ".join([*(getattr(t, "path", None) or []), name_of(t)]).strip()


def progress_count(t: Any) -> str:
    """ "2 of 5 done" for a parent with entries under it; "" otherwise."""
    total = getattr(t, "children_total", 0)
    return f"{getattr(t, 'children_done', 0)} of {total} done" if total else ""


_PROGRESS_WORDS = {"not_started": "not started", "started": "only started", "drafting": "drafted",
                   "ready": "ready"}


def progress_summary(children: Iterable[Any]) -> str:
    """What the note's body says about a group of items: "1 ready, 2 drafted, 1 only started"."""
    counts: Dict[str, int] = {}
    for c in children:
        p = getattr(c, "progress", "")
        if p:
            counts[p] = counts.get(p, 0) + 1
    order = ("ready", "drafting", "started", "not_started")
    return ", ".join(f"{counts[k]} {_PROGRESS_WORDS[k]}" for k in order if counts.get(k))


def prompt_tag(t: Any, tasks: Optional[Iterable[Any]] = None) -> str:
    """How the model is told what an item is, beyond its words: " [on hold]", " [container: 2 of 5 done; plan
    the items under it]", " [blocked: no key yet]", " [notes: estimate 45]". "" for a plain open entry."""
    parts: List[str] = []
    kind = getattr(t, "line_kind", "") or ENTRY
    if kind == LINK:
        parts.append("a link to consider, never a task")
    elif kind in (GROUP, QUESTION):
        parts.append("a question: the items under it are options to decide on" if kind == QUESTION
                     else "a group label, not a task")
    state = getattr(t, "state", "")
    if state and state not in (CURRENT,):
        parts.append(STATE_WORDS.get(state, state))
    if is_container(t) and kind == ENTRY:
        extra = ""
        if tasks is not None:
            kids = [c for c in tasks if getattr(c, "parent_task_id", None) == t.id]
            summary = progress_summary(kids)
            extra = f"; {summary}" if summary else ""
        parts.append(f"container: {progress_count(t)}{extra}; plan the items under it, not this line")
    blocker = getattr(t, "blocker", "")
    if blocker:
        parts.append(f"waiting on: {blocker}" + ("; only the last step waits, the rest can be done now"
                                                 if getattr(t, "blocker_last_step", False) else "; not for now"))
    if getattr(t, "progress", ""):
        parts.append(f"from the note: {_PROGRESS_WORDS.get(t.progress, t.progress)}")
    minutes = getattr(t, "estimate_minutes", 0)
    if minutes:
        parts.append(f"about {minutes} min")
    name = name_of(t)
    if name and name != str(getattr(t, "title", "")).strip() and kind != LINK:
        parts.append(f"name: {name}")
    return f" [{'; '.join(parts)}]" if parts else ""


# --- asked about once -----------------------------------------------------------------------------------------

ONCE_KEY = "list_reading_once"


def _once(db) -> Dict[str, str]:
    try:
        data = json.loads(db.get_setting(ONCE_KEY) or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def once_lines(db, tasks: Iterable[Any], notes: Iterable[Dict[str, Any]], today: Optional[date] = None,
               someday_sections: Sequence[str] = ()) -> List[str]:
    """Things to raise with the user once, ever: open items left under a finished item, and a section of a note's
    body that matches no item on its list (a possible missing item). Each is returned the first time only; after
    that it's remembered as raised (on this device), so it's never brought up again."""
    if db is None:
        return []
    seen = _once(db)
    out: List[str] = []
    fresh: Dict[str, str] = {}
    day = (today or date.today()).isoformat()
    from thirties_core.item_path import label
    tasks = list(tasks)
    for t in tasks:
        if getattr(t, "state", "") != LEFTOVER or getattr(t, "line_kind", ENTRY) != ENTRY:
            continue
        key = f"leftover:{t.id}"
        if key in seen:
            continue
        fresh[key] = day
        out.append(f"Left open under a finished item: {label(t, tasks)} (still wanted?)")
    for note in notes:
        body = note.get("body") or ""
        if not body:
            continue
        reading = read_note(body, someday_sections)
        for sec in reading.unmatched:
            key = f"missing:{note.get('id', '')}:{sec.head.strip().lower()[:60]}"
            if key in seen:
                continue
            fresh[key] = day
            out.append(f"In {note.get('title', 'a list')}, a section with no item on the list: {sec.head.strip()[:80]} "
                       f"(an item to add?)")
    if fresh:
        seen.update(fresh)
        try:
            db.set_setting(ONCE_KEY, json.dumps(seen, sort_keys=True))
        except Exception as e:
            logger.warning("Couldn't remember what was raised: %s", e)
    return out


# --- F: never write an id into a note -------------------------------------------------------------------------

_HEX = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_PREFIXED = re.compile(r"^(?:media|task|item|span|note|id|cal)[:_-]\S*\d\S*$", re.IGNORECASE)
_SAID_ID = re.compile(r"^(?:(?:item|task|span|event|note)[\s_-]?)?id\s*[:=]?\s*[0-9a-f]{6,}$", re.IGNORECASE)
_ID_MARK = re.compile(r"\[\s*id\s*[:=]?\s*[0-9a-f]{6,}\s*\]", re.IGNORECASE)  # "Sweep the porch [ID: 3fa9c2d1]"


def looks_like_id(text: Any) -> bool:
    """Text that is an item id (16 hex characters) or another bare id-like token ("media:gr:123", a uuid,
    "ID: 3fa9c2d1", "id=3fa9c2d1", a calendar event's "cal:..."), or words carrying an item's id marker, not
    words a person wrote."""
    if _ID_MARK.search(str(text or "")):
        return True
    t = str(text or "").strip().strip("'\"`*_[](). ")
    if not t:
        return False
    if _SAID_ID.match(t) or _UUID.match(t) or _PREFIXED.match(t):
        return True
    if _HEX.match(t):
        return any(c.isdigit() for c in t) or len(t) >= 12
    return False


# What each change writes as new text into a note (the rest only looks an item up by its words).
WRITTEN_FIELDS = {"add_task": ("title",), "rename": ("new_title",), "split": ("parts",), "break_down": ("steps",),
                  "create_list": ("title", "items"), "move_many": ("new_title",)}


def refused_text(op: Dict[str, Any]) -> Optional[str]:
    """The text a change would write that is an id rather than words, or None when it's fine."""
    for key in WRITTEN_FIELDS.get(str(op.get("op") or ""), ()):
        value = op.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if isinstance(v, dict):
                v = v.get("title") or v.get("text") or ""
            if isinstance(v, str) and looks_like_id(v):
                return v
    return None


__all__ = ["ENTRY", "CONDITION", "GROUP", "QUESTION", "LINK", "PLACEHOLDER", "CURRENT", "LONG_RUNNING", "ON_HOLD",
           "CLOSED", "DROPPED", "UNDECIDED", "LATER_TALK", "REFERENCE", "LEFTOVER", "OPTION", "OFFERED_STATES",
           "LineReading", "Line", "NoteReading", "BodySection", "read_text", "read_note", "section_state",
           "estimate_minutes", "is_link", "link_site", "is_condition", "apply_line", "live", "is_container", "offerable",
           "do_now", "name_of", "match_text", "progress_count", "progress_summary", "prompt_tag", "once_lines",
           "looks_like_id", "refused_text", "section_progress"]
