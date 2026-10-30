"""Parse accuracy of the SLM brain's model calls, on labelled utterances.

Fast feedback for instruction/example changes: each case is one user message
with a little context, and the expected action types plus a few key fields.
Runs the real parser session (KV cache reused across cases) on Gemma, or on
another small model from the catalog (--slm; see tests/slm_model_compare.py).

By default a message goes through the brain's own steps (thirties_core/brains/slm/steps.py):
Python separates the requests, the model parses each, and parts nothing covered
are parsed again on their own. ``--pipeline whole`` is one parse per message,
as measured before 0.2 (comparable with the 2026-10-15 numbers).

Cases are the hand-written ones below plus tests/slm_parse_cases.json, which
Gemini wrote and labelled from the parser's own instruction (--make-cases), and
which were then spot-checked. When Gemma and a label disagree, read both: the
label can be the wrong one.

    ~/.local/share/thirties/venv/bin/python -m tests.slm_parse_eval
    ~/.local/share/thirties/venv/bin/python -m tests.slm_parse_eval --pipeline whole
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
    # 0.2: what a Gemini user could already do
    ("put my morning routine on", ["routine"], {0: {"title": "routine"}}, {}),
    ("do the morning routine at 9 tomorrow", ["routine"], {0: {"start": "9", "day": "tomorrow"}}, {}),
    ("stick the bank call on today's list", ["today"], {0: {"title": "bank"}}, {}),
    ("that's not urgent", ["urgency"], {0: {"urgency": "whenever"}}, {"last_item": "Composing"}),
    ("the passport renewal has to happen by friday", ["urgency"], {0: {"target": "passport", "day": "friday"}}, {}),
    ("composing has to wait until after dinner", ["fact"], {0: {"target": "compos", "after": "dinner"}}, {}),
    ("dinner is at 6 and it can't move", ["fact"], {0: {"target": "dinner", "fixed": "True"}}, {}),
    # several requests, and one item with something alongside
    ("work ran until 4:30, and move composing to 8", ["move", "move"],
     {0: {"target": "work", "end": "4:30"}, 1: {"target": "compos", "start": "8"}}, {}),
    ("finished the taxes. push composing back half an hour and what's left tonight?", ["outcome", "move", "question"],
     {0: {"target": "tax"}, 1: {"target": "compos", "shift": "+"}}, {}),
    ("dinner at 6, cooking and eating, listening to Project Hail Mary", ["add", "add"],
     {0: {"title": "inner"}, 1: {"title": "Hail Mary", "alongside": "True"}}, {}),
]


PRONOUN_LABELS = {"it", "that", "this", "them", "those"}


def _same_field(key: str, want: Any, got: Any, context: Dict[str, Any]) -> bool:
    """A field matches if it contains the label, or means the same thing (an hour == 1 hour, 'it' == the last item)."""
    from datetime import date
    from thirties_core.clock import ClockError, parse_day, parse_duration_minutes
    want_s, got_s = str(want).lower(), str(got).lower()
    if want_s in got_s:
        return True
    if key == "topic":
        return True  # free text the brain only scans for a few words; the action type is what matters
    if key == "duration":
        for word, num in (("forty-five", "45"), ("fifteen", "15"), ("twenty", "20"), ("thirty", "30"), ("ninety", "90")):
            want_s = want_s.replace(word, num)
        try:
            return parse_duration_minutes(want_s) == parse_duration_minutes(got_s)
        except (ClockError, ValueError):
            return False
    if key in ("target", "other") and want_s in PRONOUN_LABELS:
        last = str(context.get("last_item", "")).lower()
        return bool(last) and (got_s in last or last in got_s)
    if key in ("day", "to_day"):
        today = date(2026, 10, 15)
        try:
            fixed = {"satruday": "saturday", "tmrw": "tomorrow", "tmr": "tomorrow", "tomorow": "tomorrow"}
            return parse_day(fixed.get(want_s, want_s), today) == parse_day(got_s, today)
        except Exception:
            return False
    return False


def _equivalent(want_type: str, want: Dict[str, Any], got) -> bool:
    """Different action, same effect: 'finished work at 3' as an outcome or as moving work's end to 3."""
    if want_type in ("confirm", "choose") and got.type == "accept":
        return True
    if want_type == "add" and got.type == "move":  # naming an item that's already planned moves it
        title = str(want.get("title", "")).lower()
        return bool(title) and title in str(got.fields.get("target", "")).lower()
    if want_type == "outcome" and "work" in str(want.get("target", "")).lower() and got.type == "move":
        return "work" in str(got.fields.get("target", "")).lower() and \
            str(want.get("finished", "")).lower() in str(got.fields.get("end", "")).lower()
    return False


ANCHOR_ENDS = {"work": "4:00", "dinner": "7:00", "composing": "8:30"}  # on SCHEDULE


def check(actions, expected_types, fields, context=None) -> Tuple[bool, str]:
    context = context or {}
    if any(a.type != "chat" for a in actions) and any(t != "chat" for t in expected_types):
        # small talk next to real requests changes nothing; ignore it on both sides
        kept = [i for i, t in enumerate(expected_types) if t != "chat"]
        fields = {kept.index(i): f for i, f in fields.items() if i in kept}
        expected_types = [expected_types[i] for i in kept]
        actions = [a for a in actions if a.type != "chat"]
    types = [a.type for a in actions]
    if len(types) != len(expected_types):
        return False, f"types {types} != {expected_types}"
    for i, (want_type, got) in enumerate(zip(expected_types, actions)):
        if got.type != want_type and not _equivalent(want_type, fields.get(i, {}), got):
            return False, f"types {types} != {expected_types}"
    for i, wanted in fields.items():
        if actions[i].type != expected_types[i]:
            continue  # an equivalent action was checked above
        for key, sub in wanted.items():
            got = actions[i].fields.get(key, "")
            if key == "after" and not got and str(sub).lower() in ANCHOR_ENDS:
                # "after work" given as the time work ends, read off the schedule
                start = str(actions[i].fields.get("start", "")).lower().replace(" pm", "").replace("pm", "")
                if start == ANCHOR_ENDS[str(sub).lower()]:
                    continue
            if not _same_field(key, sub, got, context):
                return False, f"action {i} {key}={str(got)!r}, expected ~{sub!r}"
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
    "routine": "putting one of their routines on the plan (morning routine, bedtime routine), sometimes at a time",
    "today_list": "putting something on today's list, with no time",
    "urgency": "saying how pressing an item on their lists is, or a date it's due by",
    "fact": "a rule an item must keep: after another item, not before a time, can't move, needs daylight",
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


class _NoModel:
    """The interpreter never calls the model."""

    def parser(self, system):
        raise RuntimeError("no model in the interpreter")


def make_interpreter():
    """An SLM brain on the benchmark's schedule, used only for its deterministic reading of parses."""
    from tests.fixtures import DAY, at, make_scheduler
    from thirties_core.astronomy import set_debug_time
    from thirties_core.brains.slm.brain import SlmBrain
    from thirties_core.conversation import ConversationManager
    set_debug_time(at("15:00"))
    sched = make_scheduler()
    plan, pending = sched.build_day_plan(DAY)
    manager = ConversationManager(plan, [], pending, sched, active_notes=[], brain=None, inference_engine=None)
    manager.execute_tool("add_event", {"title": "Dinner", "start": "6:00 PM", "end": "7:00 PM"})
    manager.execute_tool("add_event", {"title": "Composing", "start": "7:00 PM", "end": "8:30 PM"})
    return SlmBrain(manager.tools, tasks=[], runtime=_NoModel())


def interpret(brain, raw, message: str, context: Dict[str, Any]):
    """What the SLM brain would actually do with this parse (a raw reply or parsed actions), given the same context."""
    import re
    brain.state = type(brain.state)()
    asked = context.get("assistant") or context.get("pending") or ""
    if asked:
        options = re.findall(r"(?:^|\s)(\d)\s*[).:]\s*([^,;|]+)", asked)
        if len(options) >= 2:
            brain.state.proposal = [{"title": t.strip()} for _, t in options]
            brain.state.proposal_kind = "options"
        elif re.search(r"(^|[.!?]\s*)(should i|shall i|want me to|would you like (me )?to|do you want (me )?to|"
                       r"how about|can i|could i)\b|^whether to\b", asked, re.I):
            brain.state.proposal = [{"title": "the suggestion"}]  # an offer awaiting yes/no
            brain.state.proposal_kind = "options"
    last = str(context.get("last_item") or "").lower()
    if last:
        brain.state.last_ids = [s.id for s in brain.tools.day_plan.spans if s.title.lower() == last][:1]
    brain._message = message
    actions = parse_actions(raw) if isinstance(raw, dict) else [type(a)(a.type, dict(a.fields)) for a in raw]
    return brain._interpret(actions, message)


PIPELINES = ("steps", "whole")


def run_benchmark(runtime, cases: List[Dict[str, Any]], pipeline: str = "steps", quiet: bool = False,
                  log=print) -> Dict[str, Any]:
    """Run the cases through one runtime's parser session; returns the scores, timings and failures.

    ``steps``: the brain's pipeline (separate the requests, parse each, retry what nothing covered).
    ``whole``: one parse per message, as before 0.2. Either way "model" is what the model's parses gave, and "final"
    is after Python's reading (SlmBrain._interpret). A case where the brain would ask about a part it couldn't read
    counts as a miss, and is counted separately as "asked" (better than dropping it silently).
    """
    from thirties_core.brains.slm.steps import parse_in_steps
    session = runtime.parser(SYSTEM_PROMPT)
    brain = make_interpreter()
    by_cat: Dict[str, List[bool]] = defaultdict(list)
    by_type: Dict[str, List[bool]] = defaultdict(list)
    final_cat: Dict[str, List[bool]] = defaultdict(list)
    failures, times, calls, asked = [], [], [], 0
    for c in cases:
        extra = c["context"]
        raws: List[Any] = []

        def parse_one(text: str):
            context = build_context("Thursday 3:00 PM", SCHEDULE, extra.get("assistant", ""), extra.get("pending"),
                                    [], text, last_item=extra.get("last_item", ""))
            raw = extract_json(session.ask(context)) or {}
            raws.append(raw)
            return parse_actions(raw)

        start = time.time()
        if pipeline == "whole":
            actions, unclear = parse_one(c["message"]), []
        else:
            actions, unclear, _ = parse_in_steps(c["message"], parse_one, brain._python_handles)
        times.append(time.time() - start)
        calls.append(len(raws))
        asked += bool(unclear)
        ok, why = check(actions, c["types"], c["fields"], c["context"])
        final_ok, final_why = check(interpret(brain, actions, c["message"], c["context"]), c["types"], c["fields"],
                                    c["context"])
        final_cat[c["category"]].append(final_ok)
        by_cat[c["category"]].append(ok)
        for t in set(c["types"]):
            by_type[t].append(ok)
        if not ok or not final_ok:
            failures.append({**c, "why": why, "final_why": final_why, "final_ok": final_ok, "got": raws,
                             "unclear": unclear})
        if not final_ok or not quiet:
            log(f"{'PASS' if final_ok else 'FAIL'} {times[-1]:.1f}s [{c['category']}] {c['message'][:110]}"
                + ("" if final_ok else f"  -> {final_why}"))
    n = max(1, len(cases))
    ordered = sorted(times) or [0.0]
    return {
        "pipeline": pipeline, "cases": len(cases),
        "model": sum(ok for oks in by_cat.values() for ok in oks),
        "final": sum(ok for oks in final_cat.values() for ok in oks),
        "asked": asked,
        "median_s": round(ordered[len(ordered) // 2], 2), "p90_s": round(ordered[min(len(ordered) - 1, int(n * 0.9))], 2),
        "calls_per_message": round(sum(calls) / n, 2),
        "by_category": {cat: [sum(oks), len(oks), sum(by_cat[cat])] for cat, oks in final_cat.items()},
        "by_type": {t: [sum(oks), len(oks)] for t, oks in by_type.items()},
        "failures": failures,
    }


def print_summary(result: Dict[str, Any], log=print) -> None:
    n = max(1, result["cases"])
    log(f"\nModel alone: {result['model']}/{result['cases']} ({result['model'] / n:.0%}). After Python's reading: "
        f"{result['final']}/{result['cases']} ({result['final'] / n:.0%}). Asked about a part: {result['asked']}. "
        f"Median {result['median_s']:.1f}s per message, {result['calls_per_message']} model calls per message "
        f"({result['pipeline']})")
    log("\nBy category, after Python (worst first; model alone in brackets):")
    for cat, (good, total, raw_good) in sorted(result["by_category"].items(), key=lambda kv: kv[1][0] / kv[1][1]):
        log(f"  {cat:<18} {good:>3}/{total:<3} {good / total:>4.0%}   ({raw_good / total:.0%})")
    log("\nModel alone, by expected action type:")
    for t, (good, total) in sorted(result["by_type"].items(), key=lambda kv: kv[1][0] / kv[1][1]):
        log(f"  {t:<10} {good:>3}/{total:<3} {good / total:.0%}")


def select_cases(categories=None, limit: int = 0) -> List[Dict[str, Any]]:
    cases = [c for c in all_cases() if not categories or c["category"] in categories]
    random.Random(0).shuffle(cases)  # the KV-cached session sees a realistic mix, not one category in a row
    return cases[:limit] if limit else cases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make-cases", type=int, default=0, metavar="N", help="have Gemini write N cases per category")
    ap.add_argument("--model", default="gemini-3.8-flash-medium", help="the Gemini model that writes cases")
    ap.add_argument("--slm", default=None, help="the small model to measure (tests/slm_model_compare.py lists them)")
    ap.add_argument("--pipeline", choices=PIPELINES, default="steps")
    ap.add_argument("--category", nargs="*", help="only these categories")
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases (of the fixed shuffle)")
    ap.add_argument("--quiet", action="store_true", help="print only failures")
    args = ap.parse_args()
    if args.make_cases:
        make_cases(args.make_cases, args.model)
        return

    from thirties_core.brains.slm.catalog import load_runtime
    result = run_benchmark(load_runtime(model=args.slm), select_cases(args.category, args.limit), args.pipeline,
                           args.quiet, log=lambda s: print(s, flush=True))
    print_summary(result)
    out = REPO_ROOT / "docs" / "eval_reports" / f"parse_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result["failures"], indent=1, default=str))
    print(f"\nFailures: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
