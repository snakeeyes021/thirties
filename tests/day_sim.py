"""Day-long simulated-user eval: a whole day with the clock running, the way the app is really used.

The short sim (tests/sim_eval.py) freezes the clock at one moment and gives the
simulated user two to four crisp goals. Real days aren't like that: time passes,
things run late, the app's own check-ins and start messages arrive, constraints
come out only when the plan breaks them, and the user reports what they did late
and vaguely. This harness runs that.

A day script (JSON; see tests/day_scripts/) holds the persona, how the day
starts, the hidden course of the day ("life"), hidden constraints, and the
ground truth of what the user actually did and when. The simulated user (Gemini
through agy) lives the day: after each move it says how many minutes pass before
its next message. The harness advances the clock in five-minute steps and runs
the real background service, so check-ins and start messages reach the user as
they would on a phone; one arriving interrupts the wait.

Scored deterministically (minute times shown, app messages per waking hour and
repeats, internal text leaking into replies, merged items, recorded outcomes
against the ground truth) and by a judge over the whole day, plus the simulated
user's own rating of their day with the app.

    ~/.local/share/thirties/venv/bin/python -m tests.day_sim --brain frontier
    ... --brain both --scripts tests/day_scripts docs/eval_reports/day_scripts --workers 2

Scripts derived from the user's real conversations are made by tests/day_scripts.py
and kept in docs/eval_reports/day_scripts/ (gitignored). Reports go to
docs/eval_reports/day/.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.sim_eval import (QuotaExhausted, SimScenario, at, budget_left, build_manager, cal, make_llm,
                            quota_stop, raise_if_agy_failed)
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.backlog import load_backlog
from thirties_core.brains.slm.parse import extract_json
from thirties_core.calendar_engine import MockCalendarEngine
from thirties_core.clock import format_clock
from thirties_core.config import ThirtiesConfig, load_config
from thirties_core.inference import AntigravityInferenceEngine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.models import SpanCategory, SpanStatus, TaskItem
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports" / "day"
SCRIPT_DIRS = [REPO_ROOT / "tests" / "day_scripts", REPO_ROOT / "tests" / "day_scripts" / "people",
               REPO_ROOT / "docs" / "eval_reports" / "day_scripts"]
STEP = timedelta(minutes=5)
MAX_MOVES = 160         # simulator decisions per day (waits included)
MAX_MESSAGES = 80       # user messages per day
CALLS_PER_DAY = {"slm": 60, "frontier": 100}  # agy calls: simulator moves + judge + rating (+ Frontier replies); measured ~37 and ~63
logger = logging.getLogger("day_sim")


# ----------------------------------------------------------------------------
# Day scripts
# ----------------------------------------------------------------------------

@dataclass
class DayScript:
    id: str
    day: date
    wake: str
    bed: str
    persona: str
    situation: str
    life: List[str]
    constraints: List[str] = field(default_factory=list)
    truth: List[Dict[str, Any]] = field(default_factory=list)  # {"item", "match", "start", "end", "done"}
    work: Optional[List[str]] = None       # ["08:30", "16:00"]; None: no work today
    events: List[Dict[str, Any]] = field(default_factory=list)  # {"summary", "start", "end", "invite"}
    setup: List[List[Any]] = field(default_factory=list)        # [tool, args] before the day starts
    profile: Dict[str, str] = field(default_factory=dict)
    source: str = ""
    # The person's own lists, if not the user's real Joplin: {"notebook", "title", "role", "body" (markdown)}
    notes: List[Dict[str, str]] = field(default_factory=list)
    message_level: str = ""  # off / light / medium / heavy, as this person would set it (asked once if missing)

    def own_backlog(self) -> Tuple[List[TaskItem], List[Dict[str, Any]]]:
        """Tasks and notes from the script's own lists (note ids are made up and stable)."""
        from thirties_core.joplin_engine import checklist_tasks
        notes = [{"id": f"{self.id}-note{i}", "source_notebook": n.get("notebook", "Lists"), "title": n["title"],
                  "body": n.get("body", ""), "is_todo": False, "todo_completed": 0}
                 for i, n in enumerate(self.notes)]
        tasks = [t for n in notes for t in checklist_tasks(n["id"], n["title"], n["source_notebook"], n["body"],
                                                             ["Someday", "Maybe", "Someday/Maybe"])]
        return tasks, notes

    @classmethod
    def load(cls, path: Path) -> "DayScript":
        data = json.loads(path.read_text())
        data["day"] = date.fromisoformat(data.pop("date"))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def scenario(self) -> SimScenario:
        setup = [(tool, args) for tool, args in self.setup]
        if self.work is None:
            setup.insert(0, ("remove_event", {"event": "Work"}))
        else:
            setup.insert(0, ("update_event", {"event": "Work", "start": self.work[0], "end": self.work[1]}))
        events = [cal(self.day, f"day-{i}", e["summary"], e["start"], e["end"], invite=bool(e.get("invite")))
                  for i, e in enumerate(self.events)]
        return SimScenario(self.id, self.day, self.wake, self.persona, self.situation, goals=[],
                           extra_events=events, setup=setup, profile=dict(self.profile),
                           background_calendar=False)  # a script's calendar is exactly its own events


HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(data: Dict[str, Any]) -> List[str]:
    """What's wrong with a script (an empty list if nothing): for scripts written elsewhere."""
    problems = []
    for key in ("id", "date", "wake", "bed", "persona", "situation", "life", "truth"):
        if not data.get(key):
            problems.append(f"missing {key}")
    try:
        date.fromisoformat(str(data.get("date")))
    except ValueError:
        problems.append(f"date {data.get('date')!r} isn't YYYY-MM-DD")
    for key in ("wake", "bed"):
        if data.get(key) and not HHMM.match(str(data[key])):
            problems.append(f"{key} {data[key]!r} isn't HH:MM")
    for beat in data.get("life") or []:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d\b", str(beat)):
            problems.append(f"life beat without a time first: {str(beat)[:40]!r}")
    for t in data.get("truth") or []:
        if not t.get("item"):
            problems.append("truth item without a name")
        for key in ("start", "end"):
            if t.get(key) and not HHMM.match(str(t[key])):
                problems.append(f"truth {t.get('item')!r} {key} {t[key]!r} isn't HH:MM")
    work = data.get("work")
    if work is not None and not (isinstance(work, list) and len(work) == 2 and all(HHMM.match(str(w)) for w in work)):
        problems.append(f"work {work!r} isn't null or [HH:MM, HH:MM]")
    for e in data.get("events") or []:
        if not (e.get("summary") and HHMM.match(str(e.get("start", ""))) and HHMM.match(str(e.get("end", "")))):
            problems.append(f"event {e!r} needs summary, start and end (HH:MM)")
    roles = {"routine", "today", "menu", "list", "project", "backlog", "reference", "ignore"}
    for n in data.get("notes") or []:
        if not n.get("title") or "body" not in n:
            problems.append(f"note {str(n)[:40]!r} needs a title and a body")
        if n.get("role") and n["role"] not in roles:
            problems.append(f"note {n.get('title')!r} role {n['role']!r} isn't one of {sorted(roles)}")
    return problems


def find_scripts(dirs: List[Path], only: Optional[List[str]] = None) -> List[DayScript]:
    scripts = []
    for d in dirs:
        if not Path(d).exists():
            continue
        for path in sorted(Path(d).glob("*.json")) if Path(d).is_dir() else [Path(d)]:
            script = DayScript.load(path)
            if not only or script.id in only:
                scripts.append(script)
    return scripts


# ----------------------------------------------------------------------------
# The simulated user and the judge
# ----------------------------------------------------------------------------

DAY_SIM_PROMPT = """You are role-playing a real person through one whole day, texting their personal planning assistant app ("Thirties") from their phone. Stay in character.
Persona: {persona}
How the day starts: {situation}

Your day, as it will really go. The assistant does NOT know any of this. It unfolds as time passes: only what's at or before the current time has happened yet.
{life}

Things that are true for you that you don't spell out unless they matter (say one when the assistant's plan breaks it, or when you naturally would):
{constraints}

It is now {now}. You go to bed around {bed}.

The conversation so far. Times are when each message was sent. "App" lines are the app's own notifications; answer or ignore them as a real person would.
{transcript}

Decide your next move. Return ONLY a JSON object:
{{"wait_minutes": <minutes from now until your next message; 0 to reply right away>, "message": <your next message as you'd type it, or null to send nothing and let time pass>, "end_of_day": <true once you're going to bed and done with the app today>}}
Rules:
- Text like a real person on a phone: often short, sometimes several things in one message, sometimes vague or late about times ("started about half an hour ago").
- Live your day as written. Report what you did when you naturally would or when asked, with your day's real times, rounded as a person would.
- If the assistant gets something wrong, forgets something, or nags you, react the way your persona would.
- Don't fill time with messages: if nothing is due, wait until the next thing in your day (wait_minutes up to 180)."""

DAY_JUDGE_PROMPT = """You are grading a personal planning assistant over one whole day of real use, from its conversation with the user.
User persona: {persona}
How the day started: {situation}
How the day really went (hidden from the assistant):
{life}
The user's hidden constraints:
{constraints}

The conversation, with times. "App" lines are the app's own notifications; "(app did)" lines are what the app actually did (its tool calls and their results), which the user didn't see: use them to check what the assistant claims.
{transcript}

The final record (what the assistant's schedule says, with outcomes; ground truth for anything the assistant claimed):
{schedule}

Measured facts: {facts}

Grade strictly. Return ONLY a JSON object:
{{"requests": [{{"request": <short: something the user asked for>, "met": 0 | 0.5 | 1, "why": <short>}}...],
 "accuracy": 1-5 (do the assistant's statements match the schedule and what the user said?),
 "understanding": 1-5 (did it grasp what the user meant, including corrections, vague references and constraints once stated?),
 "plan_respect": 1-5 (did it change the plan only when asked or agreed, without shuffling things the user didn't mention?),
 "message_load": 1-5 (5 = the app's own messages were the right amount and useful; 1 = nagging, repeated or pointless),
 "record_keeping": 1-5 (did it track what actually happened, and when, from the user's reports?),
 "clarity": 1-5 (replies easy to read, shaped for the question, times in whole and half hours unless exact minutes were asked for),
 "tone": 1-5 (warm, appropriate, not pushy),
 "worst_moments": [<short: the moments a real user would be most annoyed>],
 "issues": [<short concrete problems>]}}"""

RATING_PROMPT = """You are this person: {persona}
You just spent a day using a planning assistant app. Here is the whole conversation:
{transcript}

Honestly, in character: how was it? Return ONLY a JSON object:
{{"rating": 1-10 (would you keep using it?), "annoyances": [<up to three, in your words>], "liked": [<up to two>]}}"""


# ----------------------------------------------------------------------------
# Running a day
# ----------------------------------------------------------------------------

@dataclass
class DayRun:
    script: DayScript
    brain: str
    message_level: str = ""
    manager: Any = None
    scheduler: Any = None
    service: Any = None
    log: List[Tuple[str, str, str]] = field(default_factory=list)  # (HH:MM, who, text); who: user|assistant|app
    latencies: List[float] = field(default_factory=list)
    end_reason: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)
    judge: Dict[str, Any] = field(default_factory=dict)
    rating: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def transcript_text(self, for_user: bool = False) -> str:
        """The conversation; for the judge, with what the app actually did (its tool calls) in brackets."""
        names = {"user": "You" if for_user else "User", "assistant": "Assistant", "app": "App", "tool": "(app did)"}
        return "\n".join(f"[{t}] {names[w]}: {x}" for t, w, x in self.log
                         if not (for_user and w == "tool")) or "(nothing yet)"

    def plan(self, offset: int = 0):
        return self.scheduler.build_day_plan(self.script.day + timedelta(days=offset))[0]

    def record_text(self) -> str:
        lines = []
        for offset in (0, 1):
            plan = self.plan(offset)
            lines.append(f"{plan.target_date.strftime('%A %b %d')}:")
            for s in plan.spans_in_view():
                status = "" if s.status == SpanStatus.PLANNED else f" [{s.status.value}]"
                actual = ""
                if s.actual_start or s.actual_end:
                    actual = (f" (actually {format_clock(s.actual_start) if s.actual_start else '?'}"
                              f" – {format_clock(s.actual_end) if s.actual_end else '?'})")
                lines.append(f"  {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title}{status}{actual}")
        return "\n".join(lines)


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


LEVEL_PROMPT = """You are this person: {persona}

You've just installed a planning assistant app. In its settings it asks how often it should message you:
- Off: never (only pings you ask for)
- Light: a heads-up shortly before each calendar event
- Medium: what's planned at the start of each stretch of the day, and one check-in at its end
- High: when each thing starts, and a check-in after each one

Which would you pick? Answer with one word: Off, Light, Medium or High."""
LEVELS_FILE = REPO_ROOT / "docs" / "eval_reports" / "day_scripts" / "message_levels.json"


def message_level(script: DayScript, sim) -> str:
    """The message level this person chose, like a real user at setup: from the script, else asked once and
    kept (docs/eval_reports/day_scripts/message_levels.json), so paired runs use the same level."""
    if script.message_level:
        return script.message_level
    known = json.loads(LEVELS_FILE.read_text()) if LEVELS_FILE.exists() else {}
    if script.id not in known and sim is not None:
        answer = (sim(LEVEL_PROMPT.format(persona=script.persona)) or "").strip().lower()
        level = next((k for word, k in (("off", "off"), ("light", "light"), ("medium", "medium"), ("high", "heavy"))
                      if word in answer), "medium")
        known[script.id] = level
        LEVELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEVELS_FILE.write_text(json.dumps(known, indent=2, sort_keys=True))
    return known.get(script.id, "medium")


def start_day(script: DayScript, brain: str, tasks, notes, engine, lists_db: str = "", sim=None) -> DayRun:
    run = DayRun(script, brain)
    set_debug_time(at(script.day, script.wake))
    if script.notes:  # someone else's life: their own lists, with the roles they'd confirm at setup
        tasks, notes = script.own_backlog()
        lists_db = ""
    run.manager, run.scheduler = build_manager(script.scenario(), brain, tasks, notes, engine)
    if script.notes:
        try:
            from thirties_core.lists import ListCatalog
            catalog = ListCatalog(run.scheduler.state_db)
            for n, given in zip(notes, script.notes):
                if given.get("role"):
                    catalog.set_role(n["id"], given["role"], n["title"], source="user")
            run.manager.tools.services.tasks = catalog.apply(run.manager.tools.services.tasks)
        except ImportError:
            pass  # a version without list roles
    if lists_db:  # the lists sorted once (tests/day_scripts.py lists), as if the app had sorted them
        try:
            from thirties_core.lists import ListCatalog
            mine = ListCatalog(run.scheduler.state_db)
            for record in ListCatalog(StateDatabase(Path(lists_db))).all_records():
                mine.merge_record(record)
        except ImportError:
            pass  # a version without list roles
    cfg = run.scheduler.config
    cfg.assistant.message_level = run.message_level = message_level(script, sim)  # chosen by the person
    cfg.assistant.checkins = True
    cfg.telegram.notify_block_transitions = True

    def notify(_title: str, body: str, _kind: str) -> None:
        run.log.append((_hhmm(run.service.now()), "app", body))

    joplin = run.manager.tools.services.joplin
    run.service = BackgroundService(cfg, notify, scheduler=run.scheduler, joplin_engine=joplin,
                                    inference_engine=engine, sync_engine=SimpleNamespace(sync=lambda: None))
    run.service.today = lambda: script.day  # the script's day throughout (before sunrise is still "yesterday")
    run.service._manager, run.service._day = run.manager, script.day
    return run


def advance(run: DayRun, now: datetime, until: datetime) -> datetime:
    """Let time pass to ``until``, running the background service every five minutes. Stops early, at
    that time, if the app sent the user something."""
    before = len(run.log)
    t = now - timedelta(minutes=now.minute % 5, seconds=now.second, microseconds=now.microsecond) + STEP
    while t <= until:
        set_debug_time(t)
        run.service.tick(t)
        if len(run.log) > before:
            return t
        t += STEP
    set_debug_time(until)
    return until


def tool_lines(messages) -> List[str]:
    """What the app did in a turn: tool calls and their results (Gemini), or the actions run (SLM)."""
    out = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            out.append(f"{call.get('name')} {json.dumps(call.get('arguments', {}), ensure_ascii=False)[:200]}")
        if m.get("role") == "tool":
            out.append(f"→ {str(m.get('content', ''))[:200]}")
        for action in m.get("slm_actions") or []:
            out.append(json.dumps(action, ensure_ascii=False)[:200])
    return out


def live_day(run: DayRun, sim, max_moves: int = MAX_MOVES) -> None:
    script = run.script
    now = at(script.day, script.wake)
    bed = at(script.day, script.bed, 1 if script.bed < script.wake else 0)
    life = "\n".join(f"- {b}" for b in script.life)
    constraints = "\n".join(f"- {c}" for c in script.constraints) or "(none)"
    messages = 0
    run.service.tick(now)  # anything due at wake-up (the morning rollover)
    for _ in range(max_moves):
        raw = sim(DAY_SIM_PROMPT.format(persona=script.persona, situation=script.situation, life=life,
                                        constraints=constraints, now=format_clock(now), bed=format_clock(bed),
                                        transcript=run.transcript_text(for_user=True)))
        move = extract_json(raw) or {}
        if not move:
            logger.warning("simulator move wasn't JSON: %r", raw[:200])
            continue
        wait = max(0, min(int(move.get("wait_minutes") or 0), 240))
        if not wait and not (move.get("message") or "").strip() and not move.get("end_of_day"):
            wait = 15  # neither talking nor waiting: let some time pass
        target = min(now + timedelta(minutes=wait), bed)
        reached = advance(run, now, target) if target > now else now
        if reached < target:  # the app interrupted the wait: decide again with its message in view
            now = reached
            continue
        now = reached
        text = (move.get("message") or "").strip()
        if text:
            run.log.append((_hhmm(now), "user", text))
            start = time_mod.time()
            before = len(run.manager.brain.messages)
            reply = run.manager.send_user_message(text)
            run.log += [(_hhmm(now), "tool", t) for t in tool_lines(run.manager.brain.messages[before:])]
            if run.brain == "frontier":
                raise_if_agy_failed(reply)
            run.latencies.append(round(time_mod.time() - start, 1))
            run.log.append((_hhmm(now), "assistant", reply))
            print(f"    [{_hhmm(now)}] U: {text[:150]}\n            A ({run.latencies[-1]}s): {reply[:220]}", flush=True)
            messages += 1
        if move.get("end_of_day") or now >= bed:
            run.end_reason = "end of day"
            return
        if messages >= MAX_MESSAGES:
            run.end_reason = "message limit"
            return
    run.end_reason = "move limit"


# ----------------------------------------------------------------------------
# Measuring the day
# ----------------------------------------------------------------------------

CLOCK_RE = re.compile(r"\b(1[0-2]|0?[1-9]):([0-5]\d)\b")
EXACT_ASKED_RE = re.compile(r"exact|minute|precise|to the min", re.IGNORECASE)
LEAK_RE = re.compile(r"\bCALL [a-z_]+ ?\{|(?i:NOT_DONE|\bid[= ][0-9a-f]{6}|\[id |for the assistant only|\[System check|"
                     r"tool result|the (previous|last) turn|conversation history)")  # "Call the landlord" is fine
JOINED_RE = re.compile(r"\s/\s|\s&\s")


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2]


def measure(run: DayRun) -> Dict[str, Any]:
    script = run.script
    facts: Dict[str, Any] = {}
    leaks, internal = [], []
    asked_exact = False
    for t, who, text in run.log:
        if who == "tool":
            continue
        if who == "user":
            asked_exact = bool(EXACT_ASKED_RE.search(text))
            continue
        if not asked_exact:
            leaks += [f"[{t}] {m.group(0)}" for m in CLOCK_RE.finditer(text) if m.group(2) not in ("00", "30")]
        if who == "assistant" and LEAK_RE.search(text):
            internal.append(f"[{t}] {text[:120]}")
    facts["minute_times_shown"] = len(leaks)
    facts["minute_time_examples"] = leaks[:8]
    facts["internal_text_in_replies"] = internal[:8]
    app = [x for _, w, x in run.log if w == "app"]
    awake_h = max(1.0, (at(script.day, script.bed, 1 if script.bed < script.wake else 0)
                        - at(script.day, script.wake)).total_seconds() / 3600)
    facts["app_messages"] = len(app)
    facts["app_messages_per_waking_hour"] = round(len(app) / awake_h, 2)
    facts["app_messages_repeated"] = len(app) - len(set(app))
    facts["user_messages"] = sum(1 for _, w, _ in run.log if w == "user")
    plan = run.plan(0)
    spans = [s for s in plan.spans_in_view() if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)]
    facts["joined_titles"] = [s.title for s in spans if JOINED_RE.search(s.title)]
    # outcomes against what really happened
    rows = []
    for item in script.truth:
        words = [w.strip().lower() for w in (item.get("match") or item["item"]).split("|")]
        found = [s for s in spans if any(w in s.title.lower() for w in words)]
        done = [s for s in found if s.status in (SpanStatus.DONE, SpanStatus.PARTIAL)]
        row = {"item": item["item"], "really_done": item.get("done", True), "recorded": bool(done)}
        if done and item.get("start") and item.get("end"):
            s = done[0]
            if s.actual_start:
                row["start_error_min"] = round(abs((s.actual_start - at(script.day, item["start"])).total_seconds()) / 60)
            if s.actual_end:
                row["end_error_min"] = round(abs((s.actual_end - at(script.day, item["end"])).total_seconds()) / 60)
        rows.append(row)
    facts["outcomes"] = rows
    real = [r for r in rows if r["really_done"]]
    facts["done_items_recorded"] = f"{sum(r['recorded'] for r in real)}/{len(real)}"
    facts["not_done_items_recorded_done"] = sum(1 for r in rows if not r["really_done"] and r["recorded"])
    errs = [r[k] for r in rows for k in ("start_error_min", "end_error_min") if k in r]
    facts["mean_time_error_min"] = round(sum(errs) / len(errs), 1) if errs else None
    try:
        facts["joplin"] = joplin_facts(run)
    except ImportError:
        pass  # a version without the write log
    return facts


def joplin_facts(run: DayRun) -> Dict[str, Any]:
    """Every Joplin write of the day, and what must hold at the end of it (heavy principle 2): nothing written into
    a routine note, everything on today's plan on the Today note, what's done ticked in its list, no line twice."""
    from collections import Counter
    from thirties_core.joplin_writeback import WriteLog
    from thirties_core.lists import ListCatalog
    from thirties_core.today import TodayLinks, note_items, notes_with_role
    db = run.scheduler.state_db
    writes = WriteLog(db).since("")
    roles = ListCatalog(db).roles()
    joplin = run.manager.tools.services.joplin
    notes = joplin.fetch_active_notes() if joplin is not None else []
    routine_ids = {n["id"] for n in notes_with_role(roles, notes, "routine")}
    out: Dict[str, Any] = {
        "writes": len(writes), "failed": sum(1 for w in writes if not w["ok"]),
        "into_routine": [f"{w['note_title']}: +{w['added']}" for w in writes
                         if w["note_id"] in routine_ids and w["added"] and w["op"].get("op") != "untick_all"],
        "log": [f"{w['op'].get('op')} {w['note_title']}: -{w['removed']} +{w['added']}" for w in writes][:40],
    }
    today = next(iter(notes_with_role(roles, notes, "today")), None)
    if today is not None:
        lines = [t.lower() for t, _ in note_items(today)]
        links = TodayLinks(db)
        plan = run.plan(0)
        planned = [s for s in plan.spans if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
                   and s.status != SpanStatus.SKIPPED and s.source != "calendar"]
        out["plan_not_on_today"] = [s.title for s in planned if s.title.lower() not in lines
                                    and (links.today_title_of(s.task_id) or "") not in lines]
        top = re.findall(r"^-\s*\[[ xX]\]\s*(.+?)\s*$", today.get("body") or "", re.M)
        out["today_duplicates"] = [t for t, n in Counter(x.lower() for x in top).items() if n > 1]
        open_ids = {t.id for t in (joplin.fetch_tasks() if joplin is not None else [])}
        out["done_but_open"] = [s.title for s in planned if s.status == SpanStatus.DONE and s.task_id in open_ids]
    return out


JUDGE_DIMS = ("accuracy", "understanding", "plan_respect", "message_load", "record_keeping", "clarity", "tone")


def judge_day(run: DayRun, judge, sim) -> None:
    script = run.script
    facts = {k: v for k, v in run.facts.items() if k not in ("outcomes", "minute_time_examples")}
    verdict = judge(DAY_JUDGE_PROMPT.format(
        persona=script.persona, situation=script.situation, life="\n".join(f"- {b}" for b in script.life),
        constraints="\n".join(f"- {c}" for c in script.constraints) or "(none)",
        transcript=run.transcript_text(), schedule=run.record_text(), facts=json.dumps(facts)))
    run.judge = extract_json(verdict) or {"raw": verdict[:500]}
    rating = sim(RATING_PROMPT.format(persona=script.persona, transcript=run.transcript_text(for_user=True)))
    run.rating = extract_json(rating) or {"raw": rating[:300]}


def requests_met(judge: Dict[str, Any]) -> Optional[float]:
    vals = [float(r.get("met", 0)) for r in judge.get("requests") or [] if isinstance(r, dict)]
    return round(sum(vals) / len(vals), 2) if vals else None


# ----------------------------------------------------------------------------
# Jobs, reports
# ----------------------------------------------------------------------------

def run_jobs(args, jobs) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cfg = load_config()
    sched = DeterministicScheduler(cfg, StateDatabase(Path(__import__("tempfile").mkdtemp()) / "s.sqlite"),
                                   MockCalendarEngine([]))
    tasks, notes = load_backlog(cfg, JoplinEngine(cfg), sched)
    if args.lists_from:
        from thirties_core.lists import ListCatalog
        catalog = ListCatalog(StateDatabase(Path(args.lists_from)))
        tasks = catalog.apply(tasks)
        ignored = {nid for nid, r in catalog.roles().items() if r["role"] == "ignore"}
        notes = [n for n in notes if n["id"] not in ignored]
    sim, judge = make_llm(args.sim_model), make_llm(args.sim_model)
    fcfg = ThirtiesConfig()
    fcfg.inference.gemini_model = args.frontier_model
    fcfg.inference.timeout_seconds = 180
    engine = AntigravityInferenceEngine(fcfg)
    results = []
    for n, (script, brain) in enumerate(jobs, 1):
        stop = quota_stop([args.sim_model] + ([args.frontier_model] if brain == "frontier" else []), args.budget)
        if stop:  # the real quota (agy /quota), checked before every day
            return results, f"stopped after {len(results)} finished days in this worker: {stop}"
        print(f"\n== {script.id} [{brain}] ({n}/{len(jobs)})", flush=True)
        run = None
        try:
            run = start_day(script, brain, tasks, notes, engine, args.lists_from, sim)
            live_day(run, sim, args.max_moves)
            run.facts = measure(run)
            judge_day(run, judge, sim)
        except QuotaExhausted as e:
            return results, f"agy quota ran out during day {n} ({script.id}) after {len(results)} finished days: {e}"
        except Exception as e:
            logger.exception("day %s failed", script.id)
            run = run or DayRun(script, brain)
            run.error = f"{type(e).__name__}: {e}"
        finally:
            set_debug_time(None)
        result = {
            "script": script.id, "brain": brain, "end_reason": run.end_reason, "error": run.error,
            "message_level": getattr(run, "message_level", ""),
            "requests_met": requests_met(run.judge), "judge": run.judge, "rating": run.rating, "facts": run.facts,
            "mean_latency": round(sum(run.latencies) / len(run.latencies), 1) if run.latencies else None,
            "log": run.log, "record": run.record_text() if run.scheduler else "",
        }
        print(f"  -> {run.end_reason or run.error}; requests met {result['requests_met']}; rating "
              f"{run.rating.get('rating')}; " + ", ".join(f"{d} {run.judge.get(d)}" for d in JUDGE_DIMS)
              + f"\n     facts: { {k: v for k, v in run.facts.items() if k not in ('outcomes',)} }", flush=True)
        results.append(result)
    return results, None


def summarize(results: List[Dict[str, Any]], brains: List[str]) -> None:
    print("\n=== Summary ===")
    for brain in brains:
        rs = [r for r in results if r["brain"] == brain and not r["error"]]
        if not rs:
            continue

        def mean(vals):
            vals = [v for v in vals if isinstance(v, (int, float))]
            return f"{sum(vals) / len(vals):.2f}" if vals else "–"
        print(f"{brain}: {len(rs)} days | requests met {mean(r['requests_met'] for r in rs)} | user rating "
              f"{mean(r['rating'].get('rating') for r in rs)}/10 | "
              + " ".join(f"{d} {mean(r['judge'].get(d) for r in rs)}" for d in JUDGE_DIMS))
        print(f"  minute times shown {sum(r['facts'].get('minute_times_shown', 0) for r in rs)} | app messages per "
              f"waking hour {mean(r['facts'].get('app_messages_per_waking_hour') for r in rs)} | repeated "
              f"{sum(r['facts'].get('app_messages_repeated', 0) for r in rs)} | internal text "
              f"{sum(len(r['facts'].get('internal_text_in_replies', [])) for r in rs)} | joined titles "
              f"{sum(len(r['facts'].get('joined_titles', [])) for r in rs)} | joplin: writes "
              f"{sum(r['facts'].get('joplin', {}).get('writes', 0) for r in rs)}, into routines "
              f"{sum(len(r['facts'].get('joplin', {}).get('into_routine', [])) for r in rs)}, plan not on Today "
              f"{sum(len(r['facts'].get('joplin', {}).get('plan_not_on_today', [])) for r in rs)}, done but open "
              f"{sum(len(r['facts'].get('joplin', {}).get('done_but_open', [])) for r in rs)} | time error "
              f"{mean(r['facts'].get('mean_time_error_min') for r in rs)} min | latency {mean(r['mean_latency'] for r in rs)}s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", choices=["slm", "frontier", "both"], default="frontier")
    ap.add_argument("--scripts", nargs="*", default=[str(d) for d in SCRIPT_DIRS],
                    help="script files or directories (default: tests/day_scripts and docs/eval_reports/day_scripts)")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--max-moves", type=int, default=MAX_MOVES, help="cut each day short (for a quick smoke run)")
    ap.add_argument("--lists-from", default="", help="list roles and urgency from this database "
                                                      "(tests/day_scripts.py lists makes one)")
    ap.add_argument("--shard", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--budget", type=float, default=0.66)
    ap.add_argument("--sim-model", default="gemini-3.8-flash-low")
    ap.add_argument("--frontier-model", default="gemini-3.8-flash-low")
    ap.add_argument("--label", default="")
    ap.add_argument("--order-seed", type=int, default=None, help="run days in a fixed shuffled order (milestone runs)")
    ap.add_argument("--resume", default="", help="skip days already in reports with this label")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="      [%(name)s] %(message)s")

    brains = ["slm", "frontier"] if args.brain == "both" else [args.brain]
    scripts = find_scripts([Path(p) for p in args.scripts], args.only)
    jobs = [(s, b) for s in scripts for b in brains]
    if args.order_seed is not None:  # a fixed shuffled order: any first N days are a fair sample of all of them
        import hashlib
        jobs.sort(key=lambda j: hashlib.sha256(f"{args.order_seed}:{j[0].id}:{j[1]}".encode()).hexdigest())
    if args.resume:  # skip days already run under this label (a run that spans windows or weeks)
        done = set()
        for report in REPORTS_DIR.glob(f"day_*_{args.resume}.json"):
            done |= {(r["script"], r["brain"]) for r in json.loads(report.read_text()) if not r.get("error")}
        jobs = [j for j in jobs if (j[0].id, j[1]) not in done]
        print(f"resuming {args.resume}: {len(done)} days done, {len(jobs)} to go", flush=True)
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        results, quota = run_jobs(args, jobs[k::n])
        Path(args.out).write_text(json.dumps({"results": results, "quota": quota}, default=str))
        sys.exit(3 if quota else 0)
    used, allowed, capacity = budget_left(args.budget)
    need = sum(CALLS_PER_DAY[b] for _, b in jobs)
    print(f"{len(jobs)} days ({len(scripts)} scripts × {len(brains)} brains). agy: {used} calls in the last 5 h; "
          f"budget {allowed} ({args.budget:.0%} of ~{capacity}); this run needs about {need}.", flush=True)
    quota_notes: List[str] = []
    if args.workers > 1:
        import subprocess
        log_dir = REPORTS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        procs = []
        for k in range(args.workers):
            out = log_dir / f"{stamp}_worker{k}.json"
            cmd = [sys.executable, "-m", "tests.day_sim", *sys.argv[1:], "--shard", f"{k}/{args.workers}", "--out", str(out)]
            log = open(log_dir / f"{stamp}_worker{k}.log", "w")
            procs.append((subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT), out, log))
        print(f"logs in {log_dir.relative_to(REPO_ROOT)}/{stamp}_worker*.log", flush=True)
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
        print(f"\n!!! Stopped early (agy budget or quota): {len(results)} of {len(jobs)} days finished.")
        for q in quota_notes:
            print("    " + q)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"day_{datetime.now().strftime('%Y%m%d_%H%M%S')}{'_' + args.label if args.label else ''}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"Report: {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
