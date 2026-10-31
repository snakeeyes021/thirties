"""Turning one user message into structured actions: the only judgment Gemma makes.

The schema is deliberately flat and small, and the instruction is mostly
examples. Everything the model returns is treated as a hint: Python resolves
references, times and tasks, and validates before anything changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ACTION_TYPES = ["add", "move", "remove", "swap", "outcome", "confirm", "reject", "choose", "suggest", "question",
                "remember", "profile", "energy", "chat", "today", "urgency", "fact", "routine", "horizon", "times"]
URGENCIES = ["now", "soon", "whenever", "someday"]

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
                    "urgency": {"type": "string", "enum": URGENCIES},
                    "fixed": {"type": "boolean"},
                    "daylight": {"type": "boolean"},
                    "when": {"type": "string"},
                    "woke": {"type": "string"},
                    "slept": {"type": "string"},
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
today: put something on today's list, with no time. title.
urgency: says how pressing something on their lists is. target; urgency now|soon|whenever|someday; day if it's due by a day.
fact: a rule an item must keep. target; after (an item it must come after); start (it can't start before); fixed=true if it can't move; daylight=true if it needs daylight.
routine: put one of their routines on the plan. title (e.g. "Morning routine"); start if given.
horizon: when something on their lists belongs or is planned, beyond today; or planning or seeing a week, month or year. target (the item); when ("this year", "someday", "Thursday", "next week"); topic "plan" or "show" for a whole period.
times: says when they actually woke, went to bed, or started or finished work. woke; slept; started (work); finished (work; "now" if just done).
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
took the day off! piano 10 to 12, then lunch with Sam at 12:30 for an hour => {"actions":[{"type":"remove","target":"work"},{"type":"add","title":"Piano","start":"10","end":"12"},{"type":"add","title":"Lunch with Sam","start":"12:30","duration":"an hour"}]}
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
show me the whole day as a clean list => {"actions":[{"type":"question","topic":"schedule"}]}
what's on tomorrow? => {"actions":[{"type":"question","topic":"schedule","day":"tomorrow"}]}
put my morning routine on => {"actions":[{"type":"routine","title":"Morning routine"}]}
add the bank call to today's list => {"actions":[{"type":"today","title":"Bank call"}]}
that's not urgent => {"actions":[{"type":"urgency","target":"that","urgency":"whenever"}]}
the passport renewal has to happen by friday => {"actions":[{"type":"urgency","target":"passport renewal","day":"friday"}]}
the walk has to wait until after the shower => {"actions":[{"type":"fact","target":"walk","after":"shower"}]}
the cats' dinner is at 5 and can't move => {"actions":[{"type":"fact","target":"cats' dinner","fixed":true}]}
that's a this-year thing => {"actions":[{"type":"horizon","target":"that","when":"this year"}]}
put the taxes on Thursday => {"actions":[{"type":"horizon","target":"taxes","when":"Thursday"}]}
plan my week => {"actions":[{"type":"horizon","topic":"plan","when":"this week"}]}
what's on this week? => {"actions":[{"type":"horizon","topic":"show","when":"this week"}]}
I woke up at 7 and started work at 7:15 => {"actions":[{"type":"times","woke":"7","started":"7:15"}]}
done with work => {"actions":[{"type":"times","finished":"now"}]}
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
        said = last_assistant if len(last_assistant) <= 160 else last_assistant[:157] + "..."
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


BREAKDOWN_PROMPT = """You help someone start a task they keep putting off by breaking it into small steps.
You get the task and where it's from. Write 3 to 6 short, concrete steps in order, each doable in 30 minutes or less, starting with the easiest first action. Use the person's own words where you can; no explanations.
Reply with JSON only: {"steps": ["...", "..."]}"""

BREAKDOWN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["steps"],
                    "properties": {"steps": {"type": "array", "minItems": 2, "maxItems": 6,
                                             "items": {"type": "string"}}}}

