"""How the assistant talks, checked by Python before a reply is sent (the Frontier brain calls these with its own
checks of what the reply claims, brains/frontier.py ``_moves_made``).

Each check reads the reply (and the user's words, and what the tools did this turn) and either fixes the reply
outright, where the fix is a fact Python has, or names the problem for one rewrite by the model:

- **The time now** is said as it is: "it's 7:30 right now" at 7:44 becomes "it's 7:44 right now" (fixed outright).
- **No made-up limits**: the assistant never says it's "text only" or can't see or change what it can (the user's
  plan, lists, notes or calendar).
- **No made-up explanations**: asked why something on the plan is as it is, the model is told with the message
  where the item's record is (why_notes.py); that's a note for its one answer, not a rewrite.
- **Its questions are its own**: whether to ask the user which time, or whether they'd like a change, is the
  model's judgement, and such a question is never sent back for a rewrite. Told to pick ("you pick", "up to
  you"), the model gets a note with the message that the choice is left to it (``choice_note``).
- **Its notes are its own**: it never talks about its workings (its tools, system, internal lists or dates), as if
  the user could see them; what it keeps is "my notes".
- **The day's end is no reason to drop things**: when something doesn't fit, it says where it goes, never offers to
  drop it, unless the user brought dropping up.
- **The moon** is as computed (sky.py), never guessed.

A reply that still has a made-up limit or talk of the workings after its rewrite loses those sentences. Every cut
here takes whole sentences, and a sentence introducing a list takes the list with it (clean_prose.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, List, Optional

# "it's 8:30 right now", "right now it's 8:30", "it is currently 8:30 AM"
_NOW_TIME_RE = re.compile(r"\b((?:it'?s|it is|the time is)\s+(?:now\s+|currently\s+)?(?:about\s+|around\s+|just\s+|nearly\s+"
                          r"|almost\s+)?)(\d{1,2}):(\d{2})(\s*[ap]\.?m\.?)?", re.IGNORECASE)
_NOW_WORD_RE = re.compile(r"\b(?:right now|now|currently|at the moment)\b", re.IGNORECASE)

LIMIT_RE = re.compile(
    r"\btext[- ]only\b|\btext mode\b|\bonly (?:operate|work|run) in text\b|"
    r"\bI(?:'m| am) (?:just |only )?(?:a |an )?(?:text-based|language model|AI model|chatbot)\b|"
    r"\bI (?:can'?t|cannot|am unable to|'m unable to|don'?t have (?:the )?(?:ability|access) to)\s+"
    r"(?:see|access|read|open|view|look at|check)\s+(?:your|the)\s+(?:plan|schedule|lists?|notes?|joplin|calendar|"
    r"agenda|day|to-?do)\b|"
    r"\bI (?:can'?t|cannot|am unable to|'m unable to)\s+(?:change|edit|update|move|add to)\s+(?:your|the)\s+"
    r"(?:plan|schedule|lists?|notes?|agenda|day|to-?do)\b", re.IGNORECASE)

WORKINGS_RE = re.compile(
    r"\bunder the hood\b|\bmy (?:internal|system|planning session|back ?end|tools?|database|prompt|instructions|"
    r"code|programming)\b|\binternal (?:state|planning|lists?|notes|system|date|anchor|records?)\b|"
    r"\b(?:the|my) (?:system|back ?end|database|prompt|open pool|item facts|tool call|tool result|display tool|"
    r"schedule (?:display )?tool)\b|\banchor(?:ed)? (?:date|to)\b|\bmemory notes?\b|\bprofile section\b|"
    r"\b(?:the )?\w+_\w+ (?:tool|function)\b", re.IGNORECASE)

WHY_RE = re.compile(r"\bwhy\b|\bhow come\b|\bwhat happened (?:to|with)\b|\bwho (?:moved|removed|marked|changed|put|"
                    r"took)\b|\bwhere did\b.*\bgo\b", re.IGNORECASE)

DELEGATE_RE = re.compile(r"\byou (?:pick|choose|decide|sort it|figure it out)\b|\byour (?:call|job|choice)\b|"
                         r"\bup to you\b|\bwhatever(?:'s| is) best\b|\bjust (?:pick|put|do|schedule|plan|choose|decide|"
                         r"slot|fit|make)\b|\bpick (?:one|the best|for me)\b|\bfigure it out\b|\bsort (?:it|the|out)\b|"
                         r"\bdon'?t ask\b|\bstop asking\b|\bno more questions\b|\bbest one\b|"
                         r"\bset (?:it|that|them|the \w+) up\b", re.IGNORECASE)

DROP_OFFER_RE = re.compile(r"\b(?:drop|let (?:it|them|that|those) go|skip (?:it|them|that|those)|remove (?:it|them|"
                           r"that|those)|take (?:it|them|that|those) off|cut (?:it|them|that|those)|leave (?:it|them) "
                           r"off)\b", re.IGNORECASE)
FIT_RE = re.compile(r"\b(?:doesn'?t fit|don'?t fit|won'?t fit|wouldn'?t fit|no room|not enough (?:time|room)|out of "
                    r"time|too much|(?:day|evening|afternoon|today) is (?:full|packed)|squeez\w*|crowded|packed|"
                    r"overbooked|run(?:ning)? out|before (?:bed|bedtime|sleep)|no (?:free )?time left)\b", re.IGNORECASE)
USER_DROP_RE = re.compile(r"\b(?:drop|skip|cancel|remove|delete|let \w+ go|don'?t need|not doing|forget|scrap|ditch|"
                          r"nix|take \w+ off)\w*", re.IGNORECASE)

MOON_RE = re.compile(r"\bmoon\b", re.IGNORECASE)
_MOON_WORDS = {"waxing": "waxing", "waning": "waning", "full moon": "full moon", "new moon": "new moon",
               "first quarter": "first quarter", "last quarter": "last quarter", "third quarter": "last quarter",
               "crescent": "crescent", "gibbous": "gibbous"}

# a promise about what Thirties will or won't do from now on ("I'll stop sending the lists", "I won't ping you about
# it again", "no more check-ins", "I'll stay quiet until then")
PROMISE_RE = re.compile(
    r"\b(?:i'?ll|i will|i'?m going to)\s+(?:stop|quit|no longer|stay (?:quiet|silent)|keep (?:quiet|it short))\b|"
    r"\b(?:i )?won'?t (?:\w+ ){0,2}(?:send|show|ping|remind|ask|message|mention|bug|nag|list|include|add|text|"
    r"check in|say|bother)\w*\b[^.!?\n]*\b(?:again|anymore|any more|from now on|going forward|until)\b|"
    r"\bno more (?:\w+ ){0,2}(?:lists?|pings?|reminders?|check-?ins?|messages?|notifications?|questions?|updates?|"
    r"block (?:lines?|talk)|next lines?)\b|\b(?:from now on|going forward),? i'?ll\b|"
    r"\bi'?ll (?:only|just) (?:send|show|ping|give|tell)\b", re.IGNORECASE)
# what Python adds to replies (what's next, Not marked yet, the block line): a promise that it stops or changes holds
# only after set_reply_extras, never with another call; writing less never removes them
_PART = r"(?:block(?: talk| mention| line)?s?|lists?|next (?:section|lines?|list)|what'?s next|not marked yet|status line)"
# a part's name as a reply writes it: in quotes or not ('the "Not marked yet" section', the “what's next” lines)
_Q = "[\"'“”‘’]?"
_NAMED = rf"{_Q}{_PART}{_Q}"
_WORDS = rf"(?:{_Q}\w+{_Q} ){{0,2}}"
EXTRAS_RE = re.compile(
    rf"\bno (?:more )?{_NAMED}"
    rf"|\b(?:won'?t|will not|i'?ll stop|stop(?:ped)?|not going to|no longer) (?:\w+ ){{0,2}}(?:show|send|includ|add|"
    rf"mention|list|say|print)\w*(?: \w+){{0,4}} {_NAMED}"
    rf"|\b(?:dropped|removed|deleted|cut|took out|turned off|switched off|hid|hidden|muted|turned (?:it |them )?back "
    rf"on|turned on|switched (?:back )?on|brought back|restored) {_WORDS}{_NAMED}"
    rf"|\b(?:the |your )?{_NAMED}(?: (?:section|lines?|list|part))? (?:is|are) (?:now )?(?:off|hidden|gone|back on|"
    rf"back|on again)\b",
    re.IGNORECASE)
# the calls that record any other such promise, so Python keeps it
RECORDING_TOOLS = ("set_reply_extras", "mute_checkins", "unmute_checkins", "remember", "update_profile",
                   "set_sun_notices", "set_reminder", "check_on", "set_week", "set_anchor", "skip_routine")

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> List[str]:
    return [s for s in _SENTENCE_RE.split(text or "") if s.strip()]


# --- fixed outright ---

def fix_now_time(reply: str, now: Optional[datetime]) -> str:
    """The time now, said as it is (10-26: the current minute was rounded to its block). Only a time said as now's."""
    if not reply or now is None:
        return reply
    from thirties_core.clock import format_clock

    def fix(m: re.Match, sentence: str) -> str:
        h, mm = int(m.group(2)), int(m.group(3))
        recent = [now - timedelta(minutes=k) for k in range(0, 11)]
        if any((t.hour % 12 or 12) == (h % 12 or 12) and t.minute == mm for t in recent):
            return m.group(0)
        said = format_clock(now)
        if not m.group(4):
            said = re.sub(r"\s*[AP]M$", "", said)
        return re.sub(r"(?:about|around|just|nearly|almost)\s+$", "", m.group(1)) + said

    out = []
    for piece in re.split(r"((?<=[.!?])\s+|\n+)", reply):
        if _NOW_WORD_RE.search(piece):
            piece = _NOW_TIME_RE.sub(lambda m: fix(m, piece), piece)
        out.append(piece)
    return "".join(out)


# --- for one rewrite by the model ---

def _user_words(brain: Any, turn_start: int) -> str:
    said = getattr(brain.tools, "user_message", None)
    if said:
        return said
    users = [m for m in brain.messages[:turn_start] if m.get("role") == "user"
             and not str(m.get("content") or "").startswith(brain.CHECK_PREFIX)]
    return str(users[-1].get("content") or "") if users else ""


def _called(brain: Any, turn_start: int, name: str) -> bool:
    return any(m.get("role") == "tool" and m.get("name") == name for m in brain.messages[turn_start:])


def _named_in(text: str, brain: Any) -> List[str]:
    from thirties_core.item_facts import mentions
    plan = getattr(brain.tools, "day_plan", None)
    if plan is None:
        return []
    titles = [brain.tools._title(s) for s in plan.spans if not s.is_envelope]
    return [t for t in titles if mentions(text, t, titles)]


def moon_wrong(reply: str, now: Optional[datetime]) -> str:
    """The phase the reply gives for the moon, when it isn't the computed one: '' when right or not said."""
    if now is None or not MOON_RE.search(reply or ""):
        return ""
    from thirties_core.sky import moon_phase, waxing
    name, _ = moon_phase(now)
    true = {name, "waxing" if waxing(now) else "waning"} | {w for w in ("crescent", "gibbous") if w in name}
    for sentence in _sentences(reply):
        if not MOON_RE.search(sentence) or sentence.rstrip().endswith("?"):
            continue
        low = sentence.lower()
        said = {v for k, v in _MOON_WORDS.items() if k in low}
        if said and not said <= true:
            return name + (" (waxing)" if waxing(now) else " (waning)")
    return ""


# a reply talking about an earlier message or attempt of its own
CORRECTION_RE = re.compile(
    r"\b(?:I need to|I have to|let me|I should|I must|I want to|I'd like to)\s+correct\b|"
    r"^\W*(?:one |a quick |quick |small |a small )?correction\b|\bcorrect(?:ing)? (?:my|what I said|myself)\b|"
    r"\bmy (?:last|earlier|previous|first) (?:message|reply|answer|note)\b|\b(?:earlier|before),? I (?:said|told you|"
    r"wrote)\b|\bI (?:said|told you|wrote) (?:earlier|before)\b|\bmy first (?:attempt|try|go)\b|\bthe first time\b|"
    r"\b(?:hadn'?t|wasn'?t) actually\b[^.!?\n]*\bthen\b|\bto be clear about (?:my|what I)\b", re.IGNORECASE)
# an opening that only announces the correction ("I need to correct my last message:", "One correction:")
_OPENER_RE = re.compile(r"^\W*(?:(?:I need to|I have to|let me|I should|I must|I want to|I'd like to)\s+correct\b[^:.!\n]*"
                        r"|(?:one |a quick |quick |small |a small )?correction(?: to my (?:last|earlier|previous) "
                        r"(?:message|reply))?)\s*[:.!,–-]\s*", re.IGNORECASE)


def _turn_had_drafts(brain: Any, turn_start: int) -> bool:
    """This turn had something the user never saw: a send-back, an earlier draft, or a refused call."""
    from thirties_core.plan_tools import NOT_DONE
    for m in brain.messages[turn_start:]:
        if brain._is_check(m):
            return True
        if m.get("role") == "tool" and str(m.get("content") or "").startswith(NOT_DONE):
            return True
    drafts = [m for m in brain.messages[turn_start:] if m.get("role") == "assistant" and str(m.get("content") or "").strip()]
    return len(drafts) > 1


def _last_seen(brain: Any, turn_start: int) -> str:
    """The last assistant message before this turn that the user saw."""
    for m in reversed(brain.messages[:turn_start]):
        if m.get("role") == "assistant" and not m.get("hidden") and str(m.get("content") or "").strip():
            return str(m.get("content") or "")
    return ""


def unseen_corrections(brain: Any, turn_start: int, reply: str) -> List[str]:
    """The reply's sentences that correct a message of its own the user never saw: a draft or attempt of this turn,
    or anything the last message the user saw didn't say. A real correction of what the user read is left alone."""
    found = [s for s in _sentences(reply) if CORRECTION_RE.search(s)]
    if not found:
        return []
    if _turn_had_drafts(brain, turn_start):
        return found
    seen = _last_seen(brain, turn_start)
    return [s for s in found if not seen or not (set(_named_in(s, brain)) & set(_named_in(seen, brain)))]


def strip_corrections(reply: str) -> str:
    """Cut the correction openings ("I need to correct my last message:") and the sentences about the unseen draft
    or attempt; what's true now stays. Whole sentences go, with the list one introduces; an opening that only
    announced a list leaves it (and a caller checks the result still reads whole: clean_prose.introduced)."""
    from thirties_core.clean_prose import edit

    def new_for(s: str) -> Optional[str]:
        rest = _OPENER_RE.sub("", s, count=1)
        if rest != s:
            return "" if not rest.strip() or CORRECTION_RE.search(rest) else rest
        return "" if CORRECTION_RE.search(s) else None

    def opener_only(s: str) -> bool:
        return bool(_OPENER_RE.match(s)) and not _OPENER_RE.sub("", s, count=1).strip()
    out, _ = edit(reply or "", new_for, keeps_list=opener_only)
    return out or reply


def _refused(brain: Any, turn_start: int, name: str) -> bool:
    """The call was made this turn and refused, with no later one of it going through: nothing changed."""
    from thirties_core.plan_tools import NOT_DONE
    outs = [str(m.get("content") or "") for m in brain.messages[turn_start:]
            if m.get("role") == "tool" and m.get("name") == name]
    return bool(outs) and all(o.startswith(NOT_DONE) for o in outs)


def unkept_promises(brain: Any, turn_start: int, reply: str) -> List[str]:
    """Sentences promising what Thirties will or won't do from now on that no call this turn recorded: one about what
    Python adds to replies needs set_reply_extras; any other needs a call that records it (a mute, a note). The
    model writing less never changes what Python sends, so a promise with no call behind it is a false claim."""
    from thirties_core.plan_tools import NOT_DONE
    went = {m.get("name") for m in brain.messages[turn_start:] if m.get("role") == "tool"
            and not str(m.get("content") or "").startswith(NOT_DONE)}
    out = []
    for s in _sentences(reply):
        if s.rstrip().endswith("?"):
            continue
        if EXTRAS_RE.search(s):
            if "set_reply_extras" not in went:
                out.append(s.strip())
        elif PROMISE_RE.search(s) and not (went & set(RECORDING_TOOLS)):
            out.append(s.strip())
    return out


def promise_checks(promised: List[str], brain: Any = None, turn_start: int = 0) -> List[str]:
    """What the rewrite is told about each kind of unkept promise (with the turn, a refused call is named as one)."""
    out = []
    if any(EXTRAS_RE.search(s) for s in promised):
        if brain is not None and _refused(brain, turn_start, "set_reply_extras"):
            out.append("something Python adds to replies (what's next, Not marked yet, or the block line) changed, "
                       "but set_reply_extras was refused this turn, so nothing changed: leave that claim out, and "
                       "answer only what the user asked")
        else:
            out.append("something Python adds to replies (what's next, Not marked yet, or the block line) will stop "
                       "or change, but set_reply_extras wasn't called this turn, so it won't: call set_reply_extras "
                       "now, or leave the promise out")
    if any(not EXTRAS_RE.search(s) for s in promised):
        out.append("later replies or messages will change, but no call recorded it, so nothing will change: make "
                   f"the call ({promise_tools()}) or leave the promise out")
    return out


def promise_tools() -> str:
    """The recording calls this build has, for the rewrite's instruction."""
    from thirties_core.plan_tools import TOOL_SPECS
    have = {spec["name"] for spec in TOOL_SPECS}
    named = [t for t in ("set_reply_extras", "mute_checkins", "remember") if t in have]
    return ", ".join(named) or "the tool that records it"


# said when a reply was nothing but a claim that didn't happen
NOTHING_CHANGED = "I couldn't change that just now."


def drop_sentences(reply: str, sentences: List[str]) -> str:
    """The reply without these sentences (a promise Python can't keep), lines kept; a reply that was nothing else
    says plainly that nothing changed, never the claim. Whole sentences go, with the list one introduces."""
    from thirties_core.clean_prose import cut
    gone = {s.strip() for s in sentences if s and s.strip()}
    out = cut(reply or "", lambda s: s.strip() in gone)
    return out or NOTHING_CHANGED


def problems(brain: Any, turn_start: int, reply: str, now: Optional[datetime] = None) -> List[str]:
    """What's wrong with how the reply talks, each as an instruction for its rewrite."""
    out: List[str] = []
    said = _user_words(brain, turn_start)
    lines = _sentences(reply)
    if any(LIMIT_RE.search(s) for s in lines):
        out.append("it says you can't do or see something you can: you see the user's plan, lists, notes and "
                   "calendar and change them with your tools, so never say otherwise")
    if any(WORKINGS_RE.search(s) for s in lines):
        out.append("it talks about how Thirties works inside: say what you keep as your own notes (\"I've noted…\", "
                   "\"I'll remember…\") and leave the workings out")
    # asked why about an item: the model hears where its record is with the message, never a rewrite (why_notes.py)
    # a question back to the user (which time, whether they'd like a change) is the model's to ask (choice_note)
    if said is not None and not USER_DROP_RE.search(said or ""):
        if any(DROP_OFFER_RE.search(s) and FIT_RE.search(s) for s in lines):
            out.append("it offers to drop something because today is full; the day's end is no reason to drop "
                       "anything: say where it goes instead (the next free time, or tomorrow)")
    from thirties_core import morning_day
    if morning_day.is_morning_turn(brain, turn_start):
        missing = morning_day.left_out(brain, reply)
        if missing:
            out.append("it's the morning's message and leaves part of the day out (" + ", ".join(missing[:4])
                       + "): list the whole day as Python lists it, each routine part with its steps and the meals, "
                       "then what you put on")
    # --- what it says about the leftovers from before: the plan's counts and places (leftover_facts.py) ---
    from thirties_core import leftover_facts
    out += leftover_facts.problems(brain, turn_start, reply)
    # --- end leftovers ---
    moon = moon_wrong(reply, now)
    if moon:
        out.append(f"the moon tonight is a {moon}, as computed; say that, or nothing about the moon")
    return out


def choice_note(said: Optional[str]) -> Optional[str]:
    """When the user leaves a choice to Thirties ("you pick", "up to you", "don't ask"), a note for the model with
    their message; None otherwise. A note, never a rewrite: whether to ask anyway is the model's judgement."""
    if not said or not DELEGATE_RE.search(said):
        return None
    return ("(The user's message leaves the choice to you: pick, make the change, and say in a line what you chose "
            "and why.)")


def morning_fill(brain: Any, turn_start: int, reply: str) -> str:
    """A morning reply that still omits routine steps or anchors gets Python's lines for the day under it. One that
    still miscounts the leftovers gets Python's notice in place of those sentences (leftover_facts.py)."""
    from thirties_core.leftover_facts import set_straight
    reply = set_straight(brain, turn_start, reply)
    from thirties_core import morning_day
    if not morning_day.is_morning_turn(brain, turn_start) or not morning_day.left_out(brain, reply):
        return reply
    lines = morning_day.missing_line(brain.tools.assistant)
    return (reply.rstrip() + "\n\n" + lines) if lines else reply


def last_resort(reply: str) -> str:
    """After its rewrite, a reply that still claims a made-up limit or talks about the workings loses those
    sentences (never the whole reply), and a list one introduces with it."""
    from thirties_core.clean_prose import cut
    out = cut(reply or "", lambda s: bool(LIMIT_RE.search(s) or WORKINGS_RE.search(s)))
    return out or reply


__all__ = ["choice_note", "fix_now_time", "problems", "last_resort", "morning_fill", "unkept_promises", "promise_checks", "unseen_corrections", "strip_corrections", "drop_sentences", "EXTRAS_RE", "moon_wrong", "LIMIT_RE", "WORKINGS_RE", "DELEGATE_RE"]
