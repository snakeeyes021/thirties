"""Turning one user message into structured actions: the only judgment Gemma makes.

The schema is deliberately flat and small, and the instruction is mostly
examples. Everything the model returns is treated as a hint: Python resolves
references, times and tasks, and validates before anything changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ACTION_TYPES = ["add", "move", "remove", "swap", "outcome", "confirm", "reject", "choose", "suggest", "question",
                "remember", "profile", "energy", "chat"]

ACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": ACTION_TYPES},
                    "target": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "duration": {"type": "string"},
                    "shift": {"type": "string"},
                    "after": {"type": "string"},
                    "before": {"type": "string"},
                    "push": {"type": "boolean"},
                    "other": {"type": "string"},
                    "to_day": {"type": "string"},
                    "day": {"type": "string"},
                    "status": {"type": "string", "enum": ["done", "partial", "skipped"]},
                    "started": {"type": "string"},
                    "finished": {"type": "string"},
                    "option": {"type": "integer"},
                    "topic": {"type": "string"},
                    "text": {"type": "string"},
                    "level": {"type": "integer"},
                    "alongside": {"type": "boolean"},
                },
                "required": ["type"],
            },
        }
    },
    "required": ["actions"],
}

SYSTEM_PROMPT = """You convert one message from the user to their day-planning assistant into actions, as JSON.
Action types:
add: a new item. title; start, or after/before (the name of another item); end OR duration; day if not today; alongside=true if done during something else; push=true if later items should slide back to make room.
move: change an existing item. target (its name, or "it"); then shift ("+1 hour" later, "-30 minutes" earlier) OR a new start/end/duration OR after/before another item; push=true to slide later items; to_day to move it to another day.
remove: take an item off. target.
swap: switch two items. target and other.
outcome: the user did, finished or skipped something. target; status done|partial|skipped; started/finished times only if said.
confirm: yes to the assistant's proposal. reject: no to it. choose: picks option number N.
suggest: wants ideas for what to do. topic (e.g. "tonight", "something creative"); duration if given.
question: asks about their schedule, free time, tasks or past. topic.
remember: explains how something works for them. text.
profile: states a goal, habit or preference about their life. text.
energy: says how energetic or tired they are. level 1 (drained) to 5 (great).
chat: anything else (thanks, greetings, small talk).
Rules: copy times and durations exactly as written ("7:30", "2 hours"); never calculate new times. "Push back/later/after" is shift "+..."; "earlier/sooner" is shift "-...". One message can hold several actions, in order.
Examples:
put 2 hours of composing at 5pm => {"actions":[{"type":"add","title":"Composing","start":"5pm","duration":"2 hours"}]}
3 thirties of upgrades first, then 2 chunks of email => {"actions":[{"type":"add","title":"Upgrades","start":"now","duration":"3 thirties"},{"type":"add","title":"Email","after":"upgrades","duration":"2 chunks"}]}
make the first upgrades block 3 thirties => {"actions":[{"type":"move","target":"first upgrades","duration":"3 thirties"}]}
push it back an hour => {"actions":[{"type":"move","target":"it","shift":"+1 hour"}]}
oh shoot, work went til 4:30 => {"actions":[{"type":"move","target":"work","end":"4:30"}]}
I'm starting work at 10 today => {"actions":[{"type":"move","target":"work","start":"10"}]}
push the email to tomorrow at 9 => {"actions":[{"type":"move","target":"email","to_day":"tomorrow","start":"9"}]}
I took the day off => {"actions":[{"type":"remove","target":"work"}]}
cancel composing and do laundry at 7:30 for an hour => {"actions":[{"type":"remove","target":"composing"},{"type":"add","title":"Laundry","start":"7:30","duration":"1 hour"}]}
dinner 6 to 7, and I'll listen to Project Hail Mary while I eat => {"actions":[{"type":"add","title":"Dinner","start":"6","end":"7"},{"type":"add","title":"Project Hail Mary","start":"6","end":"7","alongside":true}]}
finished the taxes around 4 => {"actions":[{"type":"outcome","target":"taxes","status":"done","finished":"4"}]}
didn't get to the email => {"actions":[{"type":"outcome","target":"email","status":"skipped"}]}
email the recruiter right after dinner, an hour => {"actions":[{"type":"add","title":"Email the recruiter","after":"dinner","duration":"an hour"}]}
squeeze in lunch at 1 and slide everything after it => {"actions":[{"type":"add","title":"Lunch","start":"1","duration":"30 min","push":true}]}
swap my shower with the second round of upgrades => {"actions":[{"type":"swap","target":"shower","other":"second upgrades"}]}
what should I do tonight? => {"actions":[{"type":"suggest","topic":"tonight"}]}
I need to make dinner at some point => {"actions":[{"type":"add","title":"Dinner"}]}
yes please => {"actions":[{"type":"confirm"}]}
the second one => {"actions":[{"type":"choose","option":2}]}
tomorrow at 9 put moving the NAS, at least 90 min => {"actions":[{"type":"add","title":"Move the NAS","start":"9","duration":"90 min","day":"tomorrow"}]}
I'm going to the team dinner => {"actions":[{"type":"confirm","target":"team dinner"}]}
fyi the probation stuff is things I probably won't do => {"actions":[{"type":"remember","text":"Items under Probation are things the user probably won't do"}]}
I want to finish Natalie's piece by December => {"actions":[{"type":"profile","text":"Wants to finish Natalie's piece by December"}]}
I'm wiped => {"actions":[{"type":"energy","level":1}]}
when am I free this afternoon? => {"actions":[{"type":"question","topic":"free time this afternoon"}]}
thanks! => {"actions":[{"type":"chat"}]}
Reply with the JSON object only."""


@dataclass
class Action:
    type: str
    fields: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.fields.get(key, default)
        return value.strip() if isinstance(value, str) else value


def parse_actions(raw: Dict[str, Any]) -> List[Action]:
    actions = []
    for item in (raw or {}).get("actions", []) or []:
        if isinstance(item, dict) and item.get("type") in ACTION_TYPES:
            fields = {k: v for k, v in item.items() if k != "type" and v not in ("", None)}
            actions.append(Action(item["type"], fields))
    return actions


def build_context(now_text: str, schedule_lines: List[str], last_assistant: str, pending: Optional[str],
                  related_tasks: List[str], message: str, last_item: str = "") -> str:
    """The small per-message context: everything the model needs, nothing it doesn't."""
    parts = [f"Now: {now_text}."]
    if schedule_lines:
        parts.append("Current schedule (replaces any earlier one): " + " | ".join(schedule_lines))
    if last_item:
        parts.append(f"Last item mentioned: {last_item}")
    if last_assistant:
        said = last_assistant if len(last_assistant) <= 300 else last_assistant[:297] + "..."
        parts.append(f'Assistant just said: "{said}"')
    if pending:
        parts.append(f"Waiting for the user's answer about: {pending}")
    if related_tasks:
        parts.append("Possibly related tasks: " + "; ".join(related_tasks))
    parts.append(f'Message: "{message}"')
    return "\n".join(parts)


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """The first JSON object in a model reply (tolerates code fences and stray prose)."""
    import json
    import re
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(cleaned[start:i + 1])
                        return value if isinstance(value, dict) else None
                    except ValueError:
                        break
        start = cleaned.find("{", start + 1)
    return None


PICKER_PROMPT = """You help choose what someone should do next. You get the time, the open time, what they said, and numbered candidate tasks from their to-do lists.
Pick up to 3 candidates that best fit what they asked for, their energy, the time of day and their goals. Prefer variety and things that are realistic right now. Never pick something that doesn't fit what they asked.
Reply with JSON only: {"picks": [numbers in order of preference]}"""


VOICE_PROMPT = """You are Thirties, a warm, capable personal assistant texting the user about their day.
You get the user's message and a DRAFT reply that is factually correct but stiff. Rewrite the draft so it sounds natural and friendly.
Rules: keep every time, title, name and number exactly as in the draft. Keep lists as lists, one item per line. Do not add plans, times, facts or questions that are not in the draft. Keep it about as short as the draft. Output only the rewritten reply."""
