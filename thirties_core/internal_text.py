"""What the model is fed never reaches the user, whatever the model writes.

The prompt hands the model each tool's result wrapped as "(Result of show_schedule, for the assistant only: …)"
(inference._format_prompt), and Python's checks come to it as "[System check, not from the user] …". A model can
copy either back into its reply, or write a call out as text ("update_event {...}", "CALL add_event {...}"). The
brains ask for a fresh reply when they see that, but a rewrite asked for by another check can bring it back, so
this is the last word, in Python, on every reply:

- an echoed result goes whole: from "(Result of" to the bracket that closes it, across lines and paragraphs; one
  cut off before its closing bracket goes to the end of the reply (nothing after it was meant for the user). A note
  Python adds beside the user's message ("(The user's message asks why about …)", ``NOTE_OPENER``) goes the same
  way;
- a line with a check, a refusal ("NOT_DONE"), "for the assistant only", or a call written out goes;
- a sentence about the app's own workings goes: CALL lines, tool or function calls, commands it can't run, what
  "the system" returned ("I can't make that change with a command in plain text"). The user never sees those
  workings, so a sentence about them only puzzles them. The brains send such a reply back once for plain words
  (``talks_internals``); what's still left is taken out here;
- the mark on when an item under way began ("(recorded; show the block, not the minute)") goes, leaving the
  words around it;
- the leftovers' tally the model checks its words against ("kept N from before; N of them on today's plan; …")
  becomes the one plain sentence the user may hear: how many things wait in Before today.

(An id standing in for a name is messages.names_not_ids' job: it puts the name in its place.)

What's left is the reply the user should read; it can be empty, and the caller decides what to say then.
"""

from __future__ import annotations

import re

# how every note Python adds for the model beside the user's message opens (why_notes, on_site, engineer_notes,
# the ask for quiet): copied into a reply, it goes whole, like an echoed result
NOTE_OPENER = "(The user's message"
# where an echoed tool result, or such a note, starts
_RESULT_RE = re.compile(r"\(\s*(?:Result of|The user['’]s message)\b", re.IGNORECASE)
# a line that's for the model only: a check, a refusal, the prompt's own wording, a call written out
_LINE_RE = re.compile(
    r"for the assistant only|\[System check\b|\bNOT_DONE\b|\bResult of [a-z]+_[a-z_]+\b|"
    r"^\W*`*\s*(?-i:CALL)\s+[A-Za-z_]\w*|"  # CALL add_event {...}, never "Call the plumber"
    r"^\W*[a-z]+_[a-z_]+\s*(?:\(\s*)?\{",  # update_event {...}, show_schedule({...})
    re.IGNORECASE)


# a sentence about the app's own workings: its call lines, tools, commands, functions, what the system returned
# ("Call the gallery", "the clay tools", "a command of the wheel" are the user's words and stay)
_TALK_RE = re.compile(
    r"(?-i:\bCALLs?\b)\s+(?:commands?|lines?|syntax|statements?|format|directives?|blocks?|text)\b|"
    r"\b(?:using|with|via|through|writing|issuing|making|output(?:ting)?|any|these|those)\s+(?-i:CALLs?\b)"
    r"(?!\s+(?:[A-Z][a-z]|me\b|you\b|them\b|us\b))|"
    r"\b(?:tool|function)[ -]?calls?\b|"
    r"\b(?:commands?|calls?|directives?) (?:written )?in (?:plain )?text\b|"
    r"\b(?:can(?:no|')?t|unable to|not able to|couldn'?t|won'?t be able to|am not able to)\s+(?:process|execute|run|"
    r"issue|use|make|send|parse|call)\s+(?:\w+\s+){0,3}?(?:commands?|calls?|functions?|directives?)\b|"
    r"\bmy (?:tools?|functions?|commands?|tool ?set)\b|"
    r"\b(?:the |available |scheduling |planning )(?:tools?|functions?) (?:I have|available to me|let me|allow me|"
    r"don'?t|do not|aren'?t|can'?t|won'?t|failed|returned|said|reported|rejected)\b|"
    r"\b(?:system|tool|function) (?:error|result|output|response)s?\b|"
    r"\bthe system (?:returned|said|reported|rejected|refused|didn'?t|won'?t|blocked)\b|"
    r"\b[a-z]+_[a-z_]+ (?:tool|function|call|command)s?\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# --- the mark on when an item under way began, for the model only (under_way.RECORDED) ---
# "began 10:50 AM (recorded; show the block, not the minute)": copied into a reply, the mark goes; the minute
# beside it is said by its block (under_way.unsaid, clock.to_thirties).
_RECORDED_RE = re.compile(r"\s*\(\s*recorded;\s*show the block,\s*not the minute\s*\)", re.IGNORECASE)
# --- end the mark ---

# --- the leftovers' tally for the model never reaches the user (leftover_facts.summary) ---
# The model is handed "kept N from before; N of them on today's plan; N not on today's plan; N proposed for
# dropping" to check its words against. Copied into a reply it reads as bookkeeping, so the sentence holding it
# becomes the one plain sentence the user may hear: how many wait in Before today (or nothing, when none do).
# With three kept off the plan and two proposed drops, that sentence is "5 things from before are waiting in Before
# today." (tests/test_no_tally_leak.py has whole examples).
_N = r"(\d+|no|none|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_TODAYS = r"today['’]s (?:plan|schedule)"
_TALLY_RE = re.compile(
    rf"\bkept\s+{_N}\s+from before\s*;\s*{_N}\s+of them on {_TODAYS}"
    rf"|\b{_N}\s+of them on {_TODAYS}(?:\s*\([^()]*\))?\s*;\s*{_N}\s+not on {_TODAYS}"
    rf"|\b{_N}\s+not on {_TODAYS}(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*;\s*{_N}\s+proposed for dropping", re.IGNORECASE)
_KEPT_RE = re.compile(rf"\bkept\s+{_N}\s+from before", re.IGNORECASE)
_ON_RE = re.compile(rf"\b{_N}\s+of them on {_TODAYS}", re.IGNORECASE)
_OFF_RE = re.compile(rf"\b{_N}\s+not on {_TODAYS}", re.IGNORECASE)
_DROPS_RE = re.compile(rf"\b{_N}\s+proposed for dropping", re.IGNORECASE)
# a sentence beside it that only points at Before today ("All the details are in Before today.")
_POINTER_RE = re.compile(r"^\W*(?:all|the|everything|details?|it['’]?s|it is|they['’]re|they are|more|see)\b"
                         r"[^.!?\d]{0,50}\bBefore today\W*$", re.IGNORECASE)
_WORDS = {"no": 0, "none": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def waiting_sentence(n: int) -> str:
    """The one plain sentence on the leftovers: how many wait in Before today ("" when none)."""
    if n <= 0:
        return ""
    return f"{n} thing{'s' if n != 1 else ''} from before {'are' if n != 1 else 'is'} waiting in Before today."


def _count(rx: "re.Pattern", text: str):
    m = rx.search(text)
    if m is None:
        return None
    word = m.group(1).lower()
    return _WORDS[word] if word in _WORDS else int(word)


def _tally_said_plainly(sentence: str) -> str:
    """A sentence holding the tally, as the one plain sentence its own counts give."""
    off, drops = _count(_OFF_RE, sentence), _count(_DROPS_RE, sentence) or 0
    if off is None:
        kept, on = _count(_KEPT_RE, sentence), _count(_ON_RE, sentence)
        off = max(0, kept - on) if kept is not None and on is not None else 0
    return waiting_sentence(off + drops)


def _without_tally(line: str) -> str:
    """The line with each sentence holding the tally said plainly, and the pointers beside it gone."""
    if not _TALLY_RE.search(line):
        return line
    parts = _SENTENCE_RE.split(line)
    out = [_tally_said_plainly(s) if _TALLY_RE.search(s) else s for s in parts]
    out = [s for s, was in zip(out, parts) if s and not (not _TALLY_RE.search(was) and _POINTER_RE.match(s))]
    return " ".join(out).rstrip()

# --- end the leftovers' tally ---


def talks_internals(text: str) -> bool:
    """Whether a sentence of the text talks about the app's own workings (see the module notes)."""
    return bool(text) and bool(_TALK_RE.search(text))


def has_internal(text: str) -> bool:
    """Whether the text holds anything only the model was meant to see."""
    if not text:
        return False
    return bool(_RESULT_RE.search(text) or any(_LINE_RE.search(line) for line in text.splitlines())
                or _TALK_RE.search(text) or _TALLY_RE.search(text) or _RECORDED_RE.search(text))


def _without_talk(line: str) -> str:
    """The line without its sentences about the app's workings."""
    if not _TALK_RE.search(line):
        return line
    return " ".join(s for s in _SENTENCE_RE.split(line) if not _TALK_RE.search(s)).rstrip()


def _without_results(text: str) -> str:
    """Each echoed "(Result of …)" taken out up to its closing bracket; one never closed, to the end."""
    out, pos = [], 0
    while True:
        m = _RESULT_RE.search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        depth, i = 0, m.start()
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        pos = i + 1
        if pos >= len(text):
            break
    return "".join(out)


def strip_internal(text: str) -> str:
    """The reply without anything only the model was meant to see (see the module notes)."""
    if not text or not has_internal(text):
        return text
    out = _RECORDED_RE.sub("", _without_results(text))
    out = "\n".join(_without_talk(_without_tally(line.rstrip())) for line in out.splitlines()
                    if not _LINE_RE.search(line))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


__all__ = ["NOTE_OPENER", "has_internal", "strip_internal", "talks_internals", "waiting_sentence"]
