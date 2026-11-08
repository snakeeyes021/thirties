"""The bar for the Frontier brain (ROADMAP C4, AGENTS.md §7 light): a frontier model handed the raw backlog.

AGENTS.md §7 sets the bar: "a frontier model handed the raw backlog and asked to
plan the day. Our harness should beat that." This measures that baseline, so the
claim can be checked instead of assumed.

For each day script (tests/day_sim.py), the simulated user writes their first
message of the day, exactly as in the day sim. Then:

- **The bar:** Gemini gets the user's notes as plain text (every note's title and
  body, as they are in Joplin), the profile the script gives, the calendar, work
  hours, the time now and anything already planned, plus the user's message. No
  Thirties tools, no scaffolding, no rules about blocks. It answers in one reply:
  a message to the user, and the plan as machine-readable lines, which are put on
  a fresh copy of the day so the record looks like Thirties' own.
- **Thirties** (unless ``--no-pair``): the Frontier brain answers the same message
  on the same starting day, with its tools.

Both are graded by the day sim's own judge (``judge_day``) and measured by its
own ``measure``, so the two grades are directly comparable. The judge sees one
exchange (the opening request and the plan), not a whole day: compare requests
met, accuracy, understanding, plan respect, clarity and tone; message load and
record keeping say little here (nothing has happened yet).

    ~/.local/share/thirties/venv/bin/python -m tests.frontier_bar --all
    ... --script saturday_errands --no-pair

It calls agy (the simulated user, the planner, the judge); it checks the quota
first, like the day sim. Reports go to docs/eval_reports/frontier_bar/.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time as time_mod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.day_sim import (DAY_SIM_PROMPT, JUDGE_DIMS, SCRIPT_DIRS, DayRun, DayScript, find_scripts, judge_day,
                           measure, requests_met, tool_lines)
from tests.sim_eval import (QuotaExhausted, at, budget_left, build_manager, make_llm, quota_stop,
                            raise_if_agy_failed)
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.slm.parse import extract_json
from thirties_core.clock import format_clock
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, new_span_id

REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports" / "frontier_bar"
CALLS_PER_SCRIPT = {"bar": 4, "thirties": 12}  # opening + plan + judge + rating; Thirties' replies are a few more
FALLBACK_OPENING = "Morning! Can you plan my day?"
logger = logging.getLogger("frontier_bar")


# ----------------------------------------------------------------------------
# The prompt: the raw backlog and the situation, nothing else
# ----------------------------------------------------------------------------

BAR_PROMPT = """You are a capable personal assistant. Your client has just texted you (below). Plan their day.

Today is {weekday}, {date}. It is now {now}. Sunrise {sunrise}, sunset {sunset}.
Work today: {work}

Their calendar today:
{calendar}

Already planned today:
{planned}

Planned yesterday and not marked done:
{yesterday}

What they've told you about themselves:
{profile}

Everything in their notes app, as it is (every note: notebook, title, then its text):
{notes}

Their message:
"{message}"

Reply with your message to them, as you'd text it. Then, after a line with only PLAN on it, list the plan for the rest of today, one item per line, in 24-hour time, exactly like this:
PLAN
09:30-10:00 | Breakfast
10:00-11:00 | Laundry
Put only what they will do in the plan (not work or calendar events, which are already there). Nothing after the plan."""


def notes_text(notes: List[Dict[str, Any]]) -> str:
    """The notes as plain text, titles and bodies untouched: the raw backlog."""
    blocks = []
    for n in notes:
        head = f"### {n.get('source_notebook') or n.get('notebook') or 'Notes'} / {n.get('title', '')}"
        blocks.append(f"{head}\n{(n.get('body') or '').strip() or '(empty)'}")
    return "\n\n".join(blocks) or "(no notes)"


def _span_lines(spans: List[EventSpan]) -> str:
    return "\n".join(f"- {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title}"
                     + (" (unconfirmed invite)" if s.tentative else "") for s in spans) or "(none)"


def bar_prompt(script: DayScript, plan, yesterday, notes: List[Dict[str, Any]], profile: Dict[str, str],
               message: str) -> str:
    """The planner's whole input. Facts a person's assistant would know, in plain words; no tools, no rules."""
    work = next((s for s in plan.spans if s.category == SpanCategory.WORK and s.overlaps(plan.day_start, plan.day_end)),
                None)
    calendar = [s for s in plan.spans_in_view() if s.category == SpanCategory.CALENDAR]
    mine = [s for s in plan.spans_in_view() if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)]
    left = [s for s in yesterday.spans_in_view() if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
            and s.status == SpanStatus.PLANNED and s.start_dt.date() == yesterday.target_date]
    about = "\n".join(f"{k}: {v}" for k, v in profile.items() if v.strip()) or "(nothing yet)"
    return BAR_PROMPT.format(
        weekday=script.day.strftime("%A"), date=script.day.strftime("%B %d, %Y"),
        now=format_clock(at(script.day, script.wake)),
        sunrise=format_clock(plan.sunrise), sunset=format_clock(plan.sunset),
        work=f"{format_clock(work.start_dt)} – {format_clock(work.end_dt)}" if work else "none (day off)",
        calendar=_span_lines(calendar),
        planned=_span_lines(mine), yesterday=_span_lines(left), profile=about, notes=notes_text(notes),
        message=message)


# ----------------------------------------------------------------------------
# Reading the plan back
# ----------------------------------------------------------------------------

PLAN_LINE = re.compile(r"^\s*[-*•]?\s*(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?\s*[-–—]+\s*(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?"
                       r"\s*[|:]\s*(.+?)\s*$", re.IGNORECASE)
PLAN_MARK = re.compile(r"^\s*(?:\*\*)?PLAN(?:\*\*)?:?\s*$", re.MULTILINE)


def _hour(h: int, m: int, ampm: Optional[str]) -> Tuple[int, int]:
    if ampm:
        pm = ampm.lower().startswith("p")
        h = h % 12 + (12 if pm else 0)
    return h % 24, m


def split_reply(reply: str) -> Tuple[str, str]:
    """(what the user reads, the plan block). Without a PLAN line, the whole reply is both."""
    m = PLAN_MARK.search(reply)
    if not m:
        return reply.strip(), reply
    return reply[:m.start()].strip(), reply[m.end():]


def parse_plan(text: str, day: date, wake: str) -> List[Tuple[datetime, datetime, str]]:
    """Plan lines as (start, end, title) on the script's day. A time well before waking is after midnight; an end
    at or before its start crosses midnight."""
    woke = at(day, wake)
    out = []
    for line in text.replace("```", "\n").splitlines():
        m = PLAN_LINE.match(line)
        if not m:
            continue
        h1, m1 = _hour(int(m.group(1)), int(m.group(2)), m.group(3) or m.group(6))
        h2, m2 = _hour(int(m.group(4)), int(m.group(5)), m.group(6))
        title = m.group(7).strip().strip("*").strip()
        if not title:
            continue
        start = at(day, f"{h1:02d}:{m1:02d}")
        if start < woke - timedelta(hours=3):
            start += timedelta(days=1)
        end = start.replace(hour=h2, minute=m2)
        while end <= start:
            end += timedelta(days=1)
        out.append((start, end, title))
    return out


_ALREADY_THERE = {"work", "sleep", "bed", "bedtime", "go to bed"}


def put_on_plan(run: DayRun, items: List[Tuple[datetime, datetime, str]]) -> List[str]:
    """Write the bar's plan to its copy of the day, as Thirties' record would hold it. An item already planned
    (same title) moves to the bar's time rather than appearing twice; work, sleep and calendar events are already
    there. Returns what was skipped, for the report."""
    db = run.scheduler.state_db
    plan = run.plan(0)
    existing = {s.title.strip().lower(): s for s in plan.spans_in_view()}
    skipped = []
    for start, end, title in items:
        key = title.strip().lower()
        same = existing.get(key)
        if key in _ALREADY_THERE or (same is not None and (same.category == SpanCategory.CALENDAR or same.is_envelope)):
            skipped.append(title)
            continue
        if same is not None:
            same.start_dt, same.end_dt = start, end
            db.upsert_span(same)
            continue
        span = EventSpan(new_span_id(), title, start, end, SpanCategory.ACTIVITY)
        db.upsert_span(span)
        existing[key] = span
    return skipped


# ----------------------------------------------------------------------------
# One script: the opening message, the bar, and (paired) Thirties
# ----------------------------------------------------------------------------

def ask_engine(engine) -> Callable[[str], str]:
    """A prompt -> text callable from an inference engine (complete() if it has one, else one chat turn)."""
    def ask(prompt: str) -> str:
        if hasattr(engine, "complete"):
            return engine.complete(prompt)
        return str(engine.chat([{"role": "user", "content": prompt}]).get("content") or "")
    return ask


def opening_message(script: DayScript, sim) -> str:
    """The simulated user's first message of the day, from the day sim's own prompt: the same request the
    Thirties day starts with."""
    life = "\n".join(f"- {b}" for b in script.life)
    constraints = "\n".join(f"- {c}" for c in script.constraints) or "(none)"
    wake = at(script.day, script.wake)
    bed = at(script.day, script.bed, 1 if script.bed < script.wake else 0)
    move = extract_json(sim(DAY_SIM_PROMPT.format(persona=script.persona, situation=script.situation, life=life,
                                                  constraints=constraints, now=format_clock(wake),
                                                  bed=format_clock(bed), transcript="(nothing yet)"))) or {}
    return (move.get("message") or "").strip() or FALLBACK_OPENING


def fresh_day(script: DayScript, tasks, notes, engine, brain: str) -> Tuple[DayRun, List[Dict[str, Any]]]:
    """The script's starting day (work, calendar, setup, profile), as the day sim builds it. Returns the run and
    the notes it's planned from (the script's own lists, if it has them)."""
    if script.notes:
        tasks, notes = script.own_backlog()
    run = DayRun(script, brain)
    set_debug_time(at(script.day, script.wake))
    run.manager, run.scheduler = build_manager(script.scenario(), "frontier", tasks, notes, engine)
    return run, notes


def run_bar(script: DayScript, tasks, notes, opening: str, planner, judge, sim, engine) -> DayRun:
    """The bar: one reply from the raw backlog, put on a copy of the day, measured and judged like a day."""
    run, notes = fresh_day(script, tasks, notes, engine, "bar")
    try:
        prompt = bar_prompt(script, run.plan(0), run.plan(-1), notes, script.profile, opening)
        wake = at(script.day, script.wake).strftime("%H:%M")
        start = time_mod.time()
        reply = planner(prompt)
        raise_if_agy_failed(reply)
        run.latencies.append(round(time_mod.time() - start, 1))
        shown, block = split_reply(reply)
        items = parse_plan(block, script.day, script.wake)
        skipped = put_on_plan(run, items)
        run.log += [(wake, "user", opening), (wake, "assistant", shown)]
        run.facts = measure(run)
        run.facts["plan_items"] = len(items)
        run.facts["plan_items_skipped"] = skipped
        if not items:
            run.facts["plan_unreadable"] = True
        judge_day(run, judge, sim)
        run.end_reason = "planned"
    finally:
        set_debug_time(None)
    return run


def run_thirties(script: DayScript, tasks, notes, opening: str, judge, sim, engine) -> DayRun:
    """Thirties' Frontier brain on the same message and the same starting day, judged the same way."""
    run, _ = fresh_day(script, tasks, notes, engine, "thirties")
    try:
        wake = at(script.day, script.wake).strftime("%H:%M")
        run.log.append((wake, "user", opening))
        start = time_mod.time()
        before = len(run.manager.brain.messages)
        reply = run.manager.send_user_message(opening)
        run.log += [(wake, "tool", t) for t in tool_lines(run.manager.brain.messages[before:])]
        raise_if_agy_failed(reply)
        run.latencies.append(round(time_mod.time() - start, 1))
        run.log.append((wake, "assistant", reply))
        run.facts = measure(run)
        judge_day(run, judge, sim)
        run.end_reason = "planned"
    finally:
        set_debug_time(None)
    return run


def result_of(run: DayRun, opening: str) -> Dict[str, Any]:
    return {"script": run.script.id, "who": run.brain, "opening": opening, "end_reason": run.end_reason,
            "error": run.error, "requests_met": requests_met(run.judge), "judge": run.judge, "rating": run.rating,
            "facts": run.facts, "mean_latency": round(sum(run.latencies) / len(run.latencies), 1) if run.latencies
            else None, "log": run.log, "record": run.record_text() if run.scheduler else ""}


def run_script(script: DayScript, tasks, notes, sim, judge, planner, engine, pair: bool = True
               ) -> List[Dict[str, Any]]:
    """The bar and (paired) Thirties for one script, on one opening message."""
    set_debug_time(at(script.day, script.wake))
    try:
        opening = opening_message(script, sim)
    finally:
        set_debug_time(None)
    out = []
    for who in ("bar", "thirties") if pair else ("bar",):
        try:
            run = (run_bar(script, tasks, notes, opening, planner, judge, sim, engine) if who == "bar"
                   else run_thirties(script, tasks, notes, opening, judge, sim, engine))
        except QuotaExhausted:
            raise
        except Exception as e:
            logger.exception("%s %s failed", who, script.id)
            run = DayRun(script, who, error=f"{type(e).__name__}: {e}")
        out.append(result_of(run, opening))
    return out


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

def summary_lines(results: List[Dict[str, Any]]) -> List[str]:
    def mean(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return f"{sum(vals) / len(vals):.2f}" if vals else "–"
    lines = []
    for who in ("bar", "thirties"):
        rs = [r for r in results if r["who"] == who and not r["error"]]
        if rs:
            lines.append(f"{who}: {len(rs)} scripts | requests met {mean(r['requests_met'] for r in rs)} | user rating "
                         f"{mean(r['rating'].get('rating') for r in rs)}/10 | "
                         + " ".join(f"{d} {mean(r['judge'].get(d) for r in rs)}" for d in JUDGE_DIMS)
                         + f" | minute times {sum(r['facts'].get('minute_times_shown', 0) for r in rs)}")
    return lines


def write_report(results: List[Dict[str, Any]], out_dir: Path, label: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bar_{datetime.now().strftime('%Y%m%d_%H%M%S')}{'_' + label if label else ''}.json"
    out.write_text(json.dumps({"summary": summary_lines(results), "results": results}, indent=2, default=str))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--script", nargs="+", help="script ids to run")
    which.add_argument("--all", action="store_true", help="every script found")
    ap.add_argument("--scripts", nargs="*", default=[str(d) for d in SCRIPT_DIRS],
                    help="script files or directories (default: as the day sim)")
    ap.add_argument("--no-pair", action="store_true", help="the bar only, without Thirties on the same message")
    ap.add_argument("--lists-from", default="", help="leave out notes this database's list roles ignore")
    ap.add_argument("--out", default=str(REPORTS_DIR), help="report directory")
    ap.add_argument("--budget", type=float, default=0.66)
    ap.add_argument("--sim-model", default="gemini-3.8-flash-low")
    ap.add_argument("--frontier-model", default="gemini-3.8-flash-low")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="      [%(name)s] %(message)s")

    import tempfile
    from thirties_core.backlog import load_backlog
    from thirties_core.calendar_engine import MockCalendarEngine
    from thirties_core.config import ThirtiesConfig, load_config
    from thirties_core.inference import AntigravityInferenceEngine
    from thirties_core.joplin_engine import JoplinEngine
    from thirties_core.scheduler import DeterministicScheduler, StateDatabase
    scripts = find_scripts([Path(p) for p in args.scripts], None if args.all else args.script)
    cfg = load_config()
    sched = DeterministicScheduler(cfg, StateDatabase(Path(tempfile.mkdtemp()) / "s.sqlite"),
                                   MockCalendarEngine([]))
    tasks, notes = load_backlog(cfg, JoplinEngine(cfg), sched)
    if args.lists_from:
        from thirties_core.lists import ListCatalog
        catalog = ListCatalog(StateDatabase(Path(args.lists_from)))
        tasks = catalog.apply(tasks)
        ignored = {nid for nid, r in catalog.roles().items() if r["role"] == "ignore"}
        notes = [n for n in notes if n["id"] not in ignored]
    sim, judge, planner = make_llm(args.sim_model), make_llm(args.sim_model), make_llm(args.frontier_model)
    fcfg = ThirtiesConfig()
    fcfg.inference.gemini_model = args.frontier_model
    fcfg.inference.timeout_seconds = 180
    engine = AntigravityInferenceEngine(fcfg)
    used, allowed, capacity = budget_left(args.budget)
    need = len(scripts) * (CALLS_PER_SCRIPT["bar"] + (0 if args.no_pair else CALLS_PER_SCRIPT["thirties"]))
    print(f"{len(scripts)} scripts. agy: {used} calls in the last 5 h; budget {allowed} ({args.budget:.0%} of "
          f"~{capacity}); this run needs about {need}.", flush=True)
    results: List[Dict[str, Any]] = []
    stopped = ""
    for n, script in enumerate(scripts, 1):
        stop = quota_stop([args.sim_model, args.frontier_model], args.budget)
        if stop:
            stopped = f"stopped after {n - 1} scripts: {stop}"
            break
        print(f"\n== {script.id} ({n}/{len(scripts)})", flush=True)
        try:
            for r in run_script(script, tasks, notes, sim, judge, planner, engine, pair=not args.no_pair):
                results.append(r)
                print(f"  {r['who']}: requests met {r['requests_met']}; rating {r['rating'].get('rating')}; "
                      + ", ".join(f"{d} {r['judge'].get(d)}" for d in JUDGE_DIMS) + (f" ERROR {r['error']}"
                                                                                     if r["error"] else ""), flush=True)
        except QuotaExhausted as e:
            stopped = f"agy quota ran out during {script.id}: {e}"
            break
    print("\n=== Summary ===\n" + "\n".join(summary_lines(results)))
    if stopped:
        print(f"\n!!! {stopped}")
    out = write_report(results, Path(args.out), args.label)
    print(f"Report: {out}")


if __name__ == "__main__":
    main()
