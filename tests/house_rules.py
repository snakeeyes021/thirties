"""House rules: how Thirties works by design, never a friction (AGENTS.md section 7, "Natural units"; ROADMAP 0.1).

A simulated person is told these up front: they may say once that they would prefer otherwise, but none of it is a
fault and it costs no trust or patience. A judge is told a complaint against one of them is no friction. Rules with a
check in code are also enforced after judging: a friction (or a false claim, an issue) whose words only complain
against the rule is dropped and counted. One that also names something else wrong, or the rule's own real failure (a
time the person gave that was changed, a day they named that wasn't kept, a done they reported that wasn't recorded),
stays.

Any harness that judges these runs should use this list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

_UNSAID = r"(?:(?!n't\b|\bnot\b|\bnever\b|\bbut\b|\binstead\b)[^.;])"  # a clause's words with no negation in them
_THEIRS = r"(?:(?!n't\b|\bnot\b|\bnever\b|\bbut\b|\bshow|\bline\b|\bblock\b|\bread)[^.;])"  # nor what's shown


def _rx(*parts: str) -> "re.Pattern[str]":
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


# Something else wrong in the same friction: it stays, whatever else it says.
_ELSE_WRONG = _rx(r"\bcan(?:'|no)?t\b", r"\bwon'?t (?:finish|fit|work)\b", r"\b(?:not |un)workable\b", r"\boverlap",
                  r"\bconflict", r"\bclash", r"\bduplicat", r"\btwice\b", r"\bmissing\b", r"\blost\b",
                  r"\bwrong (?:item|day|thing|list)\b", r"\balready (?:past|passed|gone|over)\b", r"\bin the past\b",
                  r"\btoo late\b", r"\bdeadline\b", r"\bduring (?:work|sleep)\b", r"\bbroke", r"\bno clear answer\b",
                  r"\bwithout a clear\b", r"\binternal\b", r"\bnagg")

# Blocks, not clocks
_CLOCK = re.compile(r"(?<![\d:$])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_SHOWN = _rx(r"\b(?:show|shows|shown|showing|display\w*|list(?:s|ed|ing)?|line|row|reads?|reading|says|label\w*|"
             r"names?|named|round\w*|block|instead of|rather than)\b")
_WANTS_MINUTES = _rx(
    r"\b(?:no|not|never|without)\b[^.;]{0,25}\b(?:exact|precise|specific|actual)\s+(?:minutes?|times?|start)",
    r"n't (?:show|give|list|say|name)\w*[^.;]{0,25}\b(?:exact|precise|specific|actual)\s+(?:minutes?|times?|start)",
    r"\b(?:exact|precise|specific|actual)\s+(?:minutes?|start times?|times?)\b[^.;]{0,30}"
    r"\b(?:not (?:shown|given|listed)|missing|left out|rounded|hidden)",
    r"\bminute[- ](?:precise|level|exact|accurate|by[- ]minute)\b", r"\bto the minute\b",
    r"\brounded\b[^.;]{0,25}\b(?:block|half[- ]hours?|thirty|thirties)\b",
    r"\b(?:only|just)\s+(?:the|its|a)\s+(?:block|half[- ]hour)\b",
    r"\bwant(?:s|ed)?\b[^.;]{0,25}\b(?:minutes|exact|precise)\b")
# a time the person or a calendar gave for the plan, a block that doesn't hold the item, or minutes the plan doesn't
# have: a real friction
_GIVEN_TIME = _rx(
    r"\bcalendar\b", r"\bappointment\b", r"\bbooked\b", r"\b(?:wrong|another|different|other|earlier|later) block\b",
    r"\boutside (?:the|its) block\b", r"\bdoesn't (?:hold|contain)\b", r"\b(?:asked|wanted|requested) for\b",
    r"\b(?:requested|gave (?:a |the |its )?(?:time|start)|given time|stated time|insist\w*)\b",
    r"\b(?:person|user|they|i) (?:said|says|gave|asked|wanted|wants|set|named|picked|chose)\b"
    r"(?!\s+(?:again\s+)?at\s+\d)" + _THEIRS + r"{0,25}?\b\d{1,2}:\d\d",  # their time, not when they said it
    r"\byour (?:own )?(?:exact )?(?:times?|start)\b",
    r"\b(?:person|user)'?s (?:own )?(?:time|start|\d)", r"\bper (?:the )?(?:person|user)\b",
    r"\b(?:plan|state)\b[^.;]{0,20}\b(?:stores?|stored|has|had|keeps?|says)\b[^.;]{0,15}\b\d{1,2}:[03]0\b")
# a start the app recorded or took as now ("started at 10:20", "start right now at 10:35"), not a time for the plan
_STARTED_AT = re.compile(r"(?:\b(?:started|began|begun|since|under ?way (?:from|since))\s+(?:[a-z]+\s+){0,2}?(?:at\s+)?"
                         r"|\bnow,? at\s+)$", re.IGNORECASE)


def _started_in_a_shown_block(text: str) -> bool:
    """A half hour's start is named, and so is a start inside that half hour that the app recorded (a 10:00 shown for
    something begun at 10:20)."""
    found = [(m.start(), int(m.group(1)) % 12, int(m.group(2))) for m in _CLOCK.finditer(text)]
    starts = {(h, m) for _, h, m in found if m in (0, 30)}
    return any(m % 30 and (h, m - m % 30) in starts and _STARTED_AT.search(text[max(0, at - 30):at])
               for at, h, m in found)


def _blocks_complaint(evidence: str, quote: str) -> Optional[str]:
    """Wanting minutes where the app shows blocks. Returns the friction's other words, or None."""
    if _WANTS_MINUTES.search(evidence):
        return _WANTS_MINUTES.sub(" ", evidence)
    if _SHOWN.search(evidence) and _started_in_a_shown_block(f"{quote} ; {evidence}"):
        return _CLOCK.sub(" ", evidence)
    return None


# Skip means not today
_NEXT_DAY = r"(?:tomorrow|the\s+next\s+day)"
_DAYS = "(?:" + "|".join((_NEXT_DAY, r"(?:mon|tues|wednes|thurs|fri|satur|sun)day", r"next\s+week",
                          r"the\s+weekend")) + ")"
_SKIP = _rx(r"\b(?:skip\w*|not\s+today)\b[^.;]{0,80}\b" + _NEXT_DAY + r"\b",
            r"\b" + _NEXT_DAY + r"\b[^.;]{0,80}\bskip\w*\b")
# the person named the day, or the app moved it to a day nobody named: a real friction
_DAY_NAMED = _rx(r"\b(?:asked|told it|said|wanted|named|requested)\b" + _UNSAID + r"{0,40}?\b(?:to|for|on|until|till)\s+"
                 + _DAYS + r"\b",
                 r"(?<!n't )(?<!not )(?<!never )(?<!be )\b(?:moved|put|pushed|placed|added|rescheduled)\b[^.;]{0,30}"
                 r"\b(?:to|on|onto|for|into)\s+" + _DAYS + r"\b")


def _skip_complaint(evidence: str, quote: str) -> Optional[str]:
    """Wanting a skip to mean tomorrow."""
    return _SKIP.sub(" ", evidence) if _SKIP.search(evidence) else None


# Done only on the person's words
_NOT_MARKED = r"(?:n't|\bnot\b|\bnever\b)\s+(?:\w+\s+){0,2}(?:mark|record|tick|log|count)\w*\b"
_GUESS = r"\b(?:assum\w*|infer\w*|presum\w*|should have (?:known|guessed|figured|realised|realized))"
_DONE_UNSAID = _rx(_NOT_MARKED + r"[^.;]{0,80}" + _GUESS, _GUESS + r"[^.;]{0,80}" + _NOT_MARKED,
                   r"\b(?:started|begun|began|under ?way|in progress|was doing|is doing|doing now)\b[^.;]{0,60}"
                   + _NOT_MARKED + r"[^.;]{0,15}\b(?:as )?(?:done|finished|complete)")
_DONE_SAID = _rx(r"(?<!n't )(?<!not )(?<!never )\b(?:reported|said|told it|confirmed|typed|wrote)\b" + _UNSAID
                 + r"{0,40}?\b(?:done|finished|over|complete)\b")


def _done_complaint(evidence: str, quote: str) -> Optional[str]:
    """Wanting something marked done that the person never said was done."""
    return _DONE_UNSAID.sub(" ", evidence) if _DONE_UNSAID.search(evidence) else None


@dataclass(frozen=True)
class HouseRule:
    name: str
    person: str   # told to the simulated person, in an app arm
    judge: str    # told to the judge
    complaint: Optional[Callable[[str, str], Optional[str]]] = None  # (evidence, quote) -> its other words, or None;
    real: Optional["re.Pattern[str]"] = None                         # none: told only, no check in code
    # real: what makes it a real friction after all (read in the evidence and the quote)


HOUSE_RULES: Tuple[HouseRule, ...] = (
    HouseRule(
        "blocks_not_clocks",
        "It shows your day in half-hour blocks: something you began at 10:20 shows in the 10:00 block, and only "
        "times you or your calendar set for something are shown to the minute (a time you set yourself stays "
        "exactly as given).",
        "Blocks, not clocks. Replies and the Agenda show the day in half-hour blocks, while the plan keeps exact "
        "minutes underneath. Anything the app placed or recorded itself, and anything under way, is named by the "
        "block it is in (\"10:00 – Glazing\" for something begun at 10:20), even when it began partway through. "
        "Only times the person or a calendar gave for an item's plan are shown to the minute. A block that contains "
        "the plan's time is the same time: never a false claim, bad_time, unclear_reply or request_ignored on that "
        "account. The person wanting minutes shown, asking for a line to read their exact start, or being unsure "
        "a block holds their time is no friction. Still a friction: a block that doesn't contain the item's time, "
        "or a time the person or a calendar gave for the plan shown or stored as another time.",
        _blocks_complaint, _GIVEN_TIME),
    HouseRule(
        "skip_means_not_today",
        "Skipping something takes it off today; it comes back another day this week, and goes to tomorrow only if "
        "you say tomorrow.",
        "Skip means not today. A skipped item leaves today: a to-do comes back on a later day this week that the "
        "app picks, and a skipped meal, routine step or appointment simply ends. It goes to tomorrow only when the "
        "person's words name tomorrow (any day they name is kept). A skipped item not put on tomorrow is no "
        "friction; moving it to a day the person didn't name is an unasked_change, and a day they named that "
        "isn't kept is still a friction.",
        _skip_complaint, _DAY_NAMED),
    HouseRule(
        "done_on_their_word",
        "It marks done only what you tell it is done; work in progress counts as done once you report it finished.",
        "Done only on the person's word. Only the items the person says are done are recorded done; nothing is "
        "taken for granted: half-finished work is logged finished solely when they report it so, and one never "
        "marked on an earlier day stays unknown. Not marking something the person never said was done "
        "is no friction; a done they did report that isn't recorded still is.",
        _done_complaint, _DONE_SAID),
    HouseRule(
        "routines_on_their_own",
        "Your routines go on the plan by themselves each morning, in their usual order.",
        "Routines on their own. Routines and daily anchors go on the plan by themselves each morning, at their "
        "usual times and with their parts in their usual order, without asking. That is no unasked_change; a "
        "routine put over something else, twice, or against what the person asked still is a friction."),
    HouseRule(
        "changes_are_live",
        "What you ask for happens at once, with nothing to confirm or save; obvious details get chosen for you, "
        "and named.",
        "Changes are live. What the person asks for takes effect at once, with no step to confirm, save or lock in "
        "a plan; the day's proposal for free time is told as a notice, not a question; obvious details (a time, a "
        "length, which list) are picked and said, and the person can change them. No confirm step, or a plain "
        "pick said aloud, is no friction; a pick that breaks something (a bad_time, a stated constraint) still is."),
)

PERSON_HOUSE_NOTE = ("How the app works, by design (none of this is a mistake: you may say once that you'd prefer "
                     "otherwise, but it costs it no trust or patience):\n"
                     + "\n".join(f"- {r.person}" for r in HOUSE_RULES))
JUDGE_HOUSE_RULES = "\n".join(f"- {r.judge}" for r in HOUSE_RULES)


def house_rule_of(entry: Dict[str, Any]) -> Optional[str]:
    """The house rule a friction (or a claim judged false) only complains against, or None. Its evidence (a claim's
    "why") is what's read; its quote (a claim's own words) only for the times it names."""
    evidence = " ".join(str(entry.get(k) or "") for k in ("evidence", "note", "why")).strip()
    quote = " ".join(str(entry.get(k) or "") for k in ("quote", "claim"))
    if not evidence:
        return None
    for rule in HOUSE_RULES:
        rest = rule.complaint(evidence, quote) if rule.complaint else None
        if rest is None or (rule.real is not None and rule.real.search(f"{evidence} {quote}")) \
                or _ELSE_WRONG.search(rest):
            continue
        return rule.name
    return None


def by_house_rules(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """The entries that aren't only a complaint against a house rule, and how many were dropped, by rule."""
    kept, dropped = [], {}
    for e in entries:
        rule = house_rule_of(e)
        if rule is None:
            kept.append(e)
        else:
            dropped[rule] = dropped.get(rule, 0) + 1
    return kept, dict(sorted(dropped.items()))


def with_house_rules(persona: str) -> str:
    """A simulated person's persona, with the app's design told up front."""
    return f"{persona}\n{PERSON_HOUSE_NOTE}"


JUDGE_HOUSE_SECTION = ("House rules: how the app works by design, correct by definition. A complaint against one of "
                       "them (the user's or yours) is no fault and costs no score; only what a rule names as still "
                       "wrong counts:\n" + JUDGE_HOUSE_RULES)


def drop_house_rule_text(items: List[Any]) -> Tuple[List[Any], int]:
    """A judge's plain list of problems (issues, worst moments) without the ones that only complain against a house
    rule, and how many were dropped."""
    kept = [i for i in items if not (isinstance(i, str) and house_rule_of({"evidence": i}))]
    return kept, len(items) - len(kept)
