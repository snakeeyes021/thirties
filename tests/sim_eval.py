"""Simulated-user evaluation: a large model plays the user, talks to a brain, and judges it.

For each scenario, a simulator (Gemini through agy) role-plays the user with a
persona and hidden goals, one message at a time, against the chosen brain
running on the real Joplin backlog and a synthetic calendar. When the
simulator is satisfied (or gives up, or the turn limit hits), a judge reads
the goals, the transcript and the final schedule and scores the result.
Deterministic checks run on the final schedule as well.

    ~/.local/share/thirties/venv/bin/python -m tests.sim_eval --brain slm
    ~/.local/share/thirties/venv/bin/python -m tests.sim_eval --brain frontier --only sunday_planning
    ... --brain both --repeat 2

Use the venv with litert-lm for the SLM brain. Reports go to docs/eval_reports/sim/.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.astronomy import set_debug_time
from thirties_core.backlog import load_backlog
from thirties_core.brains.slm.parse import extract_json
from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.clock import format_clock
from thirties_core.config import ThirtiesConfig, load_config
from thirties_core.conversation import ConversationManager
from thirties_core.inference import AntigravityInferenceEngine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.models import SpanCategory, SpanStatus, TaskItem
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports" / "sim"
TZ = ZoneInfo("America/New_York")
logger = logging.getLogger("sim_eval")


def at(day: date, hhmm: str, offset: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day + timedelta(days=offset), time(h, m), tzinfo=TZ)


def cal(day: date, eid: str, summary: str, start: str, end: str, invite: bool = False) -> CalendarEvent:
    return CalendarEvent(id=eid, summary=summary, start_dt=at(day, start), end_dt=at(day, end),
                         is_ambiguous=invite, response_status="needsAction" if invite else "accepted")


def synthetic_calendar(day: date) -> List[CalendarEvent]:
    """A plausible week: weekday standups, a dentist, a class, an invite."""
    events = []
    for offset in range(-3, 8):
        d = day + timedelta(days=offset)
        if d.weekday() < 5:
            events.append(cal(d, f"standup-{d}", "Team standup", "09:30", "09:45"))
        if d.weekday() == 1:
            events.append(cal(d, f"choir-{d}", "Choir rehearsal", "19:00", "21:00"))
    return events


# ----------------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------------

Check = Callable[["SimRun"], Optional[str]]


@dataclass
class SimScenario:
    id: str
    day: date
    now: str
    persona: str
    situation: str
    goals: List[str]
    checks: List[Check] = field(default_factory=list)
    extra_events: List[CalendarEvent] = field(default_factory=list)
    setup: List[Tuple[str, dict]] = field(default_factory=list)
    profile: Dict[str, str] = field(default_factory=dict)
    opening: str = ""
    max_turns: int = 10


def has_item(words: str, between: Tuple[str, str] = None, day_offset: int = 0, minimum_minutes: int = 0) -> Check:
    """Some planned item whose title contains any of the words (|-separated), optionally within a window."""
    options = [w.strip().lower() for w in words.split("|")]

    def check(run: "SimRun") -> Optional[str]:
        plan = run.plan(day_offset)
        found = [s for s in plan.spans_in_view() if any(o in s.title.lower() for o in options)
                 and not s.tentative and s.category not in (SpanCategory.WORK, SpanCategory.SLEEP)]
        if between:
            lo, hi = at(plan.target_date, between[0]), at(plan.target_date, between[1], 1 if between[1] < between[0] else 0)
            found = [s for s in found if lo <= s.start_dt and s.end_dt <= hi]
        if minimum_minutes:
            found = [s for s in found if s.duration_minutes >= minimum_minutes]
        return None if found else f"no '{words}' item" + (f" within {between}" if between else "") + \
            (f" on day+{day_offset}" if day_offset else "")
    check.__doc__ = f"has {words} {between or ''}"
    return check


def no_item(words: str, day_offset: int = 0) -> Check:
    options = [w.strip().lower() for w in words.split("|")]

    def check(run: "SimRun") -> Optional[str]:
        found = [s.title for s in run.plan(day_offset).spans_in_view() if any(o in s.title.lower() for o in options)]
        return f"unexpected item(s): {found}" if found else None
    check.__doc__ = f"no {words}"
    return check


def order(*words: str) -> Check:
    """Items appear in this order on today's schedule."""
    def check(run: "SimRun") -> Optional[str]:
        spans = [s for s in run.plan(0).spans_in_view() if not s.is_envelope]
        starts = []
        for w in words:
            match = [s for s in spans if any(o in s.title.lower() for o in w.lower().split("|"))]
            if not match:
                return f"missing {w}"
            starts.append(min(s.start_dt for s in match))
        return None if starts == sorted(starts) else f"order wrong: {list(zip(words, [format_clock(s) for s in starts]))}"
    check.__doc__ = f"order {words}"
    return check


def work_is(start: Optional[str], end: Optional[str]) -> Check:
    def check(run: "SimRun") -> Optional[str]:
        work = [s for s in run.plan(0).active_spans() if s.category == SpanCategory.WORK]
        if start is None:
            return f"work still on: {[format_clock(w.start_dt) for w in work]}" if work else None
        if len(work) != 1:
            return f"expected one work item, got {len(work)}"
        got = (format_clock(work[0].start_dt), format_clock(work[0].end_dt))
        return None if got == (start, end) else f"work is {got}, expected {(start, end)}"
    check.__doc__ = f"work {start}-{end}"
    return check


def outcome(words: str, status: str) -> Check:
    def check(run: "SimRun") -> Optional[str]:
        spans = run.scheduler.state_db.get_spans_between(at(run.scenario.day, "00:00"), at(run.scenario.day, "23:59"))
        match = [s for s in spans if any(o in s.title.lower() for o in words.lower().split("|"))]
        if any(s.status.value == status for s in match):
            return None
        done_tasks = run.scheduler.state_db.get_all_task_histories()
        if status == "done" and any(r.completed_date for r in done_tasks):
            return None
        return f"no '{words}' marked {status} (have {[(s.title, s.status.value) for s in match]})"
    check.__doc__ = f"{words} {status}"
    return check


def memory_mentions(word: str) -> Check:
    def check(run: "SimRun") -> Optional[str]:
        services = run.manager.tools.services
        text = " ".join(n.text for n in services.memory.notes()) + " " + services.profile.as_markdown()
        return None if word.lower() in text.lower() else f"nothing about '{word}' in memory/profile"
    check.__doc__ = f"remembers {word}"
    return check


THU = date(2026, 10, 15)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)

SCENARIOS: List[SimScenario] = [
    SimScenario(
        "evening_after_work", SAT, "19:35",
        persona="Matt: a composer and developer in his thirties. Casual, friendly, types in lowercase sometimes, "
                "thinks out loud, decides as he goes.",
        situation="Saturday evening, 7:35 PM. Nothing planned yet tonight; bedtime is around 11.",
        goals=[
            "Ask what the assistant would suggest for the evening, and react naturally to its ideas.",
            "Make dinner and eat from about 8 to 9, listening to the audiobook 'Dungeon Crawler Carl' at the same time.",
            "Do about an hour of listening/inspiration for Natalie's piece (a choral commission) after dinner.",
            "Then realize you must email a recruiter back and sort out January/February PTO (about an hour) and want "
            "it right after dinner, with the Natalie listening moved after it.",
            "Wind down with something non-active at the end if there's time (a movie or show).",
        ],
        checks=[has_item("dinner", ("19:30", "21:30")), has_item("carl"), has_item("recruiter|pto|email"),
                has_item("natalie|inspiration|listening"), order("dinner", "recruiter|pto|email", "natalie|inspiration")],
    ),
    SimScenario(
        "sunday_planning", SUN, "10:15",
        persona="Matt: still in his morning routine, at the computer, a bit scattered. Gives several requests at once, "
                "changes order when he notices something, wants a clean overview at the end.",
        situation="Sunday 10:15 AM. Nothing scheduled yet. Grocery store closes around 10 PM.",
        goals=[
            "About 3 thirties (1.5 hours) of 'assistant upgrades' (coding work on the Thirties app) first.",
            "Then about an hour of work chores: email a recruiter back and sort out early 2027 PTO.",
            "Then about another hour of assistant upgrades.",
            "Fit in lunch (30 min) and a shower (30 min) in the early afternoon; when adding lunch, everything after it "
            "should slide later rather than overlap.",
            "House chores (cleaning, trash, litter) for about an hour in the afternoon.",
            "Move the NAS and router (at least 1.5 hours) starting around when an Amazon package would arrive, ~4 PM.",
            "Dinner, then about an hour on Natalie's piece, in the early evening.",
            "Wedding thank-you notes for an hour tonight, and a grocery run (30 min) before the store closes.",
            "At the end, ask to see the whole day as a clean list.",
        ],
        checks=[has_item("upgrade", minimum_minutes=60), has_item("recruiter|pto|work chore"), has_item("lunch"),
                has_item("shower"), has_item("chore|clean"), has_item("nas|router", ("15:00", "19:00"), minimum_minutes=90),
                has_item("natalie"), has_item("thank"), has_item("grocer", ("17:00", "22:00")),
                order("upgrade", "recruiter|pto|work chore")],
    ),
    SimScenario(
        "work_ran_long", THU, "16:40",
        persona="Tired after a long workday; short messages.",
        situation="Thursday 4:40 PM. Work was scheduled 8:30 AM to 4 PM.",
        goals=["Tell the assistant work actually ran until 4:30.",
               "Ask how much free time is left before bed and plan a 30-minute walk soon."],
        checks=[work_is("8:30 AM", "4:30 PM"), has_item("walk", ("16:30", "19:00"))],
    ),
    SimScenario(
        "day_off", THU, "08:10",
        persona="Relaxed and happy; chatty.",
        situation="Thursday morning, 8:10 AM. Work is on the schedule 8:30 AM to 4 PM, but you took the day off.",
        goals=["Get work off today's schedule.", "Plan two hours of piano practice in the late morning.",
               "Plan lunch with a friend at 12:30 for an hour."],
        checks=[work_is(None, None), has_item("piano", ("09:00", "12:30"), minimum_minutes=120),
                has_item("lunch", ("12:00", "14:00"))],
    ),
    SimScenario(
        "checkin_outcomes", THU, "17:05",
        persona="Practical; answers check-in questions honestly.",
        situation="Thursday 5:05 PM. Earlier today the plan had taxes 2-3 PM and a recruiter email 3-3:30 PM.",
        setup=[("remove_event", {"event": "Work"}),
               ("add_event", {"title": "File quarterly taxes", "start": "2:00 PM", "end": "3:00 PM"}),
               ("add_event", {"title": "Email recruiter", "start": "3:00 PM", "end": "3:30 PM"})],
        opening="hey, quick update on this afternoon",
        goals=["Report that you did the taxes but they took until 3:30.",
               "Report that you didn't get to the recruiter email.",
               "Ask to put the recruiter email tomorrow morning at 9."],
        checks=[outcome("taxes", "done"), outcome("recruiter|email", "skipped"),
                has_item("recruiter|email", ("08:30", "10:00"), day_offset=1)],
    ),
    SimScenario(
        "probation_awareness", SAT, "14:00",
        persona="Matt; a little annoyed when suggestions miss the point, but polite.",
        situation="Saturday 2 PM, free afternoon.",
        goals=["Ask for suggestions for the afternoon.",
               "If anything from the 'Probation' section of your Errands/Admin note is suggested (asking the CB West choir "
               "director, messaging Brad about a weekend trip, Steve and Alyssa recommendations), point out that "
               "Probation means things you're not sure you'll ever do, and make sure the assistant will remember that.",
               "Accept one reasonable suggestion."],
        checks=[no_item("brad|choir director|steve and alyssa")],
    ),
    SimScenario(
        "calendar_conflict", THU, "09:00",
        persona="Busy, slightly impatient.",
        situation="Thursday 9 AM. Day off from work (already cleared). Dentist appointment 3-4 PM is on the calendar.",
        setup=[("remove_event", {"event": "Work"})],
        extra_events=[cal(THU, "dentist", "Dentist", "15:00", "16:00")],
        goals=["Ask for the gym from 2:30 to 4.", "Accept a sensible fix that keeps the dentist appointment.",
               "Add 30 minutes of travel time right before the dentist."],
        checks=[has_item("gym"), has_item("travel|drive|commute", ("14:00", "15:00"))],
    ),
    SimScenario(
        "invite_decision", THU, "12:00",
        persona="Friendly, brief.",
        situation="Thursday noon. There's an unconfirmed invite for a team dinner 7-9 PM tonight.",
        extra_events=[cal(THU, "dinner", "Team dinner", "19:00", "21:00", invite=True)],
        goals=["Ask what's on tonight.", "Confirm you're going to the team dinner.",
               "Plan 30 minutes to get ready right before it."],
        checks=[has_item("team dinner"), has_item("ready|shower|change", ("18:00", "19:00"))],
    ),
    SimScenario(
        "tomorrow_planning", THU, "20:00",
        persona="Organized; plans ahead.",
        situation="Thursday 8 PM, thinking about tomorrow (Friday, a workday 8:30-4).",
        goals=["Before work tomorrow, plan 30 minutes to call about the ENT appointment at 8 AM.",
               "Tomorrow after work, plan moving the NAS and router for at least 90 minutes.",
               "Check that nothing about tonight changed."],
        checks=[has_item("ent", ("07:00", "08:30"), day_offset=1), has_item("nas|router", ("16:00", "21:00"), 1, 90),
                no_item("nas|router|ent")],
    ),
    SimScenario(
        "long_term_goals", SAT, "10:00",
        persona="Reflective; talks about bigger plans.",
        situation="Saturday 10 AM, a quiet morning.",
        goals=["Tell the assistant you want to finish Natalie's piece by December and practice piano three times a week.",
               "Ask what you should focus on today given those goals, and plan one block for it."],
        checks=[memory_mentions("natalie"), memory_mentions("piano"), has_item("natalie|piano|compos")],
    ),
    SimScenario(
        "messy_rambler", THU, "17:30",
        persona="Stream-of-consciousness voice-to-text: long run-on messages, self-corrections, typos.",
        situation="Thursday 5:30 PM, work is over.",
        goals=["In one rambling message: dinner at 6:30 for an hour, then laundry, oh wait laundry after a 30 minute "
               "walk actually, and maybe an hour of reading at 9.",
               "Correct anything the assistant got wrong."],
        checks=[has_item("dinner", ("18:00", "20:00")), has_item("walk"), has_item("laundry"),
                has_item("read", ("20:30", "22:30")), order("dinner", "walk", "laundry")],
    ),
    SimScenario(
        "decision_fatigued", SAT, "15:00",
        persona="Exhausted, decision-fatigued; answers in one to three words ('idk', 'sure', 'you pick').",
        situation="Saturday 3 PM, free afternoon, low energy.",
        goals=["Let the assistant know you're wiped and don't want to decide.", "Accept what it proposes for the next couple of hours."],
        checks=[has_item("")],
    ),
    SimScenario(
        "swap_and_insert", SUN, "11:00",
        persona="Matt, mid-day adjustments, concise.",
        situation="Sunday 11 AM. Planned: Assistant upgrades 1-2 PM, Shower 2-2:30, House chores 2:30-3:30.",
        setup=[("add_event", {"title": "Assistant upgrades", "start": "1:00 PM", "end": "2:00 PM"}),
               ("add_event", {"title": "Shower", "start": "2:00 PM", "end": "2:30 PM"}),
               ("add_event", {"title": "House chores", "start": "2:30 PM", "end": "3:30 PM"})],
        goals=["Have the shower come before the assistant upgrades.",
               "Add 30 minutes of lunch at 1 PM, with everything after it sliding later.",
               "Ask to see the afternoon as a clean list."],
        checks=[order("lunch", "shower", "upgrade", "chore"), has_item("lunch", ("12:30", "13:30"))],
    ),
]


# ----------------------------------------------------------------------------
# Simulator and judge
# ----------------------------------------------------------------------------

def make_llm(model: str) -> Callable[[str], str]:
    cfg = ThirtiesConfig()
    cfg.inference.gemini_model = model
    cfg.inference.timeout_seconds = 180
    engine = AntigravityInferenceEngine(cfg)

    def ask(prompt: str) -> str:
        for attempt in range(3):  # agy occasionally returns nothing or an error; try again
            out = engine.chat([{"role": "user", "content": prompt}]).get("content", "")
            if out.strip() and not out.startswith(("Error contacting model", "Failed invoking", "The Antigravity assistant timed out")):
                return out
            time_mod.sleep(5 * (attempt + 1))
        return out
    return ask


SIM_PROMPT = """You are role-playing a real person texting their personal planning assistant app ("Thirties"). Stay in character.
Persona: {persona}
Situation: {situation}
Your goals for this conversation (the assistant does NOT know them; reveal them the way a real person would, usually one or two at a time, and only state facts given here or in your persona):
{goals}
Rules: write ONLY your next message to the assistant, as plain text, no quotes. Be natural for your persona. If the assistant gets something wrong, correct it. If it asks you something, answer. When every goal is met, reply with exactly: DONE. If the assistant keeps failing at the same thing three times, reply: GIVE UP: <short reason>.

Conversation so far:
{transcript}
Your next message:"""

JUDGE_PROMPT = """You are grading a personal planning assistant from a conversation with a user.
User persona: {persona}
Situation: {situation}
The user's hidden goals:
{goals}

Transcript:
{transcript}

The assistant's actual final schedule (ground truth; anything the assistant claims must match this):
{schedule}

Grade strictly and return ONLY a JSON object:
{{"goals": [{{"goal": <short>, "met": 0 | 0.5 | 1, "why": <short>}}...],
 "accuracy": 1-5 (do the assistant's statements match the real schedule? 5 = always),
 "understanding": 1-5 (did it grasp what the user meant, including corrections and vague references?),
 "clarity": 1-5 (are replies easy to read: clock times, line-separated lists, no jargon or ids?),
 "efficiency": 1-5 (how much effort did the user need? 5 = minimal back-and-forth),
 "tone": 1-5 (warm, appropriate, not pushy),
 "issues": [<short concrete problems>]}}"""


@dataclass
class SimRun:
    scenario: SimScenario
    brain: str
    manager: Any = None
    scheduler: Any = None
    transcript: List[Tuple[str, str, float]] = field(default_factory=list)
    end_reason: str = ""
    check_failures: List[str] = field(default_factory=list)
    judge: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def plan(self, day_offset: int = 0):
        return self.scheduler.build_day_plan(self.scenario.day + timedelta(days=day_offset))[0]

    def transcript_text(self) -> str:
        return "\n".join(f"{'User' if r == 'user' else 'Assistant'}: {t}" for r, t, _ in self.transcript) or "(nothing yet)"

    def schedule_text(self) -> str:
        lines = []
        for offset in (0, 1):
            plan = self.plan(offset)
            lines.append(f"{plan.target_date.strftime('%A %b %d')}:")
            for s in plan.spans_in_view():
                status = "" if s.status == SpanStatus.PLANNED else f" [{s.status.value}]"
                tent = " [unconfirmed invite]" if s.tentative else ""
                lines.append(f"  {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title}{status}{tent}")
        return "\n".join(lines)


def build_manager(scn: SimScenario, brain: str, tasks: List[TaskItem], notes: List[Dict], engine) -> Tuple[Any, Any]:
    cfg = load_config()
    cfg.assistant.brain = brain
    cfg.sync.enabled = False
    events = synthetic_calendar(scn.day) + list(scn.extra_events)
    sched = DeterministicScheduler(cfg, StateDatabase(Path(tempfile.mkdtemp(prefix="sim_")) / "state.sqlite"),
                                   MockCalendarEngine(events))
    plan, pending = sched.build_day_plan(scn.day)
    manager = ConversationManager(plan, [TaskItem(**t.to_dict()) for t in tasks], pending, sched,
                                  inference_engine=engine, active_notes=notes)
    for tool, args in scn.setup:
        manager.execute_tool(tool, args)
    for section, text in scn.profile.items():
        manager.tools.services.profile.set_section(section, text)
    if brain == "frontier":
        manager.brain.reset()
    return manager, sched


def run_scenario(scn: SimScenario, brain: str, sim, judge, tasks, notes, frontier_engine) -> SimRun:
    run = SimRun(scn, brain)
    set_debug_time(at(scn.day, scn.now))
    try:
        run.manager, run.scheduler = build_manager(scn, brain, tasks, notes, frontier_engine)
        goals = "\n".join(f"- {g}" for g in scn.goals)
        for turn in range(scn.max_turns):
            if turn == 0 and scn.opening:
                message = scn.opening
            else:
                message = sim(SIM_PROMPT.format(persona=scn.persona, situation=scn.situation, goals=goals,
                                                transcript=run.transcript_text())).strip().strip('"')
            if not message:
                run.end_reason = "simulator returned nothing"
                break
            if message.upper().startswith("DONE"):
                run.end_reason = "done"
                break
            if message.upper().startswith("GIVE UP"):
                run.end_reason = message
                break
            run.transcript.append(("user", message, 0.0))
            runtime = getattr(run.manager.brain, "_runtime", None)
            calls_before = len(runtime.calls) if runtime is not None else 0
            start = time_mod.time()
            reply = run.manager.send_user_message(message)
            run.transcript.append(("assistant", reply, round(time_mod.time() - start, 1)))
            runtime = getattr(run.manager.brain, "_runtime", None)
            breakdown = ""
            if runtime is not None:
                breakdown = " [" + ", ".join(f"{c['kind']} {c['seconds']}s" for c in runtime.calls[calls_before:]) + "]"
            print(f"    U: {message[:160]}\n    A ({run.transcript[-1][2]}s{breakdown}): {reply[:300]}", flush=True)
        else:
            run.end_reason = "turn limit"
        for check in scn.checks:
            msg = check(run)
            if msg:
                run.check_failures.append(msg)
        verdict = judge(JUDGE_PROMPT.format(persona=scn.persona, situation=scn.situation, goals=goals,
                                            transcript=run.transcript_text(), schedule=run.schedule_text()))
        run.judge = extract_json(verdict) or {"raw": verdict[:500]}
    except Exception as e:
        logger.exception("scenario %s failed", scn.id)
        run.error = f"{type(e).__name__}: {e}"
    finally:
        set_debug_time(None)
    return run


def goal_score(judge: Dict[str, Any]) -> Optional[float]:
    goals = judge.get("goals") or []
    vals = [float(g.get("met", 0)) for g in goals if isinstance(g, dict)]
    return sum(vals) / len(vals) if vals else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", choices=["slm", "frontier", "both"], default="slm")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--sim-model", default="gemini-3.8-flash-medium")
    ap.add_argument("--frontier-model", default="gemini-3.8-flash-low")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cfg = load_config()
    sched = DeterministicScheduler(cfg, StateDatabase(Path(tempfile.mkdtemp()) / "s.sqlite"), MockCalendarEngine([]))
    tasks, notes = load_backlog(cfg, JoplinEngine(cfg), sched)
    print(f"Backlog: {len(tasks)} tasks from {len(notes)} notes", flush=True)
    sim = make_llm(args.sim_model)
    judge = make_llm(args.sim_model)
    fcfg = ThirtiesConfig()
    fcfg.inference.gemini_model = args.frontier_model
    fcfg.inference.timeout_seconds = 180
    frontier_engine = AntigravityInferenceEngine(fcfg)

    brains = ["slm", "frontier"] if args.brain == "both" else [args.brain]
    scenarios = [s for s in SCENARIOS if not args.only or s.id in args.only]
    results = []
    for scn in scenarios:
        for brain in brains:
            for i in range(args.repeat):
                print(f"\n== {scn.id} [{brain}] {i + 1}/{args.repeat}", flush=True)
                run = run_scenario(scn, brain, sim, judge, tasks, notes, frontier_engine)
                gs = goal_score(run.judge)
                latencies = [t for r, _, t in run.transcript if r == "assistant"]
                print(f"  -> end: {run.end_reason}; goals {gs if gs is None else round(gs, 2)}; checks failed: "
                      f"{run.check_failures or 'none'}; judge: { {k: run.judge.get(k) for k in ('accuracy', 'understanding', 'clarity', 'efficiency', 'tone')} }"
                      f"{'; ERROR ' + run.error if run.error else ''}", flush=True)
                if run.judge.get("issues"):
                    print("  issues: " + " | ".join(map(str, run.judge["issues"])), flush=True)
                results.append({
                    "scenario": scn.id, "brain": brain, "repeat": i + 1, "end_reason": run.end_reason,
                    "goal_score": gs, "check_failures": run.check_failures, "judge": run.judge, "error": run.error,
                    "turns": len(latencies), "mean_latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
                    "transcript": run.transcript, "final_schedule": run.schedule_text() if run.scheduler else "",
                })

    print("\n=== Summary ===")
    for brain in brains:
        rs = [r for r in results if r["brain"] == brain and not r["error"]]
        if not rs:
            continue
        gs = [r["goal_score"] for r in rs if r["goal_score"] is not None]
        checks_ok = sum(1 for r in rs if not r["check_failures"])
        dims = {d: [r["judge"].get(d) for r in rs if isinstance(r["judge"].get(d), (int, float))]
                for d in ("accuracy", "understanding", "clarity", "efficiency", "tone")}
        lat = [r["mean_latency"] for r in rs if r["mean_latency"]]
        print(f"{brain}: goals {sum(gs) / len(gs):.2f} | checks passed {checks_ok}/{len(rs)} | "
              + " ".join(f"{d} {sum(v) / len(v):.1f}" for d, v in dims.items() if v)
              + (f" | latency {sum(lat) / len(lat):.1f}s" if lat else ""))
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}{'_' + args.label if args.label else ''}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"Report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
