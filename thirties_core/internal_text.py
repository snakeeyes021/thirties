"""What the model is fed never reaches the user, whatever the model writes.

The prompt hands the model each tool's result wrapped as "(Result of show_schedule, for the assistant only: …)"
(inference._format_prompt), and Python's checks come to it as "[System check, not from the user] …". A model can
copy either back into its reply, or write a call out as text ("update_event {...}", "CALL add_event {...}"). The
brains ask for a fresh reply when they see that, but a rewrite asked for by another check can bring it back, so
this is the last word, in Python, on every reply:

- an echoed result goes whole: from "(Result of" to the bracket that closes it, across lines and paragraphs; one
  cut off before its closing bracket goes to the end of the reply (nothing after it was meant for the user);
- a line with a check, a refusal ("NOT_DONE"), "for the assistant only", or a call written out goes;
- a sentence about the app's own workings goes: CALL lines, tool or function calls, commands it can't run, what
  "the system" returned ("I can't make that change with a command in plain text"). The user never sees those
  workings, so a sentence about them only puzzles them. The brains send such a reply back once for plain words
  (``talks_internals``); what's still left is taken out here.

(An id standing in for a name is messages.names_not_ids' job: it puts the name in its place.)

What's left is the reply the user should read; it can be empty, and the caller decides what to say then.
"""

from __future__ import annotations

import re

# where an echoed tool result starts
_RESULT_RE = re.compile(r"\(\s*Result of\b", re.IGNORECASE)
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


def talks_internals(text: str) -> bool:
    """Whether a sentence of the text talks about the app's own workings (see the module notes)."""
    return bool(text) and bool(_TALK_RE.search(text))


def has_internal(text: str) -> bool:
    """Whether the text holds anything only the model was meant to see."""
    if not text:
        return False
    return bool(_RESULT_RE.search(text) or any(_LINE_RE.search(line) for line in text.splitlines())
                or _TALK_RE.search(text))


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
    out = _without_results(text)
    out = "\n".join(_without_talk(line.rstrip()) for line in out.splitlines() if not _LINE_RE.search(line))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


__all__ = ["has_internal", "strip_internal", "talks_internals"]
