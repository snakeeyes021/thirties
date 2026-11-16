"""What goes into the profile and the memory notes: what the user asked to keep, decided by Python from their words.

The profile and the notes are shown to the model in every conversation, so a line in them shapes every day after.
A preference guessed from one passing remark ("off to grab a cinnamon bun now" saved as "likes cinnamon buns"), or
a one-day arrangement saved as a lasting rule, is a change the user never asked for. So remember and
update_profile go through only when the user's message this turn:

- asks for it: "remember…", "keep in mind…", "note that…", "from now on…", "going forward…", "fyi…", "just so
  you know…"; or
- says how things are for them for good: "I always…", "I never…", "I usually…", "every Tuesday…", "on
  weekdays…", "I prefer…", "my goal is…", "I want to … by spring", "anything under Maybe is…".

A sentence about today only ("now", "today", "tonight", "this morning") says nothing lasting, unless it asks
outright. Anything else is refused with a NOT_DONE the model can leave out of its reply. A call with no message
from the user (the app's own pages) is the user's own doing and goes through.
"""

from __future__ import annotations

import re
from typing import Optional

# the user asks for it to be kept
ASK_RE = re.compile(
    r"\b(?:remember|keep in mind|bear in mind|make a note|note (?:that|this|down)|take note|don'?t forget|"
    r"for (?:future )?reference|for next time|for the future|in (?:the )?future|from now on|going forward|"
    r"for the record|fyi|just so you know|for your info(?:rmation)?|(?:save|write|jot) (?:it|that|this) down|"
    r"save (?:it|that|this)|add (?:it|that|this) to (?:my|the) profile)\b", re.IGNORECASE)
# the user says how things are for them, for good
LASTING_RE = re.compile(
    r"\b(?:i|we)\s+(?:always|never|usually|normally|generally|typically|mostly|often|rarely|tend to|prefer|"
    r"like to|love to|hate|can'?t stand|don'?t like|do not like|like my|like it when|need my|work|start work|"
    r"get up|wake up|go to bed|commute|live)\b|"
    r"\bevery (?:day|morning|afternoon|evening|night|week|weekday|weekend|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|time)\b|"
    r"\b(?:on|most) (?:weekdays|weekends|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|days|"
    r"mornings|afternoons|evenings|nights)\b|"
    r"\bmy (?:goal|goals|aim|resolution)\b|\bi(?:'m| am) (?:trying|working|training) (?:to|towards|for)\b|"
    r"\b(?:i want|i'd like|i hope|i plan|i'm planning|i mean) to\b[^.!?\n]*\b(?:by|this (?:year|month|summer|"
    r"fall|autumn|winter|spring)|next (?:year|month)|someday|one day|eventually|in \d+ (?:weeks|months|years))\b|"
    r"\b(?:anything|everything|stuff|things|items?) (?:under|in|on) [\w' ]{1,30}\b(?:is|are|means?)\b|"
    r"\b(?:is|are) (?:just )?(?:stuff|things|items|ideas|maybes?)\b|\bmeans\b",
    re.IGNORECASE)
# a sentence about today only
TODAY_RE = re.compile(r"\b(?:today|tonight|right now|now|this (?:morning|afternoon|evening)|at the moment|"
                      r"currently|just now)\b", re.IGNORECASE)
_SENTENCES_RE = re.compile(r"(?<=[.!?])\s+|\n+")

REFUSAL = ("The user's message doesn't ask you to remember anything or say how things always are for them, so "
           "nothing was saved. Save only what they ask you to keep or state as lasting; leave this out of your "
           "reply.")


def asked_to_keep(message: Optional[str]) -> bool:
    """Whether the user's message asks for something to be kept, or says how things are for them for good."""
    if message is None:
        return True
    for sentence in _SENTENCES_RE.split(message):
        if ASK_RE.search(sentence):
            return True
        if LASTING_RE.search(sentence) and not TODAY_RE.search(sentence):
            return True
    return False


def refusal(message: Optional[str]) -> str:
    """The refusal for a save the user didn't ask for; '' when it may go ahead."""
    return "" if asked_to_keep(message) else REFUSAL


__all__ = ["asked_to_keep", "refusal", "REFUSAL"]
