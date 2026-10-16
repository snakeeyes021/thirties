"""Parse accuracy of the SLM brain's one model call, on labelled utterances.

Fast feedback for instruction/example changes: each case is one user message
with a little context, and the expected action types plus a few key fields.
Runs the real Gemma parser session (KV cache reused across cases).

Cases are the hand-written ones below plus tests/slm_parse_cases.json, which
Gemini wrote and labelled from the parser's own instruction (--make-cases), and
which were then spot-checked. When Gemma and a label disagree, read both: the
label can be the wrong one.

    ~/.local/share/thirties/venv/bin/python -m tests.slm_parse_eval
    ~/.local/share/thirties/venv/bin/python -m tests.slm_parse_eval --make-cases 15   # per category, via agy
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.brains.slm.parse import ACTION_SCHEMA, ACTION_TYPES, SYSTEM_PROMPT, build_context, extract_json, parse_actions

CASES_FILE = REPO_ROOT / "tests" / "slm_parse_cases.json"
FIELDS = set(ACTION_SCHEMA["properties"]["actions"]["items"]["properties"]) - {"type"}

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


CATEGORIES = {
    "add_exact": "adding a new item at a stated clock time with a duration or end time",
    "add_loose": "adding an item with a loose time ('around 7', 'later this afternoon', 'after work')",
    "add_relative": "adding an item right after or before an existing item",
    "add_alongside": "doing something at the same time as an existing item (audiobook during dinner, podcast on a walk)",
    "add_push": "squeezing an item in and sliding everything after it later",
    "add_other_day": "adding an item on another day (tomorrow, Monday, Oct 20)",
    "add_no_time": "mentioning something they need to do with no time given",
    "add_chunks": "adding items with durations in chunks or thirties (30-minute units)",
    "move_shift": "pushing an existing item later or earlier by an amount",
    "move_time": "moving an existing item to a new time",
    "move_work": "work ran long, ended early, or starts late today",
    "move_day": "moving an existing item to another day",
    "move_length": "making an existing item longer or shorter",
    "move_pronoun": "moving 'it' / 'that' when the context names the last item mentioned",
    "remove": "cancelling or dropping an existing item",
    "day_off": "taking the day off work",
    "swap": "swapping the order of two existing items",
    "outcome_done": "reporting they finished something, sometimes with when it started or ended",
    "outcome_skipped": "reporting they didn't get to something, or only partly did it",
    "confirm": "saying yes to the assistant's proposal (give the assistant's proposal in context)",
    "reject": "saying no to the assistant's proposal (give the proposal in context)",
    "choose": "picking one numbered option the assistant offered (give the options in context)",
    "suggest": "asking for ideas, or asking the assistant to fill a stretch of time or pick for them",
    "question_free": "asking how much free time they have, or where something could fit",
    "question_schedule": "asking to see the schedule, today or another day",
    "question_tasks": "asking about their backlog or past conversations",
    "remember": "explaining how something works for them (what a list or section means, how they like things)",
    "profile": "stating a goal, habit or preference about their life",
    "energy": "saying how energetic or tired they are",
    "chat": "thanks, greetings, small talk",
    "multi": "two or three different requests in one message",
    "ramble": "a long voice-to-text message with a self-correction; label only the final intent",
}

MAKE_PROMPT = """You are writing test cases for a small model that turns a user's message to a day-planning assistant into JSON actions.
This is the model's exact instruction, which is the labelling spec:
<<<
{spec}
>>>
The user's current schedule is: {schedule}. It is Thursday 3:00 PM.

Write {n} varied, realistic messages for this category: {category}.
Vary the voice: terse, chatty, lowercase texting, typos, formal, rambling. Use the schedule's items (Work, Dinner, Composing) or new ones.
For each, give the correct labels per the spec. Only label fields you are sure of, using a short substring the field must contain
(e.g. "start": "7:30", "target": "dinner", "shift": "+", "status": "done", "push": "True", "alongside": "True").
"context" may hold "assistant" (what the assistant just said, needed for confirm/reject/choose) and "last_item" (for 'it'/'that').
Return ONLY a JSON array:
[{{"message": "...", "context": {{}}, "types": ["add"], "fields": {{"0": {{"start": "7"}}}}}}, ...]"""


def make_cases(per_category: int, model: str) -> None:
    """Have Gemini write and label cases for every category, and save them for spot-checking."""
    from thirties_core.config import ThirtiesConfig
    from thirties_core.inference import AntigravityInferenceEngine
    cfg = ThirtiesConfig()
    cfg.inference.gemini_model = model
    cfg.inference.timeout_seconds = 240
    engine = AntigravityInferenceEngine(cfg)

    def one(item):
        key, desc = item
        prompt = MAKE_PROMPT.format(spec=SYSTEM_PROMPT, schedule=" | ".join(SCHEDULE), n=per_category, category=desc)
        for _ in range(3):
            text = engine.chat([{"role": "user", "content": prompt}]).get("content", "")
            start, end = text.find("["), text.rfind("]")
            try:
                return key, json.loads(text[start:end + 1])
            except ValueError:
                continue
        return key, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(one, CATEGORIES.items()))
    cases, dropped = [], 0
    for key, batch in batches:
        for c in batch:
            ok = (isinstance(c, dict) and isinstance(c.get("message"), str) and isinstance(c.get("types"), list)
                  and c["types"] and all(t in ACTION_TYPES for t in c["types"])
                  and all(f in FIELDS for fs in (c.get("fields") or {}).values() for f in fs))
            if ok:
                cases.append({"category": key, "message": c["message"], "context": c.get("context") or {},
                              "types": c["types"], "fields": c.get("fields") or {}})
            else:
                dropped += 1
    CASES_FILE.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(cases)} cases to {CASES_FILE.relative_to(REPO_ROOT)} ({dropped} malformed dropped)")


def all_cases() -> List[Dict[str, Any]]:
    cases = [{"category": "hand", "message": m, "context": extra, "types": t, "fields": f} for m, t, f, extra in CASES]
    if CASES_FILE.exists():
        for c in json.loads(CASES_FILE.read_text()):
            c["fields"] = {int(k): v for k, v in c["fields"].items()}
            cases.append(c)
    return cases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make-cases", type=int, default=0, metavar="N", help="have Gemini write N cases per category")
    ap.add_argument("--model", default="gemini-3.8-flash-medium")
    ap.add_argument("--category", nargs="*", help="only these categories")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args()
    if args.make_cases:
        make_cases(args.make_cases, args.model)
        return

    from thirties_core.brains.slm.runtime import GemmaRuntime
    rt = GemmaRuntime.shared()
    session = rt.parser(SYSTEM_PROMPT)
    cases = [c for c in all_cases() if not args.category or c["category"] in args.category]
    random.Random(0).shuffle(cases)  # the KV-cached session sees a realistic mix, not one category in a row
    by_cat: Dict[str, List[bool]] = defaultdict(list)
    by_type: Dict[str, List[bool]] = defaultdict(list)
    failures, times = [], []
    for c in cases:
        extra = c["context"]
        context = build_context("Thursday 3:00 PM", SCHEDULE, extra.get("assistant", ""), extra.get("pending"),
                                [], c["message"], last_item=extra.get("last_item", ""))
        start = time.time()
        raw = extract_json(session.ask(context)) or {}
        times.append(time.time() - start)
        ok, why = check(parse_actions(raw), c["types"], c["fields"])
        by_cat[c["category"]].append(ok)
        for t in set(c["types"]):
            by_type[t].append(ok)
        if not ok:
            failures.append({**c, "why": why, "got": raw})
        if not ok or not args.quiet:
            print(f"{'PASS' if ok else 'FAIL'} {times[-1]:.1f}s [{c['category']}] {c['message'][:110]}"
                  + ("" if ok else f"  -> {why}"), flush=True)
    passed = sum(ok for oks in by_cat.values() for ok in oks)
    print(f"\n{passed}/{len(cases)} parsed correctly ({passed / len(cases):.0%}); "
          f"median {sorted(times)[len(times) // 2]:.1f}s per message")
    print("\nBy category (worst first):")
    for cat, oks in sorted(by_cat.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"  {cat:<18} {sum(oks):>3}/{len(oks):<3} {sum(oks) / len(oks):.0%}")
    print("\nBy expected action type:")
    for t, oks in sorted(by_type.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"  {t:<10} {sum(oks):>3}/{len(oks):<3} {sum(oks) / len(oks):.0%}")
    out = REPO_ROOT / "docs" / "eval_reports" / f"parse_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(failures, indent=1, default=str))
    print(f"\nFailures: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
