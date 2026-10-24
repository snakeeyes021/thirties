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
import re
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
from thirties_core.inference import AntigravityInferenceEngine, agy_ledger
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
    hide_tasks: List[str] = field(default_factory=list)  # backlog titles this scenario assumes aren't there
    background_calendar: bool = True  # add synthetic_calendar's weekday standups and choir (not for day scripts)
    tags: List[str] = field(default_factory=list)  # goal kinds, for per-kind results


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


def new_items(between: Tuple[str, str], at_least: int, besides: Tuple[str, ...] = ()) -> Check:
    """At least N planned items within a window today, not counting work, sleep, calendar events or `besides` titles."""
    def check(run: "SimRun") -> Optional[str]:
        plan = run.plan(0)
        lo, hi = at(plan.target_date, between[0]), at(plan.target_date, between[1])
        found = [s.title for s in plan.spans_in_view() if not s.tentative and not s.is_envelope
                 and s.category not in (SpanCategory.WORK, SpanCategory.SLEEP, SpanCategory.CALENDAR)
                 and s.title not in besides and lo <= s.start_dt and s.end_dt <= hi + timedelta(minutes=30)]
        return None if len(found) >= at_least else f"only {len(found)} new item(s) within {between}: {found}"
    check.__doc__ = f"{at_least}+ new items {between}"
    return check


def memory_mentions(word: str) -> Check:
    def check(run: "SimRun") -> Optional[str]:
        services = run.manager.tools.services
        text = " ".join(n.text for n in services.memory.notes()) + " " + services.profile.as_markdown()
        return None if word.lower() in text.lower() else f"nothing about '{word}' in memory/profile"
    check.__doc__ = f"remembers {word}"
    return check


def replies(pattern: str, present: bool = True, list_lines_only: bool = False) -> Check:
    """Some assistant reply matches (or, with present=False, none matches) a regex. With
    list_lines_only, only lines of a list ("- ..." or "1. ...") are searched."""
    rx = re.compile(pattern, re.IGNORECASE)

    def check(run: "SimRun") -> Optional[str]:
        texts = [t for r, t, _ in run.transcript if r == "assistant"]
        if list_lines_only:
            texts = [l for t in texts for l in t.splitlines() if re.match(r"\s*(?:[-*•]|\d+\.)\s", l)]
        hits = [t for t in texts if rx.search(t)]
        if present:
            return None if hits else f"no reply matches /{pattern}/"
        return f"reply matches /{pattern}/: {hits[0][:120]!r}" if hits else None
    check.__doc__ = f"{'' if present else 'no '}reply /{pattern}/"
    return check


# Durations, chunk counts and block numbers inside a plan list: noise when start times already say it.
BUSY_LIST = r"\bblocks?\s+\d|\b\d+\s+(?:chunks?|thirties)\b|\(\d+\s*(?:h|hrs?|hours?|m|min|minutes)\b"

def no_busy_plan_lines() -> Check:
    """Lines listing planned items carry no durations, chunk counts or block numbers. Free-time lines
    ("- 7:00 PM – 8:00 PM (1 hour)") are exempt: there the length is the point."""
    def check(run: "SimRun") -> Optional[str]:
        for r, text, _ in run.transcript:
            if r != "assistant":
                continue
            for line in text.splitlines():
                if not re.match(r"\s*(?:[-*•]|\d+\.)\s", line) or not re.search(BUSY_LIST, line, re.IGNORECASE):
                    continue
                words = re.sub(r"\*|\([^)]*\)|\d{1,2}(:\d{2})?\s*(am|pm)?|[-–:•.]", " ", line, flags=re.IGNORECASE)
                words = re.sub(r"\b(tomorrow|tonight|today|until|from|to|free|open|available|time|chunks?|thirties|"
                               r"slots?|windows?|blocks?|hours?|minutes?|mins?)\b", " ", words, flags=re.IGNORECASE)
                if re.search(r"[a-z]{3,}", words, re.IGNORECASE):
                    return f"busy plan line: {line.strip()[:120]!r}"
        return None
    check.__doc__ = "no busy plan lines"
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
        checks=[order("shower", "upgrade", "chore"), order("lunch", "upgrade"), has_item("lunch", ("12:30", "13:30"))],
    ),
    SimScenario(
        "fill_afternoon", SAT, "13:00",
        persona="Matt, easygoing; wants the assistant to do the organizing. Skims replies; dislikes busy formatting.",
        situation="Saturday 1 PM, nothing planned until dinner around 6.",
        goals=["Ask the assistant to fill up your afternoon (until about 6) with things from your backlog.",
               "Accept its plan, or swap out one item you don't fancy.",
               "Ask to see the afternoon at a glance."],
        checks=[has_item("", ("13:00", "18:30")), no_busy_plan_lines()],
    ),
    SimScenario(
        "new_or_existing", THU, "13:00",
        persona="Matt; knows his own lists well and gets impatient when told he already has something he doesn't.",
        situation="Thursday 1 PM; work ends at 4. His Errands/Admin list has 'Message Brad about a weekend trip' "
                  "under Probation, which is NOT what he means: Brad just messaged him and he needs to reply.",
        hide_tasks=["Message Brad back"],
        goals=["Ask to make sure you message Brad back today, around 5.",
               "If the assistant thinks it's the weekend-trip item, say it's a different thing.",
               "Have it put on your Errands/Admin list as well, and scheduled at 5."],
        checks=[has_item("brad", ("16:30", "17:30"))],
    ),
    SimScenario(
        "fits_in_chunks", THU, "12:30",
        persona="Counts time in chunks (the app's 30-minute thirties) and expects the assistant to as well. Brief.",
        situation="Thursday 12:30 PM, a day off (work already cleared). The only thing this afternoon is groceries at 3.",
        setup=[("remove_event", {"event": "Work"}),
               ("add_event", {"title": "Groceries", "start": "3:00 PM", "end": "3:30 PM"})],
        goals=["Say a client call will take at least three chunks this afternoon, and ask where it could go.",
               "Pick one of the times offered and have it scheduled."],
        checks=[has_item("call|client", ("12:30", "19:00"), minimum_minutes=90), replies(r"\bchunks?\b")],
    ),
]


# ----------------------------------------------------------------------------
# Simulator and judge
# ----------------------------------------------------------------------------

class AgyError(RuntimeError):
    """agy kept failing. Not the brain's fault: the run is left out of the scores."""


class QuotaExhausted(AgyError):
    """agy says the usage quota is used up: stop the whole eval and report how far it got."""


AGY_ERROR_PREFIXES = ("Error contacting model", "Failed invoking", "The Antigravity assistant timed out")
QUOTA_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|too many requests|usage limit|limit (reached|exceeded)",
                      re.IGNORECASE)


# --- staying within a share of the agy quota (see docs/SLM_BRAIN.md, "Eval budget") ---

QUOTA_WINDOW_SECONDS = 5 * 3600   # agy's quota window
MEASURED_CAPACITY = 2000          # calls per window on flash-low: 1,113 calls showed as 55% used (2026-10-18)
CALLS_PER_CONVERSATION = {"slm": 6, "frontier": 9}  # simulator turns + judge (+ Frontier replies), measured


def quota_capacity() -> int:
    """Calls per window: the count before a quota stop in the last day, else the measured figure (older stops
    were measured with a different mix of models)."""
    rows = agy_ledger(86400)
    stops = [t for t, _, outcome in rows if outcome == "quota"]
    if not stops:
        return MEASURED_CAPACITY
    last = stops[-1]
    first_stop = min(t for t in stops if t > last - QUOTA_WINDOW_SECONDS)
    used = sum(1 for t, _, o in rows if first_stop - QUOTA_WINDOW_SECONDS <= t < first_stop and o != "quota")
    return max(used, 200)


def agy_quota() -> Dict[str, Dict[str, Tuple[float, str]]]:
    """agy's real quota, from its /quota command (free: no agent turn, no quota spent):
    {group name: {"5h" | "weekly": (fraction remaining, reset time)}}; {} if it can't be read."""
    import subprocess
    from thirties_core.inference import agy_workspace_dir, find_agy_binary
    agy = find_agy_binary(None)
    if not agy:
        return {}
    try:
        res = subprocess.run([agy, "-p", "/quota", "--output-format", "json"], capture_output=True, text=True,
                             timeout=60, cwd=agy_workspace_dir())
        data = json.loads(res.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}
    out: Dict[str, Dict[str, Tuple[float, str]]] = {}
    for group in ((data.get("command") or {}).get("data") or {}).get("groups", []):
        for bucket in group.get("buckets", []):
            out.setdefault(group["name"], {})[bucket["window"]] = (float(bucket["remaining_fraction"]),
                                                                    bucket.get("reset_time", ""))
    return out


def quota_group(model: str) -> str:
    """Which of agy's quota groups a model draws on."""
    if model.startswith("gemini-4-argon"):
        return "Gemini 4 Argon"
    if model.startswith(("claude", "gpt")):
        return "Claude and GPT models"
    return "Gemini Models"


def quota_stop(models: List[str], five_hour_share: float, weekly_share: float = 0.70) -> Optional[str]:
    """Why evals should stop now (a group's 5-hour or weekly use past its share), or None to carry on. Reads the
    real quota; if agy can't report it, falls back to the call-count estimate."""
    quota = agy_quota()
    if not quota:
        used, allowed, capacity = budget_left(five_hour_share)
        return None if used < allowed else f"estimated budget reached: {used} calls in 5 h (budget {allowed})"
    for group in {quota_group(m) for m in models}:
        for window, share in (("5h", five_hour_share), ("weekly", weekly_share)):
            remaining, reset = quota.get(group, {}).get(window, (1.0, ""))
            if 1 - remaining >= share:
                return f"{group} {window} quota {1 - remaining:.0%} used (limit for evals {share:.0%}; resets {reset})"
    return None


def budget_left(share: float) -> Tuple[int, int, int]:
    """(calls used in the current window, calls allowed by the budget, estimated capacity)."""
    capacity = quota_capacity()
    used = sum(1 for _, _, o in agy_ledger(QUOTA_WINDOW_SECONDS) if o != "quota")
    return used, int(capacity * share), capacity


def raise_if_agy_failed(text: str) -> None:
    if text.startswith(AGY_ERROR_PREFIXES):
        raise (QuotaExhausted if QUOTA_RE.search(text) else AgyError)(text[:400])


def make_llm(model: str, timeout: int = 180) -> Callable[[str], str]:
    cfg = ThirtiesConfig()
    cfg.inference.gemini_model = model
    cfg.inference.timeout_seconds = timeout
    engine = AntigravityInferenceEngine(cfg)

    def ask(prompt: str) -> str:
        for attempt in range(3):  # agy occasionally returns nothing or an error; try again
            # As written: wrapped as a chat turn ("write the assistant's next message"), the simulated user
            # sometimes answered as the assistant, or thought out loud about the two instructions.
            out = engine.complete(prompt)
            if out.strip() and not out.startswith(AGY_ERROR_PREFIXES):
                return out
            if QUOTA_RE.search(out):
                raise QuotaExhausted(out[:400])
            time_mod.sleep(5 * (attempt + 1))
        raise_if_agy_failed(out)
        raise AgyError("agy returned nothing three times")
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
 "clarity": 1-5 (are replies easy to read and shaped for the question? The user's own units (chunks if they count in chunks); plan lists one item per line led by start times, without durations, block numbers or ids the user didn't need?),
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
    checks: List[Dict[str, Any]] = field(default_factory=list)
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


class SimJoplinAPI:
    """Joplin's API in memory, seeded from the backlog, so list edits work in evals and never touch the
    user's real notes."""

    def __init__(self, tasks: List[TaskItem], real_notes: Optional[List[Dict[str, Any]]] = None) -> None:
        self.notes: Dict[str, Dict[str, str]] = {}
        for n in real_notes or []:  # the user's notes as they are: headings, ticked items, Today, routines
            self.notes[n["id"]] = {"id": n["id"], "body": n.get("body") or ""}
        for t in tasks:
            if t.parent_note_id in self.notes or t.id in self.notes:
                continue
            if t.is_checkbox_item and t.parent_note_id:
                note = self.notes.setdefault(t.parent_note_id, {"id": t.parent_note_id, "body": ""})
                note["body"] += f"- [ ] {t.title}\n"
            elif not t.id.startswith("media:"):
                self.notes.setdefault(t.id, {"id": t.id, "body": ""})

    def ping(self) -> bool:
        return True

    def get_note(self, note_id: str, fields: str = "") -> Optional[Dict[str, str]]:
        return dict(self.notes.setdefault(note_id, {"id": note_id, "body": ""}))

    def update_note(self, note_id: str, data: Dict[str, Any]) -> bool:
        self.notes.setdefault(note_id, {"id": note_id, "body": ""}).update(data)
        return True


class SimJoplin:
    """The Joplin connector over SimJoplinAPI: the notes Thirties reads, as edited during the sim."""

    def __init__(self, tasks: List[TaskItem], notes: List[Dict[str, Any]]) -> None:
        self.api_client = SimJoplinAPI(tasks, notes)
        self.meta = {n["id"]: n for n in notes}

    def fetch_active_notes(self) -> List[Dict[str, Any]]:
        return [dict(self.meta[nid], body=n["body"]) for nid, n in self.api_client.notes.items() if nid in self.meta]

    def fetch_tasks(self) -> List[TaskItem]:
        from thirties_core.joplin_engine import checklist_tasks
        return [t for n in self.fetch_active_notes()
                for t in checklist_tasks(n["id"], n["title"], n.get("source_notebook", ""), n["body"], [])]


def build_manager(scn: SimScenario, brain: str, tasks: List[TaskItem], notes: List[Dict], engine) -> Tuple[Any, Any]:
    cfg = load_config()
    cfg.assistant.brain = brain
    cfg.sync.enabled = False
    events = (synthetic_calendar(scn.day) if scn.background_calendar else []) + list(scn.extra_events)
    sched = DeterministicScheduler(cfg, StateDatabase(Path(tempfile.mkdtemp(prefix="sim_")) / "state.sqlite"),
                                   MockCalendarEngine(events))
    plan, pending = sched.build_day_plan(scn.day)
    hidden = {h.lower() for h in scn.hide_tasks}
    tasks = [TaskItem(**t.to_dict()) for t in tasks if t.title.lower() not in hidden]
    cfg.joplin.write_back, cfg.joplin.api_token = True, "sim"
    joplin = SimJoplin(tasks, notes)
    manager = ConversationManager(plan, tasks, pending, sched, inference_engine=engine, active_notes=notes)
    manager.tools.services.joplin = joplin
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
            from thirties_core.brains.slm.runtime import GemmaRuntime
            runtime = GemmaRuntime._shared
            calls_before = len(runtime.calls) if runtime is not None else 0
            start = time_mod.time()
            reply = run.manager.send_user_message(message)
            if brain == "frontier":
                raise_if_agy_failed(reply)
            run.transcript.append(("assistant", reply, round(time_mod.time() - start, 1)))
            runtime = GemmaRuntime._shared
            breakdown = ""
            if runtime is not None:
                breakdown = " [" + ", ".join(f"{c['kind']} {c['seconds']}s" for c in runtime.calls[calls_before:]) + "]"
            print(f"    U: {message[:160]}\n    A ({run.transcript[-1][2]}s{breakdown}): {reply[:300]}", flush=True)
        else:
            run.end_reason = "turn limit"
        for check in scn.checks:
            msg = check(run)
            run.checks.append({"check": check.__doc__ or "", "ok": msg is None})
            if msg:
                run.check_failures.append(f"{check.__doc__}: {msg}")
        verdict = judge(JUDGE_PROMPT.format(persona=scn.persona, situation=scn.situation, goals=goals,
                                            transcript=run.transcript_text(), schedule=run.schedule_text()))
        run.judge = extract_json(verdict) or {"raw": verdict[:500]}
    except QuotaExhausted:
        raise
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


def choose_scenarios(args) -> List[SimScenario]:
    scenarios = [] if args.generated and not args.with_handwritten else list(SCENARIOS)
    if args.generated:
        from tests.sim_generator import generate
        scenarios += generate(args.generated, seed=args.seed)
    only = set(args.only or [])
    for report in args.only_from or []:
        only |= {r["scenario"] for r in json.loads(Path(report).read_text()) if not r.get("error")}
    skip = set()
    for report in args.skip_from or []:
        skip |= {r["scenario"] for r in json.loads(Path(report).read_text()) if not r.get("error")}
    return [s for s in scenarios if (not only or s.id in only) and s.id not in skip]


def run_jobs(args, jobs) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run (scenario, brain, repeat) jobs in this process. Returns results and a quota message if agy ran out."""
    cfg = load_config()
    sched = DeterministicScheduler(cfg, StateDatabase(Path(tempfile.mkdtemp()) / "s.sqlite"), MockCalendarEngine([]))
    tasks, notes = load_backlog(cfg, JoplinEngine(cfg), sched)
    print(f"Backlog: {len(tasks)} tasks from {len(notes)} notes; {len(jobs)} conversations", flush=True)
    sim = make_llm(args.sim_model)
    judge = make_llm(args.sim_model)
    fcfg = ThirtiesConfig()
    fcfg.inference.gemini_model = args.frontier_model
    fcfg.inference.timeout_seconds = 180
    frontier_engine = AntigravityInferenceEngine(fcfg)
    results = []
    for n, (scn, brain, i) in enumerate(jobs, 1):
        models = [args.sim_model] + ([args.frontier_model] if brain == "frontier" else [])
        stop = quota_stop(models, args.budget) if n == 1 or n % 5 == 0 else None  # /quota every few conversations
        if stop:
            return results, f"stopped after {len(results)} finished conversations in this worker: {stop}"
        print(f"\n== {scn.id} [{brain}] {i}/{args.repeat}  ({n}/{len(jobs)})", flush=True)
        try:
            run = run_scenario(scn, brain, sim, judge, tasks, notes, frontier_engine)
        except QuotaExhausted as e:
            return results, f"agy quota ran out after {len(results)} finished conversations in this worker: {e}"
        gs = goal_score(run.judge)
        latencies = [t for r, _, t in run.transcript if r == "assistant"]
        print(f"  -> end: {run.end_reason}; goals {gs if gs is None else round(gs, 2)}; checks failed: "
              f"{run.check_failures or 'none'}; judge: { {k: run.judge.get(k) for k in JUDGE_DIMS} }"
              f"{'; ERROR ' + run.error if run.error else ''}", flush=True)
        if run.judge.get("issues"):
            print("  issues: " + " | ".join(map(str, run.judge["issues"])), flush=True)
        results.append({
            "scenario": scn.id, "brain": brain, "repeat": i, "tags": scn.tags, "end_reason": run.end_reason,
            "goal_score": gs, "checks": run.checks, "check_failures": run.check_failures, "judge": run.judge,
            "error": run.error, "turns": len(latencies),
            "mean_latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "persona": scn.persona, "situation": scn.situation, "goals": scn.goals,
            "transcript": run.transcript, "final_schedule": run.schedule_text() if run.scheduler else "",
        })
    return results, None


JUDGE_DIMS = ("accuracy", "understanding", "clarity", "efficiency", "tone")


def summarize(results: List[Dict[str, Any]], brains: List[str]) -> None:
    print("\n=== Summary ===")
    for brain in brains:
        rs = [r for r in results if r["brain"] == brain and not r["error"]]
        errors = sum(1 for r in results if r["brain"] == brain and r["error"])
        if not rs:
            continue
        gs = [r["goal_score"] for r in rs if r["goal_score"] is not None]
        checks_ok = sum(1 for r in rs if not r["check_failures"])
        all_checks = [c for r in rs for c in r.get("checks", [])]
        dims = {d: [r["judge"].get(d) for r in rs if isinstance(r["judge"].get(d), (int, float))] for d in JUDGE_DIMS}
        lat = [r["mean_latency"] for r in rs if r["mean_latency"]]
        gave_up = sum(1 for r in rs if str(r["end_reason"]).upper().startswith("GIVE UP"))
        print(f"{brain}: {len(rs)} conversations{f' (+{errors} errored)' if errors else ''} | "
              f"goals {sum(gs) / len(gs):.2f} | all checks passed {checks_ok}/{len(rs)} | "
              f"single checks {sum(c['ok'] for c in all_checks)}/{len(all_checks)} | gave up {gave_up} | "
              + " ".join(f"{d} {sum(v) / len(v):.1f}" for d, v in dims.items() if v)
              + (f" | latency {sum(lat) / len(lat):.1f}s" if lat else ""))
        kinds: Dict[str, List[float]] = {}
        for r in rs:
            for tag in r.get("tags") or []:
                kinds.setdefault(tag, []).append(r["goal_score"] or 0.0)
        kind_checks: Dict[str, List[bool]] = {}
        for c in all_checks:
            m = re.match(r"\[(\w+)\]", c["check"])
            if m:
                kind_checks.setdefault(m.group(1), []).append(c["ok"])
        if kinds:
            print("  by goal kind (conversations, mean goal score, checks passed):")
            for tag in sorted(kinds, key=lambda t: sum(kinds[t]) / len(kinds[t])):
                ks = kind_checks.get(tag, [])
                print(f"    {tag:<18} {len(kinds[tag]):>4}  {sum(kinds[tag]) / len(kinds[tag]):.2f}"
                      + (f"  {sum(ks)}/{len(ks)}" if ks else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", choices=["slm", "frontier", "both"], default="slm")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--generated", type=int, default=0, help="add N generated scenarios (tests/sim_generator.py)")
    ap.add_argument("--with-handwritten", action="store_true", help="with --generated, also run the hand-written ones")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only-from", nargs="*", metavar="REPORT", help="only the scenarios in these reports (to compare)")
    ap.add_argument("--skip-from", nargs="*", metavar="REPORT", help="skip scenarios in these reports (to finish a run)")
    ap.add_argument("--workers", type=int, default=1, help="parallel worker processes (each SLM worker loads Gemma)")
    ap.add_argument("--shard", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--budget", type=float, default=0.66,
                    help="stop before agy calls in the last 5 h exceed this share of the quota (default 0.66)")
    ap.add_argument("--sim-model", default="gemini-3.8-flash-low")
    ap.add_argument("--frontier-model", default="gemini-3.8-flash-low")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="      [%(name)s] %(message)s")
    logging.getLogger("thirties_core.brains.slm.brain").setLevel(logging.INFO)

    brains = ["slm", "frontier"] if args.brain == "both" else [args.brain]
    jobs = [(scn, brain, i + 1) for scn in choose_scenarios(args) for i in range(args.repeat) for brain in brains]
    quota_notes: List[str] = []
    if not args.shard:
        used, allowed, capacity = budget_left(args.budget)
        need = sum(CALLS_PER_CONVERSATION[b] for _, b, _ in jobs)
        print(f"agy: {used} calls in the last 5 h; budget {allowed} ({args.budget:.0%} of ~{capacity}). "
              f"This run needs about {need}" + ("." if used + need <= allowed else
                                                f": it will stop at the budget, after roughly "
                                                f"{max(0, allowed - used) * len(jobs) // max(need, 1)} conversations."),
              flush=True)
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        results, quota = run_jobs(args, jobs[k::n])
        Path(args.out).write_text(json.dumps({"results": results, "quota": quota}, default=str))
        sys.exit(3 if quota else 0)
    elif args.workers > 1:
        import subprocess
        log_dir = REPORTS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        procs = []
        for k in range(args.workers):
            out = log_dir / f"{stamp}_worker{k}.json"
            cmd = [sys.executable, "-m", "tests.sim_eval", *sys.argv[1:], "--shard", f"{k}/{args.workers}", "--out", str(out)]
            log = open(log_dir / f"{stamp}_worker{k}.log", "w")
            procs.append((subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT), out, log))
        print(f"{len(jobs)} conversations on {args.workers} workers; logs in {log_dir.relative_to(REPO_ROOT)}/{stamp}_worker*.log",
              flush=True)
        results = []
        for proc, out, log in procs:
            proc.wait()
            log.close()
            if out.exists():
                data = json.loads(out.read_text())
                results += data["results"]
                if data.get("quota"):
                    quota_notes.append(data["quota"])
            else:
                print(f"worker {out.name} crashed (exit {proc.returncode}); see its log", flush=True)
    else:
        results, quota = run_jobs(args, jobs)
        if quota:
            quota_notes.append(quota)

    summarize(results, brains)
    if quota_notes:
        print(f"\n!!! Stopped early (agy budget or quota): {len(results)} of {len(jobs)} conversations finished.")
        for q in quota_notes:
            print("    " + q)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}{'_' + args.label if args.label else ''}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"Report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
