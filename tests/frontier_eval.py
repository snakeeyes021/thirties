"""Frontier Brain eval: replayable "real day" scenarios against a live model.

Each scenario sets a simulated clock, sends one or more user turns through
ConversationManager with a real inference engine, and then checks the final
schedule deterministically: "what occupies 4:15 PM?", "is sleep untouched?".
Checks never grade wording. Transcripts are saved for an agent or human judge.

Not part of the unit suite (it needs network access and an API key):

    python3 -m tests.frontier_eval                      # all scenarios, gemini-3.8-flash
    python3 -m tests.frontier_eval --only work_ran_long --repeat 3
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.astronomy import set_debug_time
from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.conversation import ConversationManager
from thirties_core.inference import GeminiInferenceEngine
from thirties_core.models import TaskItem
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports" / "frontier"
TZ = ZoneInfo("America/New_York")
TRANSPORT_ERROR = "Encountered an error communicating with Gemini API"
DAY = date(2026, 10, 15)  # a Thursday


def at(hhmm: str, day_offset: int = 0) -> datetime:
    """Clock time on the scenario day (day_offset=1 for after midnight)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(DAY + timedelta(days=day_offset), time(h, m), tzinfo=TZ)


# ------------------------------------------------------------------------------
# Plan inspection, independent of how the core stores the schedule
# ------------------------------------------------------------------------------

def occupancy(plan, t: datetime) -> Tuple[str, str]:
    """Return (category, title) of what occupies instant t; ("open", "") if free.

    Categories: work, sleep, calendar, activity, open, outside (t not on this plan).
    """
    if hasattr(plan, "spans"):
        if not (plan.blocks[0].start_dt <= t < plan.blocks[-1].end_dt):
            return ("outside", "")
        covering = [s for s in plan.spans if s.start_dt <= t < s.end_dt and not getattr(s, "tentative", False)]
        if not covering:
            return ("open", "")
        # Prefer the most specific thing: activities and calendar items sit on top of work/sleep
        rank = {"calendar": 0, "task": 1, "activity": 1, "work": 2, "sleep": 3}
        s = sorted(covering, key=lambda s: rank.get(s.category.value, 1))[0]
        cat = "activity" if s.category.value == "task" else s.category.value
        return (cat, s.title)

    from thirties_core.models import BlockKind
    block = next((b for b in plan.blocks if b.start_dt <= t < b.end_dt), None)
    if block is None:
        return ("outside", "")
    if block.kind == BlockKind.BUSY_CALENDAR:
        return ("calendar", block.label)
    if block.label and block.label not in ("Work", "Sleep"):
        return ("activity", block.label)
    if block.kind == BlockKind.WORK:
        return ("work", "Work")
    if block.kind == BlockKind.SLEEP:
        return ("sleep", "Sleep")
    if block.kind == BlockKind.AMBIGUOUS_CALENDAR:
        return ("open", "")
    return ("open", "")


Check = Callable[["Run"], Optional[str]]  # returns None if OK, else a failure message


def occupied_by(hhmm: str, category: str, title_contains: str = "", day_offset: int = 0) -> Check:
    def check(run: "Run") -> Optional[str]:
        cat, title = occupancy(run.plan, at(hhmm, day_offset))
        if cat != category:
            return f"{hhmm}{'+1d' if day_offset else ''}: expected {category}, got {cat} ({title!r})"
        if title_contains and title_contains.lower() not in title.lower():
            return f"{hhmm}{'+1d' if day_offset else ''}: expected title containing {title_contains!r}, got {title!r}"
        return None
    check.__doc__ = f"{hhmm}{'+1d' if day_offset else ''} is {category}{' ~ ' + title_contains if title_contains else ''}"
    return check


def unchanged_outside(windows: List[Tuple[str, str]]) -> Check:
    """Every half hour outside the given windows looks the same as before the turns."""
    def inside(t: datetime) -> bool:
        return any(at(a) <= t < (at(b) if at(b) > at(a) else at(b, 1)) for a, b in windows)

    def check(run: "Run") -> Optional[str]:
        diffs = []
        for t, before in run.baseline.items():
            if inside(t):
                continue
            after = occupancy(run.plan, t)
            if after != before:
                diffs.append(f"{t.strftime('%a %I:%M %p')}: {before} -> {after}")
        return ("unexpected changes: " + "; ".join(diffs[:6])) if diffs else None
    check.__doc__ = f"nothing changed outside {windows}"
    return check


def no_changes() -> Check:
    c = unchanged_outside([])
    c.__doc__ = "schedule unchanged"
    return c


def finalized(expected: bool) -> Check:
    def check(run: "Run") -> Optional[str]:
        return None if bool(run.plan.is_finalized) == expected else f"is_finalized={run.plan.is_finalized}, expected {expected}"
    check.__doc__ = f"finalized={expected}"
    return check


# ------------------------------------------------------------------------------
# Scenarios
# ------------------------------------------------------------------------------

@dataclass
class Scenario:
    id: str
    description: str
    now: str  # simulated clock on DAY, "HH:MM"
    turns: List[str]
    checks: List[Check]
    events: List[CalendarEvent] = field(default_factory=list)
    setup: List[Tuple[str, dict]] = field(default_factory=list)  # (tool, args) run before the turns, bypassing the model


def _event(eid: str, summary: str, start: str, end: str, ambiguous: bool = False) -> CalendarEvent:
    return CalendarEvent(
        id=eid, summary=summary, start_dt=at(start), end_dt=at(end),
        is_ambiguous=ambiguous, response_status="needsAction" if ambiguous else "accepted",
    )


# Default day from config: work 8:30 AM - 4:00 PM, sleep 11:00 PM - 7:00 AM.
SCENARIOS: List[Scenario] = [
    Scenario(
        "work_ran_long", "Work overran; only the work end moves.",
        now="16:35",
        turns=["oh shoot, work actually went til 4:30 today"],
        checks=[occupied_by("16:15", "work"), occupied_by("16:45", "open"), occupied_by("23:30", "sleep"),
                occupied_by("06:30", "sleep", day_offset=1), unchanged_outside([("16:00", "16:30")])],
    ),
    Scenario(
        "work_starts_late", "Late start; work start moves, sleep untouched.",
        now="07:30",
        turns=["I'm starting work at 10 today instead of 8:30"],
        checks=[occupied_by("09:00", "open"), occupied_by("10:15", "work"), occupied_by("23:30", "sleep"),
                occupied_by("06:30", "sleep", day_offset=1)],
    ),
    Scenario(
        "day_off", "Day off clears work only.",
        now="07:45",
        turns=["I took the day off work today"],
        checks=[occupied_by("09:00", "open"), occupied_by("14:00", "open"), unchanged_outside([("08:30", "16:00")])],
    ),
    Scenario(
        "duration_task", "Duration converted by the system, not the model.",
        now="09:00",
        turns=["put 2 hours of composing in my evening starting at 5pm"],
        checks=[occupied_by("17:15", "activity", "compos"), occupied_by("18:45", "activity", "compos"),
                occupied_by("19:15", "open"), unchanged_outside([("17:00", "19:00")])],
    ),
    Scenario(
        "push_back", "Relative shift of an existing activity.",
        now="09:00",
        setup=[("modify_blocks", {"start_time": "5:00 PM", "end_time": "7:00 PM", "label": "Composing"})],
        turns=["push composing back an hour"],
        checks=[occupied_by("17:15", "open"), occupied_by("18:15", "activity", "compos"),
                occupied_by("19:45", "activity", "compos"), occupied_by("20:15", "open"),
                unchanged_outside([("17:00", "20:00")])],
    ),
    Scenario(
        "sleep_tonight", "Tonight's sleep window crosses midnight into tomorrow.",
        now="20:00",
        turns=["I'm going to bed at 10:30 tonight and getting up at 6:30 tomorrow"],
        checks=[occupied_by("22:00", "open"), occupied_by("22:45", "sleep"), occupied_by("03:00", "sleep", day_offset=1),
                occupied_by("06:15", "sleep", day_offset=1)],
    ),
    Scenario(
        "suggestion_no_mutation", "Asking for ideas must not change anything.",
        now="17:00",
        turns=["what should I do with my evening?"],
        checks=[no_changes()],
    ),
    Scenario(
        "calendar_protected", "A request overlapping a firm appointment keeps the appointment.",
        now="09:00",
        events=[_event("dentist", "Dentist", "15:00", "16:00")],
        turns=["I'm off work today. Schedule the gym from 2:30 to 4"],
        checks=[occupied_by("15:15", "calendar", "dentist"), occupied_by("14:45", "activity", "gym")],
    ),
    Scenario(
        "travel_buffer", "Buffer goes before the appointment.",
        now="09:00",
        events=[_event("dentist", "Dentist", "15:00", "16:00")],
        turns=["I'm off work today. Add 30 minutes of travel time before my dentist appointment"],
        checks=[occupied_by("14:45", "activity", "travel"), occupied_by("15:15", "calendar", "dentist"),
                occupied_by("14:15", "open")],
    ),
    Scenario(
        "cancel_and_replace", "Remove one activity and add another in one message.",
        now="09:00",
        setup=[("modify_blocks", {"start_time": "5:00 PM", "end_time": "7:00 PM", "label": "Composing"})],
        turns=["actually cancel composing, and put laundry at 7:30 for an hour instead"],
        checks=[occupied_by("17:30", "open"), occupied_by("18:30", "open"), occupied_by("19:45", "activity", "laundry"),
                occupied_by("20:15", "activity", "laundry"), occupied_by("20:45", "open")],
    ),
    Scenario(
        "attend_ambiguous", "Confirming an unconfirmed invite makes it firm.",
        now="09:00",
        events=[_event("dinner", "Team dinner", "19:00", "21:00", ambiguous=True)],
        turns=["yeah I'm going to the team dinner tonight"],
        checks=[occupied_by("19:30", "calendar", "dinner"), occupied_by("20:30", "calendar", "dinner")],
    ),
    Scenario(
        "negotiate_then_finalize", "Finalize only on explicit confirmation.",
        now="08:00",
        turns=["let's put a 30 minute walk at 5pm", "great, that's everything, lock it in"],
        checks=[occupied_by("17:15", "activity", "walk"), finalized(True)],
    ),
    Scenario(
        "no_premature_finalize", "Mid-negotiation must not finalize.",
        now="08:00",
        turns=["let's put a 30 minute walk at 5pm, I'm still thinking about the rest"],
        checks=[occupied_by("17:15", "activity", "walk"), finalized(False)],
    ),
]


# ------------------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------------------

@dataclass
class Run:
    scenario: Scenario
    plan: object = None
    baseline: dict = field(default_factory=dict)
    replies: List[str] = field(default_factory=list)
    transcript: list = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


def run_scenario(scn: Scenario, model: str) -> Run:
    run = Run(scn)
    set_debug_time(at(scn.now))
    try:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_model = model
        db = StateDatabase(Path(tempfile.mkdtemp(prefix="frontier_eval_")) / "state.sqlite")
        sched = DeterministicScheduler(config=cfg, state_db=db, calendar_engine=MockCalendarEngine(list(scn.events)))
        plan, ambiguous = sched.build_day_plan(DAY)
        tasks = [
            TaskItem(id="t_taxes", source_notebook="1. Tasks", title="File quarterly taxes"),
            TaskItem(id="t_bridge", source_notebook="3. Creative", title="Compose the bridge section", deferred_count=3),
            TaskItem(id="t_cables", source_notebook="4. Chores", title="Tidy studio cables"),
        ]
        mgr = ConversationManager(
            day_plan=plan, tasks=tasks, ambiguous_events=ambiguous, scheduler=sched,
            inference_engine=GeminiInferenceEngine(cfg), active_notes=[],
        )
        for tool, args in scn.setup:
            mgr.execute_tool(tool, args)
        mgr.brain.reset()  # system prompt reflects the setup state

        probe_times = [at("00:00") + timedelta(minutes=15 + 30 * i) for i in range(48)]
        probe_times += [t + timedelta(days=1) for t in probe_times if t.hour < 7]
        run.baseline = {t: occupancy(mgr.day_plan, t) for t in probe_times}

        start = time_mod.time()
        for turn in scn.turns:
            run.replies.append(mgr.send_user_message(turn))
            if any(TRANSPORT_ERROR in str(m.get("content", "")) for m in mgr.messages):
                raise RuntimeError("model transport failed: " + next(
                    str(m["content"]) for m in mgr.messages if TRANSPORT_ERROR in str(m.get("content", ""))))
        run.seconds = time_mod.time() - start
        run.plan = mgr.day_plan
        run.transcript = [m for m in mgr.messages if m.get("role") != "system"]
        for check in scn.checks:
            msg = check(run)
            if msg:
                run.failures.append(msg)
    except Exception as e:  # keep going; one broken scenario must not stop the batch
        run.error = f"{type(e).__name__}: {e}"
    finally:
        set_debug_time(None)
    return run


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--only", nargs="*", help="Scenario ids to run.")
    ap.add_argument("--repeat", type=int, default=1, help="Runs per scenario (models are not deterministic).")
    ap.add_argument("--label", default="", help="Tag for the report file name.")
    args = ap.parse_args()

    scenarios = [s for s in SCENARIOS if not args.only or s.id in args.only]
    results = []
    for scn in scenarios:
        for i in range(args.repeat):
            run = run_scenario(scn, args.model)
            ok = not run.failures and not run.error
            print(f"{'PASS' if ok else ('ERROR' if run.error else 'FAIL')}  {scn.id} [{i + 1}/{args.repeat}] ({run.seconds:.1f}s)")
            for f in run.failures:
                print(f"      - {f}")
            if run.error:
                print(f"      ! {run.error}")
            results.append({
                "scenario": scn.id, "description": scn.description, "repeat": i + 1, "pass": ok,
                "failures": run.failures, "error": run.error, "turns": scn.turns, "replies": run.replies,
                "transcript": run.transcript, "seconds": round(run.seconds, 1),
            })

    passed = sum(r["pass"] for r in results)
    errored = sum(bool(r["error"]) for r in results)
    print(f"\n{passed}/{len(results) - errored} passed ({args.model}); {errored} not scored due to errors")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"frontier_{stamp}{'_' + args.label if args.label else ''}.json"
    out.write_text(json.dumps({"model": args.model, "passed": passed, "total": len(results), "results": results},
                              indent=2, default=str))
    print(f"Report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
