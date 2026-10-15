"""Parse accuracy of the SLM brain's one model call, on hand-labelled utterances.

Fast feedback for instruction/example changes: each case is one user message
with a little context, and the expected action types plus a few key fields.
Runs the real Gemma parser session (KV cache reused across cases).

    ~/.local/share/thirties/venv/bin/python -m tests.slm_parse_eval
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.brains.slm.parse import SYSTEM_PROMPT, build_context, extract_json, parse_actions

SCHEDULE = ["Work 8:30 AM-4:00 PM", "Dinner 6:00 PM-7:00 PM", "Composing 7:00 PM-8:30 PM", "Sleep 11:00 PM-7:00 AM"]

# (message, expected types in order, {index: {field: substring}}, extra context)
CASES: List[Tuple[str, List[str], Dict[int, Dict[str, str]], Dict[str, Any]]] = [
    ("put 2 hours of reading at 9", ["add"], {0: {"duration": "2", "start": "9"}}, {}),
    ("gym 5:30 to 6:30", ["add"], {0: {"start": "5:30", "end": "6:30"}}, {}),
    ("3 chunks of laundry after dinner", ["add"], {0: {"after": "dinner", "duration": "3"}}, {}),
    ("half an hour of tidying at 9:30", ["add"], {0: {"start": "9:30"}}, {}),
    ("tomorrow at 10 call the ENT office for 30 min", ["add"], {0: {"day": "tomorrow", "start": "10"}}, {}),
    ("monday morning at 9, an hour of taxes", ["add"], {0: {"day": "monday", "start": "9"}}, {}),
    ("I need to call mom at some point", ["add"], {0: {"title": "mom"}}, {}),
    ("podcast during dinner", ["add"], {0: {"alongside": "True"}}, {}),
    ("squeeze lunch in at 12:30 and push everything after it", ["add"], {0: {"push": "True", "start": "12:30"}}, {}),
    ("push composing back an hour", ["move"], {0: {"target": "compos", "shift": "+"}}, {}),
    ("move dinner 30 minutes earlier", ["move"], {0: {"target": "dinner", "shift": "-"}}, {}),
    ("work went til 4:45", ["move"], {0: {"target": "work", "end": "4:45"}}, {}),
    ("starting work at 10 today", ["move"], {0: {"target": "work", "start": "10"}}, {}),
    ("make composing 2 hours", ["move"], {0: {"target": "compos", "duration": "2"}}, {}),
    ("move composing to after the movie", ["move"], {0: {"target": "compos", "after": "movie"}}, {}),
    ("push it back 15", ["move"], {0: {"shift": "+15"}}, {"last_item": "Composing"}),
    ("cancel composing", ["remove"], {0: {"target": "compos"}}, {}),
    ("I took the day off", ["remove"], {0: {"target": "work"}}, {}),
    ("scratch that", ["remove"], {}, {"last_item": "Composing"}),
    ("actually cancel composing and read instead at 7", ["remove", "add"], {0: {"target": "compos"}, 1: {"start": "7"}}, {}),
    ("swap dinner and composing", ["swap"], {0: {"target": "dinner", "other": "compos"}}, {}),
    ("finished the taxes", ["outcome"], {0: {"target": "tax", "status": "done"}}, {}),
    ("did the taxes from 2 to 3:30", ["outcome"], {0: {"status": "done", "started": "2", "finished": "3:30"}}, {}),
    ("didn't get to the email", ["outcome"], {0: {"target": "email", "status": "skipped"}}, {}),
    ("got halfway through composing", ["outcome"], {0: {"status": "partial"}}, {}),
    ("yep", ["confirm"], {}, {"pending": "whether to add: Reading at 9:00 PM"}),
    ("sure, sounds good", ["confirm"], {}, {"pending": "whether to add: Walk at 5:00 PM"}),
    ("nah", ["reject"], {}, {"pending": "whether to add: Walk at 5:00 PM"}),
    ("the second one", ["choose"], {0: {"option": "2"}}, {"pending": "pick one of: Gym at 2:30 PM; Gym at 4:00 PM"}),
    ("option 1 please", ["choose"], {0: {"option": "1"}}, {"pending": "pick one of: Gym at 2:30 PM; Gym at 4:00 PM"}),
    ("yes I'm going to the team dinner", ["confirm"], {0: {"target": "team dinner"}}, {}),
    ("what should I do tonight?", ["suggest"], {}, {}),
    ("idk you pick", ["suggest"], {}, {"assistant": "What would you like to do this afternoon?"}),
    ("any ideas for something creative?", ["suggest"], {0: {"topic": "creat"}}, {}),
    ("when am I free this afternoon?", ["question"], {0: {"topic": "free"}}, {}),
    ("what's on my schedule tomorrow?", ["question"], {0: {"day": "tomorrow"}}, {}),
    ("how does my week look?", ["question"], {0: {"topic": "week"}}, {}),
    ("lay out my day in a clean list", ["question"], {}, {}),
    ("fyi the Probation section is stuff I probably won't do", ["remember"], {0: {"text": "robation"}}, {}),
    ("I like to keep evenings after 10 for winding down", ["profile"], {}, {}),
    ("I want to finish Natalie's piece by December", ["profile"], {0: {"text": "Natalie"}}, {}),
    ("I'm wiped", ["energy"], {0: {"level": "1"}}, {}),
    ("feeling great today!", ["energy"], {}, {}),
    ("thanks!", ["chat"], {}, {}),
    ("good morning", ["chat"], {}, {}),
    ("dinner at 6:30 for an hour then a 30 minute walk then laundry", ["add", "add", "add"], {0: {"start": "6:30"}, 1: {"title": "walk"}, 2: {"title": "aundry"}}, {}),
    ("hey so uh I think dinner at 7 maybe, no wait 7:30, for an hour", ["add"], {0: {"start": "7:30"}}, {}),
    ("finished taxes, still on the email", ["outcome"], {0: {"target": "tax", "status": "done"}}, {}),
    ("make the first upgrades block 3 thirties", ["move"], {0: {"target": "first", "duration": "3"}}, {}),
    ("put shower before the second upgrades", ["move"], {0: {"target": "shower", "before": "upgrades"}}, {}),
]


def check(actions, expected_types, fields) -> Tuple[bool, str]:
    types = [a.type for a in actions]
    if types[: len(expected_types)] != expected_types or len(types) != len(expected_types):
        return False, f"types {types} != {expected_types}"
    for i, wanted in fields.items():
        for key, sub in wanted.items():
            got = str(actions[i].fields.get(key, ""))
            if sub.lower() not in got.lower():
                return False, f"action {i} {key}={got!r}, expected ~{sub!r}"
    return True, ""


def main() -> None:
    from thirties_core.brains.slm.runtime import GemmaRuntime
    rt = GemmaRuntime.shared()
    session = rt.parser(SYSTEM_PROMPT)
    passed, failures, times = 0, [], []
    for message, types, fields, extra in CASES:
        context = build_context("Thursday 3:00 PM", SCHEDULE, extra.get("assistant", ""), extra.get("pending"),
                                [], message, last_item=extra.get("last_item", ""))
        start = time.time()
        raw = extract_json(session.ask(context)) or {}
        times.append(time.time() - start)
        ok, why = check(parse_actions(raw), types, fields)
        passed += ok
        if not ok:
            failures.append((message, why, raw))
        print(f"{'PASS' if ok else 'FAIL'} {times[-1]:.1f}s | {message}" + ("" if ok else f"  -> {why}"), flush=True)
    print(f"\n{passed}/{len(CASES)} parsed correctly; median {sorted(times)[len(times) // 2]:.1f}s per message")


if __name__ == "__main__":
    main()
