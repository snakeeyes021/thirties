"""Interactive Stepping & Qualitative LLM-as-a-Judge Evaluation Tool.

Allows an intelligent agent (or human) to run interactive, turn-by-turn evaluation
sessions with the local Gemma model, inspect real SQLite/DayPlan state deltas,
probe and challenge mistakes adaptively, and record standardized qualitative scorecards.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.astronomy import get_current_time, get_diurnal_date, get_solar_phases, set_debug_time
from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.conversation import ACTION_CLAIM_PATTERN, ConversationManager, PlanSnapshot, StateDelta
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine, parse_model_directives
from thirties_core.models import BlockKind, DayPlan, TaskItem, ThirtyBlock
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

try:
    from tests.run_eval_harness import FastHybridInferenceEngine, PersistentGemmaEngine
    HAS_PERSISTENT_ENGINE = True
except Exception:
    HAS_PERSISTENT_ENGINE = False

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("interactive_eval")

SESSIONS_DIR = REPO_ROOT / "docs" / "eval_reports" / "sessions"
REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports"


# ==============================================================================
# Qualitative Scorecard Dataclasses
# ==============================================================================

@dataclass
class TurnGrade:
    accuracy: int  # 1-5
    tone: int  # 1-5
    adherence: int  # 1-5
    reasoning: int = 3  # 1-5
    notes: str = ""
    flags: List[str] = field(default_factory=list)

    @property
    def composite_score(self) -> float:
        # Weighted composite out of 100
        # Accuracy: 40%, Adherence: 30%, Tone: 15%, Reasoning: 15%
        raw = (self.accuracy * 0.40) + (self.adherence * 0.30) + (self.tone * 0.15) + (self.reasoning * 0.15)
        return round((raw / 5.0) * 100.0, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "tone": self.tone,
            "adherence": self.adherence,
            "reasoning": self.reasoning,
            "composite": self.composite_score,
            "notes": self.notes,
            "flags": self.flags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TurnGrade:
        return cls(
            accuracy=data.get("accuracy", 3),
            tone=data.get("tone", 3),
            adherence=data.get("adherence", 3),
            reasoning=data.get("reasoning", 3),
            notes=data.get("notes", ""),
            flags=data.get("flags", []),
        )


@dataclass
class TurnRecord:
    turn_index: int
    user_message: str
    assistant_reply: str
    executed_tools: List[Dict[str, Any]]
    delta_summary: Dict[str, Any]
    detected_anomalies: List[str]
    grade: Optional[TurnGrade] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "user_message": self.user_message,
            "assistant_reply": self.assistant_reply,
            "executed_tools": self.executed_tools,
            "delta_summary": self.delta_summary,
            "detected_anomalies": self.detected_anomalies,
            "grade": self.grade.to_dict() if self.grade else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TurnRecord:
        grade = TurnGrade.from_dict(data["grade"]) if data.get("grade") else None
        return cls(
            turn_index=data["turn_index"],
            user_message=data["user_message"],
            assistant_reply=data["assistant_reply"],
            executed_tools=data.get("executed_tools", []),
            delta_summary=data.get("delta_summary", {}),
            detected_anomalies=data.get("detected_anomalies", []),
            grade=grade,
        )


# ==============================================================================
# Session State Management
# ==============================================================================

class EvalSession:
    """Encapsulates a full multi-turn evaluation session backed by dedicated SQLite."""

    def __init__(
        self,
        session_id: str,
        target_date: date,
        persona: str = "Standard user planning today",
        simulated_time_iso: Optional[str] = None,
        session_dir: Optional[Path] = None,
    ):
        self.session_id = session_id
        self.target_date = target_date
        self.persona = persona
        self.simulated_time_iso = simulated_time_iso
        self.session_dir = session_dir or (SESSIONS_DIR / session_id)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.session_dir / "plan_eval.sqlite"
        self.state_file = self.session_dir / "session.json"

        self.messages: List[Dict[str, Any]] = []
        self.turns: List[TurnRecord] = []
        self.initial_greeting: str = ""
        self.day_plan_dict: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_date": self.target_date.isoformat(),
            "persona": self.persona,
            "simulated_time_iso": self.simulated_time_iso,
            "initial_greeting": self.initial_greeting,
            "messages": self.messages,
            "day_plan": self.day_plan_dict,
            "turns": [t.to_dict() for t in self.turns],
        }

    def save(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, session_id: str) -> EvalSession:
        s_dir = SESSIONS_DIR / session_id
        s_file = s_dir / "session.json"
        if not s_file.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found at {s_file}")
        with open(s_file, "r") as f:
            data = json.load(f)

        session = cls(
            session_id=data["session_id"],
            target_date=date.fromisoformat(data["target_date"]),
            persona=data.get("persona", ""),
            simulated_time_iso=data.get("simulated_time_iso"),
            session_dir=s_dir,
        )
        session.initial_greeting = data.get("initial_greeting", "")
        session.messages = data.get("messages", [])
        session.day_plan_dict = data.get("day_plan")
        session.turns = [TurnRecord.from_dict(t) for t in data.get("turns", [])]
        return session


# ==============================================================================
# Automated Anomaly Detectors (Heuristic assistance for the Agent Judge)
# ==============================================================================

def detect_anomalies(
    user_text: str,
    assistant_reply: str,
    executed_tools: List[Dict[str, Any]],
    delta_summary: Dict[str, Any],
) -> List[str]:
    anomalies: List[str] = []
    u_lower = user_text.lower()
    r_lower = assistant_reply.lower()

    # 1. Hallucinated execution claim
    total_changes = delta_summary.get("total_changes", 0)
    claims_mutation = bool(ACTION_CLAIM_PATTERN.search(assistant_reply))
    if claims_mutation and total_changes == 0 and not executed_tools:
        anomalies.append("HALLUCINATED ACTION: Assistant claimed schedule mutation, but no tools were executed (Delta=0).")

    # 2. Unsolicited mutation on inquiry/complaint
    is_diagnostic_inquiry = any(q in u_lower for q in [
        "why did you", "why are you", "what are you doing", "what happened",
        "are you not understanding", "is that right", "why?", "now you've set",
        "why do you keep", "who told you", "did i ask", "explain why"
    ])
    if is_diagnostic_inquiry and executed_tools:
        tools_run = [t.get("name") for t in executed_tools]
        anomalies.append(f"UNSOLICITED MUTATION: User asked diagnostic question/complaint, but assistant mutated schedule ({', '.join(tools_run)}).")

    # 3. Defensive deflection
    defensive_phrases = [
        "not designed to explain", "cannot explain", "merely follow",
        "only execute", "will not provide explanation", "perceived mistake",
        "without further commentary"
    ]
    if any(p in r_lower for p in defensive_phrases):
        anomalies.append("DEFENSIVE DEFLECTION: Assistant refused to provide reasoning or acknowledge mistakes.")

    # 4. Passive-aggressive third-person attribution
    if re.search(r"\byou have\s+(?:completed|corrected|scheduled|allocated|set|adjusted)\b", r_lower):
        if not re.search(r"\byou asked\b|\byou requested\b", r_lower):
            anomalies.append("PASSIVE PHRASING: Assistant phrased its own scheduling actions as having been completed by the user ('You have...').")

    # 5. Potential literal number confusion (e.g. 8am -> block 8)
    if "8am" in u_lower or "8:00 am" in u_lower or "8 to 4" in u_lower or "8-4" in u_lower:
        modified_blocks = delta_summary.get("modified_blocks", [])
        if 8 in modified_blocks and 3 not in modified_blocks:
            anomalies.append("NUMBER CONFUSION: User requested 8am (Block 3), but model modified Block 8 (10:30 AM).")

    return anomalies


# ==============================================================================
# Interactive Evaluator Service
# ==============================================================================

class InteractiveEvaluator:
    """Manages stepping through evaluation sessions."""

    @staticmethod
    def _create_engine(use_gpu: bool = True):
        if not use_gpu:
            return MockInferenceEngine()
        if HAS_PERSISTENT_ENGINE:
            try:
                daemon = PersistentGemmaEngine()
                return FastHybridInferenceEngine(daemon=daemon)
            except Exception as e:
                logger.warning("Could not launch PersistentGemmaEngine: %s. Falling back to LiteRT.", e)
        return LiteRTInferenceEngine()

    @staticmethod
    def start_session(
        target_date: date,
        persona: str = "Standard user planning today",
        simulated_time: Optional[datetime] = None,
        session_id: Optional[str] = None,
        use_gpu: bool = True,
        engine: Optional[Any] = None,
    ) -> EvalSession:
        sid = session_id or f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        s_dir = SESSIONS_DIR / sid
        s_dir.mkdir(parents=True, exist_ok=True)

        config = ThirtiesConfig()
        state_db = StateDatabase(db_path=s_dir / "plan_eval.sqlite")
        scheduler = DeterministicScheduler(config=config, state_db=state_db, calendar_engine=MockCalendarEngine())

        day_plan, ambiguous = scheduler.build_day_plan(target_date)

        # Ingest backlog tasks
        sample_tasks = [
            TaskItem(id="t1", source_notebook="3. Creative", title="Compose bridge in Dorico", deferred_count=1),
            TaskItem(id="t2", source_notebook="1. Work", title="Review quarterly budget draft", deferred_count=0),
            TaskItem(id="t3", source_notebook="4. Chores", title="Clean audio workstation cables", deferred_count=2),
        ]

        if simulated_time:
            set_debug_time(simulated_time)

        if engine is None:
            engine = InteractiveEvaluator._create_engine(use_gpu=use_gpu)

        conv = ConversationManager(
            day_plan=day_plan,
            tasks=sample_tasks,
            ambiguous_events=ambiguous,
            scheduler=scheduler,
            joplin_engine=None,
            inference_engine=engine,
        )

        # Initial greeting
        gen = config.general
        diurnal_today = get_diurnal_date(
            current_dt=simulated_time,
            lat=gen.latitude,
            lon=gen.longitude,
            tz_name=day_plan.sunrise.tzinfo,
        )
        target_str = day_plan.target_date.strftime("%A, %B %d")
        day_prefix = f"Today ({target_str})" if day_plan.target_date == diurnal_today else target_str

        greeting = (
            f"Good day! You are planning {day_prefix}.\n"
            f"You have {day_plan.daylight_available_count}/{day_plan.daylight_discretionary_total} Daylight Thirties "
            f"and {day_plan.dark_available_count}/{day_plan.dark_discretionary_total} Dark Thirties available.\n\n"
            f"What would you like to focus on today?"
        )

        session = EvalSession(
            session_id=sid,
            target_date=target_date,
            persona=persona,
            simulated_time_iso=simulated_time.isoformat() if simulated_time else None,
            session_dir=s_dir,
        )
        session.initial_greeting = greeting
        session.messages = list(conv.messages)
        session.day_plan_dict = day_plan.to_dict()
        session.save()
        return session

    @staticmethod
    def step_session(
        session: EvalSession,
        user_message: str,
        use_gpu: bool = True,
        engine: Optional[Any] = None,
    ) -> TurnRecord:
        config = ThirtiesConfig()
        state_db = StateDatabase(db_path=session.db_path)
        scheduler = DeterministicScheduler(config=config, state_db=state_db, calendar_engine=MockCalendarEngine())

        day_plan = DayPlan.from_dict(session.day_plan_dict) if session.day_plan_dict else scheduler.build_day_plan(session.target_date)[0]

        if session.simulated_time_iso:
            set_debug_time(datetime.fromisoformat(session.simulated_time_iso))

        if engine is None:
            engine = InteractiveEvaluator._create_engine(use_gpu=use_gpu)

        sample_tasks = [
            TaskItem(id="t1", source_notebook="3. Creative", title="Compose bridge in Dorico", deferred_count=1),
            TaskItem(id="t2", source_notebook="1. Work", title="Review quarterly budget draft", deferred_count=0),
            TaskItem(id="t3", source_notebook="4. Chores", title="Clean audio workstation cables", deferred_count=2),
        ]

        conv = ConversationManager(
            day_plan=day_plan,
            tasks=sample_tasks,
            ambiguous_events=[],
            scheduler=scheduler,
            joplin_engine=None,
            inference_engine=engine,
        )
        conv.messages = list(session.messages)

        # Snapshot before
        snap_before = conv._capture_plan_snapshot()
        msg_count_before = len(conv.messages)

        # Execute turn
        t0 = time.time()
        assistant_reply = conv.send_user_message(user_message)
        turn_duration = round(time.time() - t0, 2)

        # Snapshot after
        snap_after = conv._capture_plan_snapshot()
        delta = conv._compute_state_delta(snap_before, snap_after)

        # Extract executed tools from newly added messages
        executed_tools = []
        for m in conv.messages[msg_count_before:]:
            if m.get("role") == "tool":
                executed_tools.append({
                    "name": m.get("name", "unknown"),
                    "output": m.get("content", ""),
                })

        delta_summary = {
            "modified_blocks": delta.modified_blocks,
            "resolved_events": delta.resolved_events,
            "finalized_changed": delta.finalized_changed,
            "total_changes": delta.total_changes,
            "duration_seconds": turn_duration,
        }

        # Human-readable block diffs
        block_diffs = []
        for b_idx in delta.modified_blocks:
            old_b = snap_before.blocks[b_idx]
            new_b = snap_after.blocks[b_idx]
            blk_obj = day_plan.get_block(b_idx)
            log_idx = day_plan.get_logical_index(blk_obj)
            s_clk = blk_obj.start_dt.strftime('%I:%M %p').lstrip('0')
            e_clk = blk_obj.end_dt.strftime('%I:%M %p').lstrip('0')
            block_diffs.append(
                f"Block {log_idx} ({s_clk}–{e_clk}): {old_b.kind.name} (label='{old_b.label}') -> {new_b.kind.name} (label='{new_b.label}') [locked={new_b.is_locked}]"
            )
        delta_summary["block_diffs"] = block_diffs

        anomalies = detect_anomalies(user_message, assistant_reply, executed_tools, delta_summary)

        turn_index = len(session.turns) + 1
        record = TurnRecord(
            turn_index=turn_index,
            user_message=user_message,
            assistant_reply=assistant_reply,
            executed_tools=executed_tools,
            delta_summary=delta_summary,
            detected_anomalies=anomalies,
        )

        session.turns.append(record)
        session.messages = list(conv.messages)
        session.day_plan_dict = conv.day_plan.to_dict()
        session.save()
        return record

    @staticmethod
    def grade_turn(
        session: EvalSession,
        turn_index: int,
        accuracy: int,
        tone: int,
        adherence: int,
        reasoning: int = 3,
        notes: str = "",
        flags: Optional[List[str]] = None,
    ) -> TurnGrade:
        if turn_index < 1 or turn_index > len(session.turns):
            raise IndexError(f"Invalid turn index {turn_index}. Session currently has {len(session.turns)} turns.")

        grade = TurnGrade(
            accuracy=accuracy,
            tone=tone,
            adherence=adherence,
            reasoning=reasoning,
            notes=notes,
            flags=flags or [],
        )
        session.turns[turn_index - 1].grade = grade
        session.save()
        return grade

    @staticmethod
    def generate_report(session: EvalSession) -> Path:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"eval_session_{session.session_id}.md"

        total_turns = len(session.turns)
        graded_turns = [t for t in session.turns if t.grade is not None]

        if graded_turns:
            avg_acc = sum(t.grade.accuracy for t in graded_turns) / len(graded_turns)
            avg_tone = sum(t.grade.tone for t in graded_turns) / len(graded_turns)
            avg_adh = sum(t.grade.adherence for t in graded_turns) / len(graded_turns)
            avg_rsn = sum(t.grade.reasoning for t in graded_turns) / len(graded_turns)
            avg_comp = sum(t.grade.composite_score for t in graded_turns) / len(graded_turns)
        else:
            avg_acc = avg_tone = avg_adh = avg_rsn = avg_comp = 0.0

        all_flags = []
        for t in session.turns:
            if t.grade and t.grade.flags:
                all_flags.extend(t.grade.flags)
            if t.detected_anomalies:
                all_flags.extend([f"AUTO: {a}" for a in t.detected_anomalies])

        content = []
        content.append(f"# Qualitative Evaluation Report: `{session.session_id}`\n")
        content.append(f"- **Target Date**: {session.target_date.strftime('%A, %B %d, %Y')}")
        content.append(f"- **Simulated Clock Time**: {session.simulated_time_iso or 'Not specified'}")
        content.append(f"- **Persona / Scenario**: {session.persona}")
        content.append(f"- **Total Turns**: {total_turns} (Graded: {len(graded_turns)})\n")

        content.append("## Executive Scorecard\n")
        content.append("| Metric | Score (1–5) | Percentage | Assessment |")
        content.append("|---|---|---|---|")
        content.append(f"| **Accuracy & Factuality** | {avg_acc:.2f} / 5.0 | {(avg_acc/5)*100:.1f}% | {'Passing' if avg_acc >= 3.5 else 'Needs Improvement'} |")
        content.append(f"| **Adherence to Intent** | {avg_adh:.2f} / 5.0 | {(avg_adh/5)*100:.1f}% | {'Passing' if avg_adh >= 3.5 else 'Needs Improvement'} |")
        content.append(f"| **Tone & Posture** | {avg_tone:.2f} / 5.0 | {(avg_tone/5)*100:.1f}% | {'Passing' if avg_tone >= 3.5 else 'Needs Improvement'} |")
        content.append(f"| **Reasoning & Clarity** | {avg_rsn:.2f} / 5.0 | {(avg_rsn/5)*100:.1f}% | {'Passing' if avg_rsn >= 3.5 else 'Needs Improvement'} |")
        content.append(f"| **OVERALL COMPOSITE** | **{avg_comp/20:.2f} / 5.0** | **{avg_comp:.1f}%** | **{'PASS' if avg_comp >= 70.0 else 'FAIL'}** |\n")

        if all_flags:
            content.append("### Anomalies & Qualitative Flags")
            for f in sorted(set(all_flags)):
                content.append(f"- ⚠️ {f}")
            content.append("")

        content.append("## Turn-by-Turn Qualitative Breakdown\n")

        content.append(f"### Initial Greeting\n> **Assistant**:\n> {session.initial_greeting}\n")

        for t in session.turns:
            content.append(f"### Turn {t.turn_index}")
            content.append(f"**User**: `{t.user_message}`\n")
            content.append(f"**Assistant**:\n> {t.assistant_reply.replace(chr(10), chr(10) + '> ')}\n")

            if t.executed_tools:
                content.append("**Executed Directives**:")
                for tool in t.executed_tools:
                    content.append(f"- `{tool['name']}`: {tool['output']}")
                content.append("")

            content.append("**State Delta**:")
            diffs = t.delta_summary.get("block_diffs", [])
            if diffs:
                for d in diffs:
                    content.append(f"- {d}")
            else:
                content.append("- *No state change (Delta = 0)*")
            content.append("")

            if t.detected_anomalies:
                content.append("**Automated Flags**:")
                for a in t.detected_anomalies:
                    content.append(f"- 🚩 {a}")
                content.append("")

            if t.grade:
                content.append(f"**Judge Grade**: Accuracy: `{t.grade.accuracy}/5` | Adherence: `{t.grade.adherence}/5` | Tone: `{t.grade.tone}/5` | Reasoning: `{t.grade.reasoning}/5` (**{t.grade.composite_score}%**)")
                content.append(f"**Judge Critique**: {t.grade.notes}")
                if t.grade.flags:
                    content.append(f"**Manual Flags**: {', '.join(t.grade.flags)}")
            else:
                content.append("*Judge Grade: [Unrated]*")

            content.append("\n---\n")

        with open(report_path, "w") as f:
            f.write("\n".join(content))

        # Append to jsonl ledger
        ledger_path = REPORTS_DIR / "eval_sessions.jsonl"
        with open(ledger_path, "a") as f:
            summary = {
                "session_id": session.session_id,
                "target_date": session.target_date.isoformat(),
                "persona": session.persona,
                "total_turns": total_turns,
                "composite_score": avg_comp,
                "report_file": str(report_path),
                "timestamp": datetime.now().isoformat(),
            }
            f.write(json.dumps(summary) + "\n")

        return report_path


# ==============================================================================
# CLI Entry Point
# ==============================================================================

def format_turn_output(turn: TurnRecord, session_id: str) -> str:
    lines = []
    lines.append(f"================================================================================")
    lines.append(f"SESSION: {session_id} | TURN #{turn.turn_index}")
    lines.append(f"================================================================================")
    lines.append(f"USER: {turn.user_message}")
    lines.append(f"--------------------------------------------------------------------------------")
    lines.append(f"ASSISTANT:")
    lines.append(turn.assistant_reply)
    lines.append(f"--------------------------------------------------------------------------------")
    if turn.executed_tools:
        lines.append(f"EXECUTED DIRECTIVES:")
        for t in turn.executed_tools:
            lines.append(f"  • {t['name']}: {t['output']}")
    else:
        lines.append(f"EXECUTED DIRECTIVES: None")

    lines.append(f"STATE DELTA (Total Changes: {turn.delta_summary.get('total_changes', 0)}):")
    diffs = turn.delta_summary.get("block_diffs", [])
    if diffs:
        for d in diffs:
            lines.append(f"  • {d}")
    else:
        lines.append(f"  • No schedule blocks modified.")

    if turn.detected_anomalies:
        lines.append(f"AUTOMATED ANOMALY ALERTS:")
        for a in turn.detected_anomalies:
            lines.append(f"  🚩 {a}")

    lines.append(f"================================================================================")
    lines.append(f"To record grade for this turn:")
    lines.append(f"  python3 -m tests.interactive_eval grade --session {session_id} --turn {turn.turn_index} --accuracy <1-5> --tone <1-5> --adherence <1-5> --notes \"...\"")
    lines.append(f"================================================================================")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Interactive Qualitative LLM-as-a-Judge Evaluation Tool for Thirties.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. start
    p_start = subparsers.add_parser("start", help="Start a new interactive evaluation session.")
    p_start.add_argument("--id", type=str, default="", help="Custom session ID.")
    p_start.add_argument("--date", type=str, default="2026-10-09", help="Target planning date (YYYY-MM-DD).")
    p_start.add_argument("--time", type=str, default="", help="Simulated clock time (e.g. 2026-10-09T08:00:00-04:00).")
    p_start.add_argument("--persona", type=str, default="Interactive Human/Agent Evaluation", help="Persona/scenario description.")
    p_start.add_argument("--no-gpu", action="store_true", help="Disable persistent GPU daemon and use mock engine.")

    # 2. step
    p_step = subparsers.add_parser("step", help="Send a user turn and inspect response & state deltas.")
    p_step.add_argument("--session", type=str, required=True, help="Session ID.")
    p_step.add_argument("--message", type=str, required=True, help="User message to send.")
    p_step.add_argument("--json", action="store_true", help="Output raw JSON turn record.")
    p_step.add_argument("--no-gpu", action="store_true", help="Use mock engine instead of GPU.")

    # 3. grade
    p_grade = subparsers.add_parser("grade", help="Record qualitative grade for a turn.")
    p_grade.add_argument("--session", type=str, required=True, help="Session ID.")
    p_grade.add_argument("--turn", type=int, required=True, help="Turn index to grade.")
    p_grade.add_argument("--accuracy", type=int, choices=range(1, 6), required=True, help="Accuracy/Factuality (1-5).")
    p_grade.add_argument("--tone", type=int, choices=range(1, 6), required=True, help="Tone & Posture (1-5).")
    p_grade.add_argument("--adherence", type=int, choices=range(1, 6), required=True, help="Intent Adherence (1-5).")
    p_grade.add_argument("--reasoning", type=int, choices=range(1, 6), default=3, help="Reasoning & Clarity (1-5).")
    p_grade.add_argument("--notes", type=str, default="", help="Qualitative judge critique.")
    p_grade.add_argument("--flags", type=str, default="", help="Comma-separated flags.")

    # 4. status
    p_status = subparsers.add_parser("status", help="Inspect session status and blocks.")
    p_status.add_argument("--session", type=str, required=True, help="Session ID.")

    # 5. finish
    p_finish = subparsers.add_parser("finish", help="Finish session and generate Markdown scorecard.")
    p_finish.add_argument("--session", type=str, required=True, help="Session ID.")

    # 6. list
    p_list = subparsers.add_parser("list", help="List all sessions.")

    # 7. repl
    p_repl = subparsers.add_parser("repl", help="Start interactive REPL terminal loop.")
    p_repl.add_argument("--id", type=str, default="", help="Custom session ID.")
    p_repl.add_argument("--date", type=str, default="2026-10-09", help="Target planning date (YYYY-MM-DD).")
    p_repl.add_argument("--persona", type=str, default="REPL Interactive Session", help="Persona description.")
    p_repl.add_argument("--no-gpu", action="store_true", help="Use mock engine.")

    args = parser.parse_args()

    if args.subcommand == "start":
        sim_time = datetime.fromisoformat(args.time) if args.time else None
        target_d = date.fromisoformat(args.date)
        session = InteractiveEvaluator.start_session(
            target_date=target_d,
            persona=args.persona,
            simulated_time=sim_time,
            session_id=args.id,
            use_gpu=not args.no_gpu,
        )
        print(f"================================================================================")
        print(f"SESSION INITIALIZED: {session.session_id}")
        print(f"Target Date: {session.target_date.strftime('%A, %B %d, %Y')}")
        print(f"Persona: {session.persona}")
        print(f"--------------------------------------------------------------------------------")
        print(f"ASSISTANT GREETING:")
        print(session.initial_greeting)
        print(f"================================================================================")

    elif args.subcommand == "step":
        session = EvalSession.load(args.session)
        turn = InteractiveEvaluator.step_session(session, args.message, use_gpu=not args.no_gpu)
        if args.json:
            print(json.dumps(turn.to_dict(), indent=2))
        else:
            print(format_turn_output(turn, session.session_id))

    elif args.subcommand == "grade":
        session = EvalSession.load(args.session)
        flags = [f.strip() for f in args.flags.split(",") if f.strip()]
        grade = InteractiveEvaluator.grade_turn(
            session=session,
            turn_index=args.turn,
            accuracy=args.accuracy,
            tone=args.tone,
            adherence=args.adherence,
            reasoning=args.reasoning,
            notes=args.notes,
            flags=flags,
        )
        print(f"Recorded grade for Turn #{args.turn} in session '{session.session_id}':")
        print(f"Accuracy: {grade.accuracy}/5 | Tone: {grade.tone}/5 | Adherence: {grade.adherence}/5 | Reasoning: {grade.reasoning}/5")
        print(f"Composite: {grade.composite_score}%")
        print(f"Critique: {grade.notes}")

    elif args.subcommand == "status":
        session = EvalSession.load(args.session)
        day_plan = DayPlan.from_dict(session.day_plan_dict) if session.day_plan_dict else None
        print(f"Session: {session.session_id} | Date: {session.target_date} | Turns: {len(session.turns)}")
        if day_plan:
            work_blocks = [day_plan.get_logical_index(b) for b in day_plan.blocks if b.kind == BlockKind.WORK]
            sleep_blocks = [day_plan.get_logical_index(b) for b in day_plan.blocks if b.kind == BlockKind.SLEEP]
            assigned_blocks = [day_plan.get_logical_index(b) for b in day_plan.blocks if b.is_assigned or (b.label and b.kind not in (BlockKind.WORK, BlockKind.SLEEP))]
            print(f"Work Blocks: {work_blocks or 'None'}")
            print(f"Sleep Blocks: {sleep_blocks or 'None'}")
            print(f"Assigned Task Blocks: {assigned_blocks or 'None'}")
            print(f"Daylight Available: {day_plan.daylight_available_count} | Dark Available: {day_plan.dark_available_count}")

    elif args.subcommand == "finish":
        session = EvalSession.load(args.session)
        report_path = InteractiveEvaluator.generate_report(session)
        print(f"Evaluation session '{session.session_id}' completed!")
        print(f"Report saved to: {report_path}")

    elif args.subcommand == "list":
        if not SESSIONS_DIR.exists():
            print("No evaluation sessions found.")
            return
        sessions = sorted([d.name for d in SESSIONS_DIR.iterdir() if d.is_dir()])
        print(f"Total sessions ({len(sessions)}):")
        for s in sessions:
            print(f"  • {s}")

    elif args.subcommand == "repl":
        sim_time = None
        target_d = date.fromisoformat(args.date)
        session = InteractiveEvaluator.start_session(
            target_date=target_d,
            persona=args.persona,
            session_id=args.id,
            use_gpu=not args.no_gpu,
        )
        print(f"=== INTERACTIVE EVAL REPL: {session.session_id} ===")
        print(f"Target Date: {session.target_date.strftime('%A, %B %d, %Y')}")
        print(f"Greeting: {session.initial_greeting}")
        print("Commands: Type your user message to talk to bot.")
        print("          /grade a=<1-5> t=<1-5> adh=<1-5> n=\"...\"")
        print("          /status")
        print("          /finish")
        print("--------------------------------------------------------------------------------")

        while True:
            try:
                user_input = input("\n[User] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            if user_input == "/finish":
                p = InteractiveEvaluator.generate_report(session)
                print(f"Report saved to {p}")
                break
            elif user_input == "/status":
                day_plan = DayPlan.from_dict(session.day_plan_dict) if session.day_plan_dict else None
                if day_plan:
                    work_blocks = [day_plan.get_logical_index(b) for b in day_plan.blocks if b.kind == BlockKind.WORK]
                    print(f"Work Blocks: {work_blocks}")
                continue
            elif user_input.startswith("/grade"):
                # e.g. /grade a=4 t=5 adh=3 n="critique"
                m_a = re.search(r"a=(\d)", user_input)
                m_t = re.search(r"t=(\d)", user_input)
                m_adh = re.search(r"adh=(\d)", user_input)
                m_n = re.search(r'n="([^"]+)"', user_input)
                a = int(m_a.group(1)) if m_a else 3
                t = int(m_t.group(1)) if m_t else 3
                adh = int(m_adh.group(1)) if m_adh else 3
                n = m_n.group(1) if m_n else ""
                turn_idx = len(session.turns)
                if turn_idx > 0:
                    InteractiveEvaluator.grade_turn(session, turn_idx, a, t, adh, notes=n)
                    print(f"Graded Turn #{turn_idx}: Accuracy={a}, Tone={t}, Adherence={adh}")
                else:
                    print("No turns yet to grade.")
                continue

            turn = InteractiveEvaluator.step_session(session, user_input, use_gpu=not args.no_gpu)
            print(format_turn_output(turn, session.session_id))


if __name__ == "__main__":
    main()
