"""Studies: the testing loop. One question per run, simulated people with a running inner state, and frictions
rather than scores.

A study (tests/studies/<id>.json) starts from a written question ("Does the morning give exactly one message when
the user wakes up late?"), names the change it tests and the days it uses, and states its budget up front. No
question, no run.

Each day is lived by a simulated person (a cloud model, through the sensitive-data filter like everything else).
After every message from the app, the person privately notes whether they understood it, whether it did what they
asked, and how their energy, patience and trust stand now. That state carries through the day and changes how
they behave: shorter and sharper messages, repeating themselves, opening the app and ticking things themselves,
ignoring check-ins, giving up for a while, or leaving a note to the engineers. They read what a phone shows
(Telegram's formatting, long messages cut off).

The output is frictions: each moment patience or trust drops, or the person repeats or corrects themselves, is
logged with their words, what the app actually did (its tool calls), and a likely cause. Frictions are grouped
across days into a catalogue with counts ("morning double message: 4 of 8 days"), compared with the study's last
run, and written up as a one-page report: what was tested and why, what it found, what changed since last time,
and what's next. A separate reviewer (a different model from the actors) says whether the people were believable
and whether the run taught anything new.

Data safety and the user's rules are checked by the fast invariant tests, never by a study.

    ~/.local/share/thirties/venv/bin/python -m tests.study tests/studies/<id>.json
    ... --days 2 (fewer days for a smoke run)     --real (also the user's own days; local only)

Reports go to docs/eval_reports/studies/<study id>/ (gitignored), and to --preview-dir if given.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.day_sim import (SCRIPT_DIRS, DayRun, DayScript, _hhmm, advance, find_scripts, joplin_facts, measure,
                           start_day, tool_lines)
from tests.sim_eval import QuotaExhausted, at, raise_if_agy_failed
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.slm.parse import extract_json
from thirties_core.clock import format_clock

STUDIES_DIR = REPO_ROOT / "tests" / "studies"
REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports" / "studies"
REAL_SCRIPTS = REPO_ROOT / "docs" / "eval_reports" / "day_scripts"  # the user's own days: local, with --real
PHONE_CUT = 600        # characters of a message a phone shows before "Read more"
MAX_MOVES = 160
MAX_MESSAGES = 80
START_STATE = {"energy": 7, "patience": 7, "trust": 6}
DROP = 2               # a drop this big in patience or trust is a friction
logger = logging.getLogger("study")


# ----------------------------------------------------------------------------
# The study
# ----------------------------------------------------------------------------

@dataclass
class Study:
    id: str
    question: str
    change: str
    scenarios: List[Dict[str, Any]]           # {"script": id, "beats": ["HH:MM ..."], "wake": "HH:MM", "why": ...}
    budget: Dict[str, int]                    # {"days": n, "calls": n}: agy calls for actors and reviewers
    why: str = ""
    brain: str = "slm"                        # the on-device model by default
    actor_model: str = "gemini-3.8-flash-low"
    frontier_model: str = "gemini-3.8-flash-low"  # the app's own model, when the brain under test is "frontier"
    reviewer_model: str = "gemini-4-argon"      # a different model from the actors
    start_state: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Study":
        data = json.loads(Path(path).read_text())
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def problems(self) -> List[str]:
        """What stops this study from running (an empty list if nothing)."""
        out = []
        if not str(self.question).strip().endswith("?"):
            out.append("no question: a study starts with a written question (ending in '?')")
        if not str(self.change).strip():
            out.append("no change named: say which change this study tests")
        if not self.scenarios:
            out.append("no days chosen")
        if not (self.budget or {}).get("days") or not (self.budget or {}).get("calls"):
            out.append("no budget: give budget.days and budget.calls")
        for s in self.scenarios:
            for beat in s.get("beats") or []:
                if not re.match(r"^([01]\d|2[0-3]):[0-5]\d\b", str(beat)):
                    out.append(f"beat without a time first: {str(beat)[:40]!r}")
        return out


def scripts_for(study: Study, real: bool = False) -> List[DayScript]:
    """The study's days, with its beats added to each day's life and any wake-up time it sets."""
    dirs = list(SCRIPT_DIRS[:2]) + ([REAL_SCRIPTS] if real else [])
    by_id = {s.id: s for s in find_scripts(dirs)}
    out = []
    for sc in study.scenarios[: study.budget.get("days", len(study.scenarios))]:
        script = by_id.get(sc.get("script"))
        if script is None:
            raise SystemExit(f"study {study.id}: no day script {sc.get('script')!r}"
                             + ("" if real else " (the user's own days need --real)"))
        life = sorted(list(script.life) + list(sc.get("beats") or []))
        script = DayScript(**{**script.__dict__, "life": life, "wake": sc.get("wake") or script.wake})
        out.append(script)
    return out


# ----------------------------------------------------------------------------
# The person
# ----------------------------------------------------------------------------

ACTOR_PROMPT = """You are role-playing a real person through one whole day, using a personal planning assistant app ("Thirties") from your phone. Stay in character.
Persona: {persona}
How the day starts: {situation}

Your day, as it will really go. The app does NOT know any of this; only what's at or before the current time has happened yet.
{life}

True for you, said only when it matters:
{constraints}

How you feel right now (0-10): energy {energy}, patience {patience}, trust in the app {trust}.
It is now {now}. You go to bed around {bed}.

Your phone, as you see it (long messages are cut off; "App" lines are the app's own notifications):
{phone}
{agenda}
What just happened: {just}

First, privately note how that landed. Then decide your next move. Return ONLY a JSON object:
{{"felt": {{"understood": "yes" | "partly" | "no" | "n/a", "did_what_i_asked": "yes" | "partly" | "no" | "n/a",
           "energy": 0-10, "patience": 0-10, "trust": 0-10,
           "annoyance": <3-8 words naming what bugged you, or null>, "why": <one short line, your own words>}},
 "move": {{"do": "message" | "wait" | "open_app" | "tick" | "give_up" | "sleep",
           "message": <what you type, if do is message>, "tick": <the item you tick in the app, if do is tick>,
           "wait_minutes": <minutes until you next pick up the phone>, "engineer_note": <optional: a note to the app's makers>}}}}
Be a person, not a tester:
- Your feelings carry over and move with what happens: a wrong or nagging reply costs patience; a false claim ("done!" when it isn't) costs trust; a good, short answer restores some. Energy drops through the day and after hassle.
- Low patience: shorter, sharper messages; you repeat yourself when you weren't understood, or correct it twice. Low trust: you check the app yourself (open_app) or just tick things there (tick). Very low: you ignore it for a while (give_up), or leave a note to the makers.
- Text like a real person: often short, sometimes several things at once, sometimes vague or late about times. Reply to check-ins late or not at all if you're busy.
- Don't fill time: if nothing is due, wait until the next thing in your day (up to 180 minutes)."""


def phone_view(text: str) -> str:
    """A message as a phone shows it: Telegram's formatting applied (no markup left), long ones cut off."""
    from thirties_core.telegram_bridge import telegram_html
    try:
        shown = html.unescape(re.sub(r"<[^>]+>", "", telegram_html(text or "")))
    except Exception:
        shown = text or ""
    return shown if len(shown) <= PHONE_CUT else shown[:PHONE_CUT].rstrip() + "… [Read more]"


@dataclass
class Friction:
    day: str
    at: str
    label: str
    quote: str
    app_did: List[str]
    reply: str
    cause: str
    kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Person:
    """The simulated person's running inner state, and what they noted privately."""
    state: Dict[str, int] = field(default_factory=lambda: dict(START_STATE))
    notes: List[Dict[str, Any]] = field(default_factory=list)

    def feel(self, felt: Dict[str, Any]) -> Dict[str, int]:
        """Take the new feelings; returns how much patience and trust dropped."""
        before = dict(self.state)
        for k in ("energy", "patience", "trust"):
            try:
                self.state[k] = max(0, min(10, int(felt.get(k, self.state[k]))))
            except (TypeError, ValueError):
                pass
        return {k: before[k] - self.state[k] for k in ("patience", "trust")}


def is_friction(felt: Dict[str, Any], move: Dict[str, Any], drop: Dict[str, int]) -> bool:
    return (drop["patience"] >= DROP or drop["trust"] >= DROP or felt.get("understood") == "no"
            or felt.get("did_what_i_asked") == "no" or move.get("do") == "give_up" or bool(move.get("engineer_note")))


# ----------------------------------------------------------------------------
# Living a day
# ----------------------------------------------------------------------------

@dataclass
class StudyDay:
    run: DayRun
    person: Person
    frictions: List[Friction] = field(default_factory=list)
    calls: int = 0


def agenda_text(run: DayRun, now: datetime) -> str:
    from thirties_core.messages import render_schedule
    return render_schedule(run.plan(0), now, whole_day=True)


def tick_in_app(run: DayRun, item: str) -> str:
    """The person ticks an item done in the app's Agenda (the same tool the window uses)."""
    want = (item or "").strip().lower()
    span = next((s for s in run.plan(0).spans_in_view() if want and (s.title.lower() == want or want in s.title.lower())),
                None)
    if span is None:
        return f"(nothing called {item!r} on the Agenda)"
    with run.manager.turn_lock:
        out = run.manager.execute_tool("log_outcome", {"event": span.id, "status": "done"})
    return f"ticked {span.title}: {out[:120]}"


def live_study_day(run: DayRun, actor: Callable[[str], str], person: Person, max_moves: int = MAX_MOVES,
                   budget_left: Callable[[], bool] = lambda: True) -> StudyDay:
    day = StudyDay(run, person)
    script = run.script
    now = at(script.day, script.wake)
    bed = at(script.day, script.bed, 1 if script.bed < script.wake else 0)
    life = "\n".join(f"- {b}" for b in script.life)
    constraints = "\n".join(f"- {c}" for c in script.constraints) or "(none)"
    run.service.tick(now)
    just, show_agenda, last_tools, last_reply, messages = "You've just woken up.", False, [], "", 0
    for _ in range(max_moves):
        if not budget_left():
            run.end_reason = "budget"
            return day
        phone = "\n".join(f"[{t}] {'You' if w == 'user' else 'App' if w == 'app' else 'Thirties'}: "
                          f"{x if w == 'user' else phone_view(x)}" for t, w, x in run.log if w != "tool") or "(nothing yet)"
        raw = actor(ACTOR_PROMPT.format(
            persona=script.persona, situation=script.situation, life=life, constraints=constraints,
            now=format_clock(now), bed=format_clock(bed), phone=phone, just=just,
            agenda=f"\nThe app's Agenda, open on your screen:\n{agenda_text(run, now)}\n" if show_agenda else "",
            **person.state))
        day.calls += 1
        reaction = extract_json(raw) or {}
        felt, move = reaction.get("felt") or {}, reaction.get("move") or {}
        if not move:
            logger.warning("actor's move wasn't JSON: %r", raw[:200])
            move = {"do": "wait", "wait_minutes": 15}
        drop = person.feel(felt)
        person.notes.append({"at": _hhmm(now), **felt, "state": dict(person.state), "do": move.get("do")})
        if is_friction(felt, move, drop):
            day.frictions.append(Friction(
                day=script.id, at=_hhmm(now), label=str(felt.get("annoyance") or felt.get("why") or "")[:120],
                quote=str(move.get("message") or move.get("engineer_note") or felt.get("why") or "")[:300],
                app_did=list(last_tools), reply=last_reply[:300], cause=str(felt.get("why") or "")[:300]))
        if move.get("engineer_note"):
            run.log.append((_hhmm(now), "user", f"(note to the makers) {move['engineer_note']}"))
        do = move.get("do") or "wait"
        show_agenda = False
        if do == "sleep" or now >= bed:
            run.end_reason = "end of day"
            return day
        if do == "message" and str(move.get("message") or "").strip():
            text = str(move["message"]).strip()
            run.log.append((_hhmm(now), "user", text))
            before = len(run.manager.brain.messages)
            start = time_mod.time()
            reply = run.manager.send_user_message(text)
            last_tools = tool_lines(run.manager.brain.messages[before:])
            run.log += [(_hhmm(now), "tool", t) for t in last_tools]
            if run.brain in ("frontier", "pipeline"):
                raise_if_agy_failed(reply)
            run.latencies.append(round(time_mod.time() - start, 1))
            run.log.append((_hhmm(now), "assistant", reply))
            last_reply, just = reply, "Thirties replied (see your phone)."
            messages += 1
            if messages >= MAX_MESSAGES:
                run.end_reason = "message limit"
                return day
            continue
        if do == "open_app":
            show_agenda, just = True, "You opened the app's Agenda."
            continue
        if do == "tick":
            done = tick_in_app(run, str(move.get("tick") or ""))
            run.log.append((_hhmm(now), "user", f"(in the app) {done}"))
            just = f"You {done}."
            continue
        wait = max(5, min(int(move.get("wait_minutes") or (120 if do == "give_up" else 15)), 240))
        target = min(now + timedelta(minutes=wait), bed)
        if do == "give_up":  # ignoring the app: its messages pile up unread until then
            while now < target:
                now = advance(run, now, target)
            just = "You come back to your phone after ignoring it for a while."
            continue
        reached = advance(run, now, target) if target > now else now
        just = "A notification from the app arrived." if reached < target else "Time passed."
        last_tools, last_reply = [], (run.log[-1][2] if reached < target and run.log else last_reply)
        now = reached
    run.end_reason = "move limit"
    return day


# ----------------------------------------------------------------------------
# The catalogue, the review and the report
# ----------------------------------------------------------------------------

CATALOGUE_PROMPT = """Group these moments of friction a planning app caused its users into kinds. Use the earlier kinds' names when a moment is the same kind of problem, so runs can be compared.
Earlier kinds: {earlier}
Moments (id: day | what bugged them | their words | what the app did):
{items}
Return ONLY a JSON list: [{{"kind": <short name>, "ids": [<ids>], "likely_cause": <one line: what in the app probably causes it>}}]"""

REVIEW_PROMPT = """You review a simulation in which models played people using a planning app for a day each. Judge the simulated people, not the app.
The study's question: {question}
Per day: the person, their private notes (state over the day) and the conversation, shortened.
{days}
Frictions found: {frictions}
Earlier runs' frictions: {earlier}
Return ONLY a JSON object:
{{"verdict": "useful" | "thin" | "samey", "believable": <one line>, "most_human": [<up to 2 moments>], "least_human": [<up to 2 moments>],
 "sameness": [<openings, day shapes or wordings repeated across people>], "not_human_enough": [<e.g. accepts everything, never repeats, energy never dips>],
 "actor_changes": [<concrete changes to the people for the next study>], "touched_question": <true if the days actually exercised the question>}}"""


def json_list(raw: str) -> Optional[List[Any]]:
    """The JSON list in a model's reply (fenced or not), or None."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return out if isinstance(out, list) else None


def catalogue(frictions: List[Friction], llm: Callable[[str], str], earlier: List[str]) -> List[Dict[str, Any]]:
    if not frictions:
        return []
    items = "\n".join(f"{i}: {f.day} | {f.label} | {f.quote[:160]} | {'; '.join(f.app_did)[:200] or '(nothing)'}"
                      for i, f in enumerate(frictions))
    groups = json_list(llm(CATALOGUE_PROMPT.format(earlier=", ".join(earlier) or "(none)", items=items)))
    if not isinstance(groups, list):
        groups = [{"kind": f.label or "unnamed", "ids": [i], "likely_cause": f.cause} for i, f in enumerate(frictions)]
    out = []
    for g in groups:
        ids = [i for i in g.get("ids") or [] if isinstance(i, int) and 0 <= i < len(frictions)]
        for i in ids:
            frictions[i].kind = g.get("kind", "")
        days = sorted({frictions[i].day for i in ids})
        out.append({"kind": g.get("kind", ""), "count": len(ids), "days": days, "likely_cause": g.get("likely_cause", ""),
                    "quotes": [frictions[i].quote for i in ids][:3]})
    return sorted(out, key=lambda g: (-len(g["days"]), -g["count"]))


def compare(now: List[Dict[str, Any]], before: List[Dict[str, Any]], days_now: int, days_before: int) -> List[str]:
    """"morning double message: 4 of 8 days → 0 of 8"."""
    old = {g["kind"]: len(g["days"]) for g in before}
    new = {g["kind"]: len(g["days"]) for g in now}
    lines = []
    for kind in sorted(set(old) | set(new), key=lambda k: -max(old.get(k, 0), new.get(k, 0))):
        if old.get(kind, 0) != new.get(kind, 0):
            lines.append(f"{kind}: {old.get(kind, 0)} of {days_before} days → {new.get(kind, 0)} of {days_now}")
    return lines


def day_record(day: StudyDay) -> Dict[str, Any]:
    """A finished day, as saved (so a run cut short by its time window can resume)."""
    run = day.run
    return {"script": run.script.id, "persona": run.script.persona, "end": run.end_reason,
            "state_at_bed": day.person.state, "notes": day.person.notes, "log": run.log, "facts": run.facts,
            "calls": day.calls, "frictions": [f.to_dict() for f in day.frictions]}


def review(study: Study, days: List[Dict[str, Any]], frictions: List[Friction], llm: Callable[[str], str],
           earlier: List[str]) -> Dict[str, Any]:
    parts = []
    for d in days:
        notes = "; ".join(f"{n['at']} {n.get('state')} {n.get('why') or ''}"[:140] for n in d["notes"][:30])
        convo = "\n".join(f"[{t}] {w}: {x[:160]}" for t, w, x in d["log"] if w != "tool")[:3000]
        parts.append(f"== {d['script']}: {d['persona'][:200]}\nNotes: {notes}\n{convo}")
    raw = llm(REVIEW_PROMPT.format(question=study.question, days="\n\n".join(parts),
                                   frictions=json.dumps([f.label for f in frictions])[:2000],
                                   earlier=", ".join(earlier) or "(none)"))
    return extract_json(raw) or {"raw": raw[:500]}


def report(study: Study, days: List[Dict[str, Any]], groups: List[Dict[str, Any]], changes: List[str],
           testers: Dict[str, Any], calls: int, next_step: str = "") -> str:
    """The one-page report: readable in two minutes."""
    lines = [f"# {study.question}", "",
             f"**Change tested:** {study.change}  " + (f"\n**Why:** {study.why}  " if study.why else ""),
             f"**Days:** {len(days)} ({', '.join(d['script'] for d in days)}), brain {study.brain}; "
             f"{calls} model calls of a budget of {study.budget.get('calls')}.", "", "## What it found", ""]
    if not groups:
        lines.append("No frictions: nobody's patience or trust dropped, and nobody repeated or corrected themselves.")
    for g in groups[:6]:
        quotes = "; ".join(f"“{q[:90]}”" for q in g["quotes"][:2])
        lines.append(f"- **{g['kind']}**: {len(g['days'])} of {len(days)} days ({g['count']}×). {quotes}"
                     + (f" Likely cause: {g['likely_cause']}" if g.get("likely_cause") else ""))
    lines += ["", "## Since the last run", ""] + ([f"- {c}" for c in changes] or ["- First run, or no change."])
    lines += ["", "## Testers", "",
              f"- Verdict: {testers.get('verdict', '?')}. {testers.get('believable', '')}",
              *(f"- Least human: {m}" for m in testers.get("least_human") or []),
              *(f"- Next time, the people: {c}" for c in (testers.get("actor_changes") or [])[:3])]
    lines += ["", "## Next", "", next_step or "_(the testing agent fills this in before the next study)_", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Running a study
# ----------------------------------------------------------------------------

def previous_run(study_dir: Path, exclude: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    runs = [r for r in sorted(study_dir.glob("*/result.json")) if exclude is None or r.parent != Path(exclude)]
    return json.loads(runs[-1].read_text()) if runs else None


DAY_ROOM = timedelta(minutes=40)  # a day isn't started with less than this left before the run's end time


def run_study(study: Study, actor: Callable[[str], str], reviewer: Callable[[str], str], engine, tasks=(), notes=(),
              real: bool = False, out_dir: Optional[Path] = None, max_moves: int = MAX_MOVES,
              next_step: str = "", until: Optional[datetime] = None, resume: Optional[Path] = None) -> Dict[str, Any]:
    """Run the study's days and write its report. ``until``: start no day that might not finish by then (a GPU
    window); ``resume``: a run directory cut short earlier, whose finished days are kept and not run again."""
    problems = study.problems()
    if problems:
        raise SystemExit(f"study {study.id} can't run: " + "; ".join(problems))
    study_dir = (out_dir or REPORTS_DIR) / study.id
    before = previous_run(study_dir, exclude=Path(resume) if resume else None)
    earlier = [g["kind"] for g in (before or {}).get("catalogue", [])]
    calls = {"n": 0}
    limit = int(study.budget["calls"])

    def counted(llm):
        def ask(prompt: str) -> str:
            calls["n"] += 1
            return llm(prompt)
        return ask

    actor, reviewer = counted(actor), counted(reviewer)
    run_dir = Path(resume) if resume else study_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    (run_dir / "days").mkdir(parents=True, exist_ok=True)
    days: List[Dict[str, Any]] = [json.loads(p.read_text()) for p in sorted((run_dir / "days").glob("*.json"))]
    calls["n"] = sum(d.get("calls", 0) for d in days)
    finished = {d["script"] for d in days}
    stopped = ""
    for script in scripts_for(study, real):
        if script.id in finished:
            continue
        if calls["n"] >= limit:
            stopped = "budget"
            break
        if until is not None and datetime.now(until.tzinfo) > until - DAY_ROOM:
            stopped = "time window"
            break
        print(f"\n== {script.id} [{study.brain}]", flush=True)
        try:
            run = start_day(script, study.brain, list(tasks), list(notes), engine)
            person = Person(state={**START_STATE, **study.start_state})
            before_calls = calls["n"]
            day = live_study_day(run, actor, person, max_moves, budget_left=lambda: calls["n"] < limit - 2)
            day.calls = calls["n"] - before_calls
            run.facts = {**measure(run), "joplin": joplin_facts(run)}
            record = day_record(day)
            (run_dir / "days" / f"{script.id}.json").write_text(json.dumps(record, indent=2, default=str))
            days.append(record)
            print(f"  -> {run.end_reason}; {len(day.frictions)} frictions; state at bed {person.state}", flush=True)
        except QuotaExhausted as e:
            print(f"  agy quota ran out: {e}", flush=True)
            stopped = "agy quota"
            break
        finally:
            set_debug_time(None)
    if stopped:
        print(f"Stopped ({stopped}): {len(days)} of {min(len(study.scenarios), study.budget['days'])} days done; "
              f"resume with --resume {run_dir}", flush=True)
    frictions = [Friction(**{k: v for k, v in f.items() if k in Friction.__dataclass_fields__})
                 for d in days for f in d["frictions"]]
    groups = catalogue(frictions, reviewer, earlier)
    testers = review(study, days, frictions, reviewer, earlier) if days else {}
    changes = compare(groups, (before or {}).get("catalogue", []), len(days), (before or {}).get("days", 0)) \
        if before else []
    text = report(study, days, groups, changes, testers, calls["n"], next_step)
    stamp = run_dir.name
    result = {"study": study.id, "question": study.question, "at": stamp, "days": len(days), "calls": calls["n"],
              "stopped": stopped, "catalogue": groups, "frictions": [f.to_dict() for f in frictions],
              "testers": testers, "per_day": days}
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    (run_dir / "report.md").write_text(text)
    result["report"] = text
    result["dir"] = str(run_dir)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("study", help="the study file (tests/studies/<id>.json)")
    ap.add_argument("--days", type=int, default=0, help="fewer days than the study's budget (a smoke run)")
    ap.add_argument("--real", action="store_true", help="also the user's own days (local data; never committed)")
    ap.add_argument("--preview-dir", default="", help="also copy the report here (the preview page)")
    ap.add_argument("--next", default="", help="what the testing agent will do next, for the report")
    ap.add_argument("--max-moves", type=int, default=MAX_MOVES)
    ap.add_argument("--until", default="", help="start no day that might not finish by this time (HH:MM, local)")
    ap.add_argument("--resume", default="", help="a run directory cut short earlier: keep its days, run the rest")
    ap.add_argument("--brain", choices=["slm", "frontier", "pipeline"], default="",
                    help="test another brain than the study names (the on-device one needs the GPU)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="      [%(name)s] %(message)s")
    study = Study.load(Path(args.study))
    if args.days:
        study.budget = {**study.budget, "days": args.days}
    if args.brain:
        study.brain = args.brain
    problems = study.problems()
    if problems:
        raise SystemExit("Not running: " + "; ".join(problems))
    from tests.sim_eval import make_llm
    from thirties_core.config import ThirtiesConfig
    from thirties_core.inference import AntigravityInferenceEngine
    cfg = ThirtiesConfig()
    cfg.inference.gemini_model, cfg.inference.timeout_seconds = study.frontier_model, 180
    engine = AntigravityInferenceEngine(cfg)
    tasks, notes = [], []
    if args.real:  # the user's own lists, for their own days (made-up people bring their own lists)
        import tempfile
        from thirties_core.backlog import load_backlog
        from thirties_core.calendar_engine import MockCalendarEngine
        from thirties_core.config import load_config
        from thirties_core.joplin_engine import JoplinEngine
        from thirties_core.scheduler import DeterministicScheduler, StateDatabase
        real_cfg = load_config()
        with tempfile.TemporaryDirectory() as tmp:
            sched = DeterministicScheduler(real_cfg, StateDatabase(Path(tmp) / "s.sqlite"), MockCalendarEngine([]))
            tasks, notes = load_backlog(real_cfg, JoplinEngine(real_cfg), sched)
    print(f"Study {study.id}: {study.question}\n  change: {study.change}\n  budget: {study.budget}", flush=True)
    until = None
    if args.until:
        h, m = (int(x) for x in args.until.split(":"))
        until = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
        if until < datetime.now().astimezone():
            until += timedelta(days=1)
    result = run_study(study, make_llm(study.actor_model), make_llm(study.reviewer_model), engine, tasks, notes,
                       real=args.real, max_moves=args.max_moves, next_step=args.next, until=until,
                       resume=Path(args.resume) if args.resume else None)
    if args.preview_dir:
        dest = Path(args.preview_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"study_{study.id}_{result['at']}.md").write_text(result["report"])
    print("\n" + result["report"])
    print(f"Saved in {Path(result['dir']).relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
