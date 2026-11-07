"""Helpers that break a turn into small steps for the small model, with Python in between (ROADMAP C3; AGENTS.md
§7: "small models get helpers").

The parse benchmark lost a part of 50% of multi-request messages ("work ran until 4:30, and move budget review to
4:30" kept only the move) when Gemma one-shot the whole message. So a turn runs as:

1. ``split_requests``: Python counts and separates the requests (no model call).
2. Each request is parsed on its own (one small model call each; a one-request message costs what it always did).
3. ``uncovered_clauses``: Python checks that every part of the message that asks for something matched an action.
   A part that didn't is parsed again on its own; if it still yields nothing, the brain asks about that part
   instead of silently dropping it.

Plus the Python readings that need no model: ``merge_alongside`` ("Dinner, cooking and eating, listening to X" is one
item with X alongside, not three items), ``effort_of`` (a rough effort estimate per task, for tired days) and
``sounds_tired``.

Everything here is deterministic and model-free; ``parse_in_steps`` takes the model call as a function, so the parse
benchmark and the model spike run exactly the brain's pipeline.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from thirties_core.assistant_tools import _stem, _words
from thirties_core.brains.slm.parse import Action

# At most this many separately parsed requests per message; anything after the last is parsed with it. Each one is a
# model call (about 0.5 s on the desktop GPU, 2-3 s on the laptop's iGPU), so a run-on message stays bounded.
MAX_REQUESTS = 4

# A self-correction ("no wait", "scratch that") means only the final intent counts: the model reads the message whole,
# since splitting would act on the part the user took back. The coverage check is skipped for the same reason.
CORRECTION_RE = re.compile(
    r"\b(?:no wait|wait no|scratch that|never ?mind|nvm|i mean|i meant|on second thought|or rather|no no|"
    r"actually,? no|actually,? wait|wait,? actually|oh wait|hold on|let me rethink|make (?:it|that) .{1,25}? instead)\b"
    r"|(?<!can )(?<!can't )(?<!cant )(?<!to )(?<!will )(?<!should )(?<!must )(?<!things )(?<!it'll )"
    r"\bwait\b(?=[\s,.!-]+(?:no|actually|sorry|scratch|make|that|let|uh|um|hmm|what|i)\b)", re.I)

_TIME = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight"
CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\b(?:at|by|until|til|till|from|around) \d{1,2}\b|"
                      r"\b\d{1,2}(?::\d{2})?\s*(?:-|–|to|until|till)\s*\d{1,2}\b|\bnoon\b", re.I)
LENGTH_RE = re.compile(r"\b(?:\d+(?:\.\d+)?|an?|one|two|three|four|five|six|half an?)\s*"
                       r"(?:chunks?|thirties|hours?|hrs?|minutes?|mins?)\b", re.I)

# Words a new request starts with ("..., and push dinner back"), after "and" / "also" / "then".
_STARTERS = (
    "add|put|schedule|book|move|push|pull|shift|slide|bump|cancel|remove|delete|drop|clear|swap|switch|make|change|"
    "extend|shorten|start|finish|finished|done|did|didn'?t|skip|skipped|log|mark|note|remember|remind|ping|show|list|"
    "tell|give|throw|squeeze|fit|plan|fill|take|took|block|set|reschedule|postpone|delay|let'?s|lets|can|could|would|"
    "will|should|what|what'?s|whats|when|where|how|how'?s|which|is|are|do|does|i|i'?m|im|i'?ve|ive|i'?d|my|work|we|"
    "yes|yeah|yep|no|nah|sure|ok|okay|option|wrapped|got|need|want|stop|please|fyi|btw")
STARTER_RE = re.compile(rf"^(?:{_STARTERS})\b", re.I)
# What may follow a boundary without being a request of its own: it modifies the request before it.
ATTACH_RE = re.compile(
    r"^(?:(?:i'?ll|we'?ll|i'?m|im|i will)\s+)?(?:listen|listening|watch|watching|while|during|alongside|"
    r"at the same time|with)\b"
    r"|^(?:slide|push|shift|bump|move)\s+(?:everything|the rest|it all|things|all|what'?s)\b"
    r"|^(?:then\s+)?(?:everything|the rest)\s+(?:after|else)\b"
    r"|^(?:for|from|until|til|till|to|at|by|before|after|around|about|so|because|since|but|except|which|that|"
    r"instead|too|as well)\b", re.I)
DIALOG_RE = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|no+|nah|nope|option \d|number \d|the (?:first|second|"
                       r"third) one|sounds good|do it|go ahead)\b", re.I)
NEGATED_RE = re.compile(r"^\s*(?:don'?t|do not|no need|not|never)\b", re.I)

# Things that make a stretch of text a request rather than chit-chat ("hey, thanks for the help earlier!").
CUE_RE = re.compile(
    rf"\b(?:{_STARTERS})\b|\?|\bday off\b|\b(?:tired|exhausted|wiped|drained|beat|fried|energ\w*)\b|"
    r"\b(?:tonight|tomorrow|today|this (?:morning|afternoon|evening)|later|after work|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)\b|"
    r"\b(?:urgent|no rush|can wait|someday|has to|must|always|never)\b", re.I)

_HARD_RE = re.compile(r"(?<=[!?;])\s+|(?<=[a-z0-9)\]'\"])\.+\s+|\s*\n+\s*", re.I)
_SOFT_RE = re.compile(r"\s*,\s*(?:and\s+then|and\s+also|and|also|plus|then)?\s*|\s+(?:and\s+then|and\s+also|and|also|"
                      r"plus|then)\s+", re.I)
_LEAD_RE = re.compile(r"^(?:(?:and|also|plus|then|oh|so|btw|and then|and also)[\s,]+)+", re.I)
_BETWEEN_RE = re.compile(rf"\b(between|from)\s+({_TIME}|\w+)\s+(and|to|-|–)\s+", re.I)


def _norm(text: str) -> str:
    # "4:30 p.m. and ..." must not end a sentence at the dots
    return re.sub(r"\b([ap])\.m\.", r"\1m", text, flags=re.I)


def has_cue(text: str) -> bool:
    return bool(CUE_RE.search(text) or CLOCK_RE.search(text) or LENGTH_RE.search(text))


def _soft_cuts(sentence: str) -> List[Tuple[int, int]]:
    """Where one sentence splits at commas and "and / also / then" because a new request starts: (end of the left
    part, start of the right part)."""
    shielded = _BETWEEN_RE.sub(lambda m: m.group(0)[:m.start(3) - m.start()] + "\0" * len(m.group(3)) +
                               m.group(0)[m.end(3) - m.start():], sentence)  # "between 7 and 9" stays whole
    cuts: List[Tuple[int, int]] = []
    last = 0
    for m in _SOFT_RE.finditer(shielded):
        left, right = sentence[last:m.start()], sentence[m.end():]
        joiner = m.group(0).strip(" ,").lower()
        if not left.strip() or not right.strip() or ATTACH_RE.match(right):
            continue
        if joiner.endswith("then") and not STARTER_RE.match(right):
            # "X, then Y": one request the parser reads well ("upgrades first, then email") unless Y has its own time
            if not CLOCK_RE.search(right.split(",")[0]):
                continue
        elif not STARTER_RE.match(right):
            continue
        cuts.append((m.start(), m.end()))
        last = m.end()
    return cuts


def _standalone(piece: str) -> bool:
    dialog = DIALOG_RE.match(piece)
    return bool((has_cue(piece) or dialog) and len(piece.split()) >= (1 if dialog else 2))


def split_requests(message: str, max_requests: int = MAX_REQUESTS) -> List[str]:
    """The separate requests in a message, in the user's words. One element when it's one request, when it's short,
    or when the user corrected themselves on the way."""
    text = _norm(message.strip())
    if len(text.split()) < 4 or CORRECTION_RE.search(text):
        return [message.strip()]
    cuts: List[Tuple[int, int]] = []  # (end of a piece, start of the next), in the text
    pos = 0
    for m in list(_HARD_RE.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        sentence = text[pos:end]
        cuts += [(pos + a, pos + b) for a, b in _soft_cuts(sentence)]
        if m:
            cuts.append((m.start() + (1 if text[m.start()] == "." else 0), m.end()))
            pos = m.end()

    def pieces_of(cuts):
        starts = [0] + [b for _, b in cuts]
        ends = [a for a, _ in cuts] + [len(text)]
        return [text[s:e] for s, e in zip(starts, ends)]

    # A fragment with nothing to act on belongs with its neighbour; so does chit-chat in front ("hey, thanks!").
    changed = True
    while changed and cuts:
        changed = False
        pieces = pieces_of(cuts)
        for i, p in enumerate(pieces):
            if i > 0 and not _standalone(_LEAD_RE.sub("", p.strip(" ,"))):
                del cuts[i - 1]
                changed = True
                break
            if i == 0 and len(pieces) > 1 and not _standalone(p.strip(" ,")):
                del cuts[0]
                changed = True
                break
    while len(cuts) > max_requests - 1:
        cuts.pop()
    pieces = [_LEAD_RE.sub("", p.strip(" ,")).strip(" ,") for p in pieces_of(cuts)]
    return [p for p in pieces if p] or [message.strip()]


# --- did the actions cover the message? ---

# Words that say nothing about *what* the request is about: the coverage check looks past them.
_NOT_CONTENT = set("""
add put schedule book move push pull shift slide bump cancel remove delete drop clear swap switch make change extend
shorten start started starting finish finished done did didn't skip skipped log mark note remember remind ping show
list tell give throw squeeze fit plan fill take took block set reschedule postpone delay let's lets can could would
will should what what's whats when where how how's which is are do does i i'm im i've ive i'd my we yes yeah yep no
nah sure ok okay option wrapped got need want stop please fyi btw also and then plus but so just really actually maybe
like gonna wanna kinda pretty quick quickly some any thing things stuff back earlier later sooner forward up down
out off over in on at to for from by with of the a an it that this them those these there here me you your
am pm today tonight tomorrow yesterday morning afternoon evening night now minute minutes min mins hour hours hr hrs
chunk chunks thirty thirties half time times day days week until til till before after around about again more
another lot bit little have has had be been being was were get going go went doing ran run running look looks
right left rest everything all else free open available hey hi hello thanks thank ugh hmm lol haha oh well yet
still too as instead other one two three four five six seven eight nine ten first second third last next
eh ah um uh that's thats it's its not nope okay cool great good awesome sorry
""".split())

_TYPE_CUES: Dict[str, re.Pattern] = {
    "energy": re.compile(r"\b(tired|exhausted|wiped|drained|beat|fried|energ\w*|great|awesome|good|level \d|"
                         r"sleepy|rested|wired)\b", re.I),
    "confirm": DIALOG_RE, "reject": DIALOG_RE, "choose": DIALOG_RE, "accept": DIALOG_RE,
    "question": re.compile(r"\?|^(what|when|where|how|which|do i|is there|am i)\b", re.I),
    "suggest": re.compile(r"\b(ideas?|suggest\w*|what should|you pick|pick|fill|up to you)\b|\?", re.I),
    "remove": re.compile(r"\bday off\b|\b(cancel|remove|drop|clear)\b", re.I),
    "urgency": re.compile(r"\b(urgent|rush|can wait|someday|priority|pressing|asap|by \w+)\b", re.I),
    "fact": re.compile(r"\b(has to|must|always|never|after|before|can'?t move|stays? put|fixed|daylight)\b", re.I),
    "routine": re.compile(r"\broutine\b", re.I),
    "today": re.compile(r"\btoday'?s?\b|\blist\b", re.I),
    "horizon": re.compile(r"\b(week|month|year|someday|plan|mon|tues|wed|thurs|fri|sat|sun)\w*\b", re.I),
    "times": re.compile(r"\b(woke|wake|up|bed|asleep|slept|work|clocked|shift)\b", re.I),
}


def content_stems(text: str) -> Set[str]:
    return {_stem(w) for w in _words(text) if w not in _NOT_CONTENT and not w.isdigit() and not re.fullmatch(
        r"\d+(?::\d+)?(?:am|pm)?", w)}


def action_stems(actions: Iterable[Action]) -> Set[str]:
    out: Set[str] = set()
    for a in actions:
        for key in ("title", "target", "other", "after", "before", "text", "topic", "list", "items"):
            value = a.fields.get(key)
            if isinstance(value, list):
                value = " ".join(str(v.get("title", v)) if isinstance(v, dict) else str(v) for v in value)
            if value:
                out |= {_stem(w) for w in _words(str(value))}
    return out


def clause_covered(clause: str, actions: Sequence[Action]) -> bool:
    """Some action is about this part of the message: it shares a content word, or the part asks for a kind of
    action that's there (a yes, an energy level, a question)."""
    real = [a for a in actions if a.type != "chat"]
    if not real:
        return False
    words = content_stems(clause)
    if words & action_stems(real):
        return True
    for a in real:
        cue = _TYPE_CUES.get(a.type)
        if cue is not None and cue.search(clause):
            return True
    return not words and bool(real)  # "push it back an hour": nothing to name, and something was done


def uncovered_clauses(message: str, actions: Sequence[Action], handled: Callable[[str], bool] = lambda c: False
                      ) -> List[str]:
    """Parts of the message that ask for something and matched no action. ``handled`` marks parts Python acts on
    itself (reminders, lists, "you pick"), which the parse needn't cover."""
    if CORRECTION_RE.search(_norm(message)):
        return []
    out = []
    for clause in split_requests(message, max_requests=99):
        if NEGATED_RE.match(clause) or not has_cue(clause) or handled(clause):
            continue
        if not content_stems(clause) and not DIALOG_RE.match(clause):
            continue  # nothing to name: can't tell, so don't ask
        if not clause_covered(clause, actions):
            out.append(clause)
    return out


def parse_in_steps(message: str, parse_one: Callable[[str], List[Action]],
                   handled: Callable[[str], bool] = lambda c: False, split: bool = True
                   ) -> Tuple[List[Action], List[str], List[str]]:
    """The whole parse of one message: separate the requests, parse each, then retry each part nothing covered.
    Returns (actions, parts still not understood, the requests as parsed)."""
    requests = split_requests(message) if split else [message]
    actions: List[Action] = []
    for request in requests:
        got = parse_one(request)
        actions += [a for a in got if not any(_same(a, b) for b in actions)]
    unclear: List[str] = []
    if not any(a.type != "chat" for a in actions):
        return actions, unclear, requests  # all small talk: nothing was asked for, so nothing was missed
    for clause in uncovered_clauses(message, actions, handled)[:2]:
        got = [a for a in parse_one(clause) if a.type != "chat"]
        if got and clause_covered(clause, got):
            actions += [a for a in got if not any(_same(a, b) for b in actions)]
        else:
            unclear.append(clause)
    if any(a.type != "chat" for a in actions):
        actions = [a for a in actions if a.type != "chat"] or actions
    return actions, unclear, requests


def _same(a: Action, b: Action) -> bool:
    return a.type == b.type and a.fields == b.fields


# --- "Dinner, cooking and eating, listening to X": one item, with X alongside ---

MEAL_RE = re.compile(r"\b(breakfast|brunch|lunch|dinner|supper)\b", re.I)
_DESCRIBES = {"cook", "cooking", "eat", "eating", "make", "making", "prep", "prepping", "prepare", "preparing", "and",
              "the", "it", "food", "meal", "cleanup", "clean", "up", "dishes", "doing", "then", "plus"}
LISTEN_RE = re.compile(r"^(?:while\s+)?(?:listen(?:ing)?\s+to|watch(?:ing)?|with)\s+(.+)$", re.I)
_PLACE_KEYS = ("start", "end", "duration", "day", "after", "before")


def _describes(title: str, main: str) -> bool:
    """A title that only describes the main item ("cooking and eating" for Dinner)."""
    words = set(re.findall(r"[a-z']+", title.lower())) - set(re.findall(r"[a-z']+", main.lower()))
    return bool(MEAL_RE.search(main)) and bool(re.findall(r"[a-z']+", title.lower())) and words <= _DESCRIBES


def merge_alongside(actions: List[Action], message: str) -> List[Action]:
    """Fold descriptions into their item and put what's listened to or watched alongside it."""
    out: List[Action] = []
    for a in actions:  # a single add titled "Dinner, cooking and eating, listening to X": split it first
        title = str(a.fields.get("title") or "")
        if a.type == "add" and "," in title:
            parts = [p.strip() for p in title.split(",") if p.strip()]
            main = parts[0]
            extra = [p for p in parts[1:] if LISTEN_RE.match(p) or _describes(p, main)]
            if extra:
                out.append(Action("add", {**a.fields, "title": main}))
                out += [Action("add", {"title": p}) for p in extra if LISTEN_RE.match(p)]
                continue
        out.append(a)
    adds = [a for a in out if a.type == "add"]
    if len(adds) < 2:
        return out
    main = next((a for a in adds if MEAL_RE.search(str(a.fields.get("title") or ""))), None) or \
        next((a for a in adds if any(a.fields.get(k) for k in _PLACE_KEYS)), adds[0])
    main_title = str(main.fields.get("title") or "")
    lowered = message.lower()
    kept: List[Action] = []
    for a in out:
        title = str(a.fields.get("title") or "")
        if a.type != "add" or a is main:
            kept.append(a)
            continue
        if _describes(title, main_title):
            for k in _PLACE_KEYS:  # its time is the meal's time
                if a.fields.get(k) and not main.fields.get(k):
                    main.fields[k] = a.fields[k]
            continue
        m = LISTEN_RE.match(title)
        named = title.lower()
        listened = m is not None or bool(named) and bool(re.search(
            rf"\b(?:listen(?:ing)? to|watch(?:ing)?|while (?:listening to|watching)|with)\s+(?:the\s+)?{re.escape(named)}",
            lowered))
        if listened:
            fields = dict(a.fields)
            if m is not None:
                fields["title"] = m.group(1).strip()
            fields["alongside"] = True
            if not any(fields.get(k) for k in ("start", "after", "before")):
                for k in _PLACE_KEYS:
                    if main.fields.get(k):
                        fields[k] = main.fields[k]
            a = Action("add", fields)
        kept.append(a)
    return kept


# --- effort, for tired days ---

EASY_RE = re.compile(r"\b(call|text|message|email|reply|respond|pay|order|buy|water|feed|tidy|trash|dishes|laundry|read|"
                     r"watch|listen|check|look up|sign|send|print|charge|pick up|return|renew|unsubscribe|rsvp|book a|"
                     r"stretch|walk|nap|rest|shower|podcast|audiobook|journal|meditat)\w*", re.I)
HARD_RE = re.compile(r"\b(write|fix|build|design|research|organi[sz]e|deep clean|clean (?:the|out)|garage|taxes|compos|"
                     r"draft|refactor|dev\b|develop|learn|study|repair|install|set up|setup|pack|apply|prepare|"
                     r"presentation|report|proposal|migrat|declutter|assemble|workout|gym|run\b|project)\w*", re.I)


def effort_of(title: str, estimated_thirties: int = 0, deferred_count: int = 0, is_checkbox_item: bool = True,
              is_substep: bool = False, is_media: bool = False) -> int:
    """A rough effort estimate: 1 easy, 2 medium, 3 hard. Python's guess from the wording and what's known of the
    item (its estimated length, how often it was put off); no model call. The obvious cases get it right, which is
    all a tired day needs ("propose one low-effort plan")."""
    if is_media:
        return 1
    score = 2
    if HARD_RE.search(title):
        score += 1
    if EASY_RE.search(title):
        score -= 1
    if estimated_thirties >= 3 or not is_checkbox_item:
        score += 1  # long, or a whole note (a project) rather than one action
    if is_substep:
        score -= 1  # broken down on purpose into small steps
    if deferred_count >= 3:
        score += 1  # put off again and again: it's heavier than it looks
    return max(1, min(3, score))


def task_effort(task) -> int:
    return effort_of(task.title, getattr(task, "estimated_thirties", 0) or 0, getattr(task, "deferred_count", 0) or 0,
                     getattr(task, "is_checkbox_item", True), bool(getattr(task, "parent_task_id", None)),
                     "media" in (getattr(task, "tags", None) or []) or str(getattr(task, "id", "")).startswith("media:"))


TIRED_RE = re.compile(r"\b(tired|exhausted|wiped|drained|fried|beat|burn(?:ed|t) out|low energy|no energy|zonked|"
                      r"knackered|sleepy|brain ?dead|can'?t think|decision fatigue|overwhelmed|running on empty|"
                      r"too much to decide)\b", re.I)


def sounds_tired(message: str, latest_energy: Optional[int] = None) -> bool:
    return bool(TIRED_RE.search(message or "")) or (latest_energy is not None and latest_energy <= 2)
