"""Make day scripts for tests/day_sim.py from real conversations, and variations of them.

    # one script per real day, from the app's stored conversations (and, optionally, a phone export with times)
    python3 -m tests.day_scripts extract --day 2026-10-16 --day 2026-10-17 --export phone_export.txt
    # variations of a script: same kind of person and backlog, a different day with different mishaps
    python3 -m tests.day_scripts vary docs/eval_reports/day_scripts/real_2026-10-17.json --n 4

Scripts made from real conversations hold personal details, so they go to
docs/eval_reports/day_scripts/ (gitignored). Each one is a single agy call.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.sim_eval import make_llm
from thirties_core.brains.slm.parse import extract_json

OUT_DIR = REPO_ROOT / "docs" / "eval_reports" / "day_scripts"
EXAMPLE = (REPO_ROOT / "tests" / "day_scripts" / "saturday_errands.json").read_text()

FORMAT = """A day script is a JSON object with exactly these fields (this example is synthetic):
{example}
- "life": the course of the day in time order, one beat per line, each starting with a 24-hour time: what the person asks the app for, what they actually do and for how long, what interrupts them, what they find out. Include what they want from the assistant in their own terms (e.g. "Ask to plan the morning routine as its separate items, about 2 thirties together").
- "constraints": facts the person knows but only says when they matter (e.g. "Nothing outside until after the shower").
- "truth": what the person really did, with real start and end times (24-hour), for scoring the app's record; "match" is |-separated words that would appear in the item's title; add "done": false for things planned but not done.
- "work": [start, end] in 24-hour time, or null if no work that day.
- "events": calendar events that day (24-hour times; "invite": true for unconfirmed invites).
- "setup": [tool, arguments] calls that set up the day before it starts, e.g. yesterday's unfinished item: ["add_event", {{"title": "Message Brad back", "start": "9:30 PM", "duration": "15 min", "day": "yesterday"}}].
- "persona": who they are and how they text and react, from how they actually wrote.
Return ONLY the JSON object, written directly in your reply: don't use tools, run commands, or read or write files."""

EXTRACT_PROMPT = """Below is a real day of someone using a planning assistant app ("Thirties"): the app's stored conversation for {day} (with the tool results it saw), and possibly the person's phone export with message times.

Write a day script that lets a simulator relive this day realistically against a new version of the app. Capture how the day really went, not how the app handled it: what the person wanted, what they did and when, what ran late, what interrupted them, the constraints they revealed (and when they revealed them), and how they reacted to the app. Don't include the app's mistakes as facts of the day.

{format}

The stored conversation:
{conversation}

The phone export (may include other days; use only {day}):
{export}"""

VARY_PROMPT = """Here is a day script for simulating a person's day with a planning assistant app:
{script}

Write {n} different variations of it, as a JSON object {{"scripts": [...]}}. Each one is the same kind of person (same persona and the same lists of things to do), but a different day: a different date within two weeks of the original (keep weekday versus weekend consistent with "work"), a different wake time, different things to get done from the same kinds of lists, different mishaps (something running long, an interruption, a change of mind, a forgotten constraint), and a different mood. Keep each script internally consistent: "truth" must match "life". Give each a new "id" ending in _v1, _v2 and so on, and "source": "variation of {sid}".

{format}"""


def stored_conversation(db_path: Path, day: str) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    lines = []
    for (messages,) in conn.execute("SELECT messages FROM conversations WHERE plan_date = ? ORDER BY created_at", (day,)):
        for m in json.loads(messages):
            content = str(m.get("content") or "").strip()
            calls = m.get("tool_calls") or []
            if m["role"] == "tool":
                lines.append(f"(tool result: {content[:400]})")
            elif content or calls:
                call_text = "".join(f"\n  CALL {c.get('name')} {json.dumps(c.get('arguments', {}))}" for c in calls)
                lines.append(f"{m['role'].capitalize()}: {content}{call_text}")
    return "\n".join(lines)


PEOPLE_DIR = REPO_ROOT / "tests" / "day_scripts" / "people"


def import_scripts(files: List[str], to: Path) -> None:
    """Every script in the files (JSON objects or arrays, fenced or not), checked, one file each."""
    import re as _re
    from tests.day_sim import validate
    to.mkdir(parents=True, exist_ok=True)
    good = bad = 0
    for f in files:
        text = Path(f).read_text()
        blocks = _re.findall(r"```(?:json)?\s*(.*?)```", text, _re.S) or [text]
        for block in blocks:
            try:
                data = json.loads(block)
            except ValueError as e:
                print(f"{f}: a block isn't valid JSON ({e}); fix it and import again")
                bad += 1
                continue
            for script in data if isinstance(data, list) else [data]:
                problems = validate(script)
                if problems:
                    print(f"{f}: {script.get('id', '?')}: " + "; ".join(problems[:6]))
                    bad += 1
                    continue
                (to / f"{script['id']}.json").write_text(json.dumps(script, indent=2, ensure_ascii=False))
                good += 1
    print(f"{good} scripts filed in {to.relative_to(REPO_ROOT)}, {bad} with problems")


def sort_real_lists(brain: str, model: str) -> None:
    """Sort the user's real Joplin lists with one model into docs/eval_reports/day_scripts/lists_<brain>.sqlite."""
    import time
    from collections import Counter
    from thirties_core.brains.list_sorter import FrontierListSorter, GemmaListSorter
    from thirties_core.config import load_config
    from thirties_core.joplin_engine import JoplinEngine
    from thirties_core.lists import ListCatalog, sort_lists
    from thirties_core.scheduler import StateDatabase
    cfg = load_config()
    engine = JoplinEngine(cfg)
    notes, tasks = engine.fetch_active_notes(), engine.fetch_tasks()
    path = OUT_DIR / f"lists_{brain}.sqlite"
    path.unlink(missing_ok=True)
    catalog = ListCatalog(StateDatabase(path))
    if brain == "slm":
        from thirties_core.brains.slm.runtime import GemmaRuntime
        sorter = GemmaListSorter(GemmaRuntime.shared(cfg.assistant.slm_model_path))
    else:
        from thirties_core.inference import AntigravityInferenceEngine
        from thirties_core.config import ThirtiesConfig
        fcfg = ThirtiesConfig()
        fcfg.inference.gemini_model, fcfg.inference.timeout_seconds = model, 600
        sorter = FrontierListSorter(AntigravityInferenceEngine(fcfg))
    start = time.time()
    counts = sort_lists(catalog, notes, tasks, sorter, date.today())
    print(f"{counts['roles']} roles and {counts['items']} urgencies in {time.time() - start:.0f}s -> {path}")
    roles = catalog.roles()
    for n in sorted(notes, key=lambda n: (n["source_notebook"], n["title"])):
        print(f"  {roles.get(n['id'], {}).get('role', '?'):<9} {n['source_notebook']} / {n['title']}")
    by_note = Counter((u["note_id"], u["urgency"]) for u in catalog.urgencies().values())
    titles = {n["id"]: n["title"] for n in notes}
    for nid in sorted({k[0] for k in by_note}, key=lambda i: titles.get(i, "")):
        print(f"  {titles.get(nid, nid)}: " + ", ".join(f"{u} {by_note[(nid, u)]}"
                                                       for u in ("now", "soon", "whenever", "someday")
                                                       if by_note[(nid, u)]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--day", action="append", required=True)
    ex.add_argument("--db", default=str(Path.home() / ".local/share/thirties/state.sqlite"))
    ex.add_argument("--export", default="")
    va = sub.add_parser("vary")
    va.add_argument("script")
    va.add_argument("--n", type=int, default=4)
    im = sub.add_parser("import", help="check and file scripts written elsewhere (e.g. the Gemini web app)")
    im.add_argument("files", nargs="+")
    im.add_argument("--people", action="store_true",
                    help="made-up people with their own lists: into tests/day_scripts/people (committed)")
    so = sub.add_parser("lists", help="sort the real lists once (roles and urgency) for the day sim's --lists-from")
    so.add_argument("--brain", choices=["slm", "frontier"], default="slm")
    ap.add_argument("--model", default="gemini-3.8-flash")
    args = ap.parse_args()
    if args.cmd == "lists":
        sort_real_lists(args.brain, args.model)
        return
    if args.cmd == "import":
        import_scripts(args.files, PEOPLE_DIR if args.people else OUT_DIR)
        return
    llm = make_llm(args.model, timeout=900)  # long prompts, long answers
    fmt = FORMAT.format(example=EXAMPLE)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made = []
    if args.cmd == "extract":
        export = Path(args.export).read_text() if args.export else "(none)"
        for day in args.day:
            date.fromisoformat(day)
            script = extract_json(llm(EXTRACT_PROMPT.format(day=day, format=fmt, export=export,
                                                            conversation=stored_conversation(Path(args.db), day))))
            if not script:
                print(f"{day}: no script came back")
                continue
            script.update({"id": f"real_{day}", "date": script.get("date") or day,
                           "source": f"the stored conversation of {day}"})
            made.append(script)
    else:
        original = json.loads(Path(args.script).read_text())
        out = extract_json(llm(VARY_PROMPT.format(script=json.dumps(original, indent=2), n=args.n,
                                                  sid=original["id"], format=fmt))) or {}
        made = [s for s in out.get("scripts", []) if isinstance(s, dict) and s.get("id")]
    for script in made:
        path = OUT_DIR / f"{script['id']}.json"
        path.write_text(json.dumps(script, indent=2, ensure_ascii=False))
        print(f"wrote {path.relative_to(REPO_ROOT)}: {len(script.get('life', []))} beats, "
              f"{len(script.get('truth', []))} truth items")


if __name__ == "__main__":
    main()
