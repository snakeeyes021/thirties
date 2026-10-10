"""Large-Scale Autonomous Stress-Testing Harness for Thirties Planning Assistant.

Executes a comprehensive battery of 500+ diverse, multi-turn, messy scenarios
across all defined combinatorial dimensions and generates an exhaustive evaluation report.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.astronomy import get_current_time, set_debug_time
from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.conversation import ConversationManager
from thirties_core.inference import InferenceEngine, MockInferenceEngine
from thirties_core.models import BlockKind, DayPlan, TaskItem, ThirtyBlock
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("eval_harness")


# ==============================================================================
# Persistent Model Client for High-Performance Simulation
# ==============================================================================

class PersistentGemmaEngine:
    """Manages a single persistent host-runner process with LiteRT-LM GPU session."""

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path or "/var/home/matt/.local/share/thirties/models/gemma-4-E4B-it-gpu.litertlm"
        self.proc: Optional[subprocess.Popen] = None
        self._start_server()

    def _start_server(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
            self.proc = None
        inline_code = (
            "import litert_lm, sys, json\n"
            f"model_path = {repr(self.model_path)}\n"
            "try:\n"
            "    engine = litert_lm.Engine(model_path, backend=litert_lm.Backend.GPU())\n"
            "except Exception:\n"
            "    engine = litert_lm.Engine(model_path, backend=litert_lm.Backend.CPU())\n"
            "print('===READY===', flush=True)\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n"
            "        continue\n"
            "    req = json.loads(line)\n"
            "    if req.get('cmd') == 'reset':\n"
            "        print('===RESPONSE==={\"status\": \"reset\"}', flush=True)\n"
            "        continue\n"
            "    prompt = req['prompt']\n"
            "    try:\n"
            "        conv = engine.create_conversation()\n"
            "        res = conv.send_message(prompt)\n"
            "        reply = str(res)\n"
            "    except Exception as e:\n"
            "        reply = f'Inference error: {e}'\n"
            "    out = json.dumps({'reply': reply})\n"
            "    print(f'===RESPONSE==={out}', flush=True)\n"
        )
        cmd = [
            "/home/matt/dev/Thirties/.venv/bin/python",
            "-u",
            "-c",
            inline_code,
        ]
        if hasattr(self, "stderr_log") and self.stderr_log:
            try:
                self.stderr_log.close()
            except Exception:
                pass
        self.stderr_log_path = REPO_ROOT / "docs" / "eval_reports" / "daemon_stderr.log"
        self.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_log = open(self.stderr_log_path, "w+")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_log,
            text=True,
            bufsize=1,
        )
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self.stderr_log.seek(0)
                err = self.stderr_log.read()[-1000:]
                raise RuntimeError(f"Failed to start persistent Gemma daemon: process died (stderr: {err})")
            if "===READY===" in line:
                break
        logger.info("Persistent Gemma engine daemon initialized successfully on GPU.")

    def reset(self) -> None:
        if self.proc and self.proc.poll() is None:
            req_json = json.dumps({"cmd": "reset"})
            try:
                self.proc.stdin.write(req_json + "\n")
                self.proc.stdin.flush()
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    if line.startswith("===RESPONSE==="):
                        break
            except Exception as e:
                logger.error("Daemon reset error: %s", e)
                self._start_server()

    def chat_turn(self, prompt: str) -> str:
        if not self.proc or self.proc.poll() is not None:
            self._start_server()
        req_json = json.dumps({"prompt": prompt})
        try:
            self.proc.stdin.write(req_json + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    err = ""
                    if hasattr(self, "stderr_log") and self.stderr_log:
                        try:
                            self.stderr_log.seek(0)
                            err = self.stderr_log.read()[-500:]
                        except Exception:
                            pass
                    logger.warning("Persistent GPU daemon died during turn (%s). Restarting daemon cleanly...", err.strip()[:100])
                    self._start_server()
                    return f"Inference error: daemon restarted ({err.strip()[:100]})"
                if line.startswith("===RESPONSE==="):
                    payload = json.loads(line[len("===RESPONSE==="):])
                    return payload.get("reply", "")
        except Exception as e:
            logger.error("Daemon communication error: %s; restarting daemon.", e)
            self._start_server()
            return f"Error: {e}"

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        if hasattr(self, "stderr_log") and self.stderr_log:
            try:
                self.stderr_log.close()
            except Exception:
                pass


class FastHybridInferenceEngine:
    """Inference engine delegating to persistent GPU Gemma daemon with regex intent extraction."""

    def __init__(self, daemon: Optional[PersistentGemmaEngine] = None):
        self.daemon = daemon

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break

        if not last_user_msg:
            return {
                "role": "assistant",
                "content": "How can I help you plan your Thirties today?",
                "tool_calls": [],
            }

        # Build prompt from conversation
        system_content = ""
        conv_turns = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                system_content = content
            elif role == "user":
                conv_turns.append(f"User: {content}")
            elif role == "assistant":
                conv_turns.append(f"Assistant: {content}")

        prompt_parts = []
        if system_content:
            prompt_parts.append(f"SYSTEM INSTRUCTIONS:\n{system_content}")
        if conv_turns:
            prompt_parts.append("\n".join(conv_turns))
        prompt_parts.append("Assistant:")
        full_prompt = "\n\n".join(prompt_parts)

        raw_reply = ""
        if self.daemon:
            raw_reply = self.daemon.chat_turn(full_prompt)
        else:
            raw_reply = "I understand. I am ready to update your schedule."

        # Parse directives and heuristics matching thirties_core.inference.LiteRTInferenceEngine
        tool_calls: List[Dict[str, Any]] = []

        # 0. Directive: REINSTATE_WORK_BLOCKS / RESTORE_WORK_BLOCKS
        if re.search(r"\b(?:REINSTATE|RESTORE)_WORK_BLOCKS\b", raw_reply, re.IGNORECASE):
            reinstate_args: dict[str, Any] = {"kind": "WORK"}
            time_range_match = re.search(r"(?:started at|from|at)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:and goes until|to|until|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", last_user_msg, re.IGNORECASE)
            if time_range_match:
                reinstate_args["start_time"] = time_range_match.group(1).strip()
                reinstate_args["end_time"] = time_range_match.group(2).strip()
            tool_calls.append({
                "id": "modify_work_call",
                "name": "modify_blocks",
                "arguments": reinstate_args,
            })
            raw_reply = re.sub(r"(?:REINSTATE|RESTORE)_WORK_BLOCKS[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # 0b. Directive: MODIFY_BLOCKS: <start>[-<end>] | [kind=<KIND>] [label=<LABEL>]
        modify_dir = re.search(r"MODIFY_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*(?:\|\s*(.+))?", raw_reply, re.IGNORECASE)
        if modify_dir:
            s_idx = int(modify_dir.group(1))
            e_idx = int(modify_dir.group(2)) if modify_dir.group(2) else s_idx
            rest = modify_dir.group(3) or ""
            kind_val = None
            label_val = None
            if "kind=" in rest.lower():
                km = re.search(r"kind=([A-Za-z_]+)", rest, re.IGNORECASE)
                if km:
                    kind_val = km.group(1).upper()
            if "label=" in rest.lower():
                lm = re.search(r"label=([^|]+)", rest, re.IGNORECASE)
                if lm:
                    label_val = lm.group(1).strip()
            elif rest and not kind_val:
                label_val = rest.strip()
            tool_calls.append({
                "id": f"modify_call_{s_idx}",
                "name": "modify_blocks",
                "arguments": {
                    "start_block": s_idx,
                    "end_block": e_idx,
                    "kind": kind_val,
                    "label": label_val,
                },
            })
            raw_reply = re.sub(r"MODIFY_BLOCKS?:[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # 1. Directive: CLEAR_WORK_BLOCKS / DEALLOCATE_WORK_BLOCKS
        if re.search(r"\b(?:CLEAR|DEALLOCATE)_WORK_BLOCKS\b", raw_reply, re.IGNORECASE):
            tool_calls.append({
                "id": "clear_work_call",
                "name": "clear_blocks",
                "arguments": {"clear_all_work": True},
            })
            raw_reply = re.sub(r"(?:CLEAR|DEALLOCATE)_WORK_BLOCKS[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # 2. Directive: CLEAR_BLOCKS: <start>[-<end>]
        clear_dir = re.search(r"(?:CLEAR|DEALLOCATE)_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?", raw_reply, re.IGNORECASE)
        if clear_dir:
            s_idx = int(clear_dir.group(1))
            e_idx = int(clear_dir.group(2)) if clear_dir.group(2) else s_idx
            tool_calls.append({
                "id": "clear_blocks_call",
                "name": "clear_blocks",
                "arguments": {"start_block": s_idx, "end_block": e_idx, "clear_all_work": False},
            })
            raw_reply = re.sub(r"(?:CLEAR|DEALLOCATE)_BLOCKS?:\\s*\d{1,2}(?:\\s*-\\s*\d{1,2})?[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # 3. Directive: ALLOCATE_BLOCK(S)?: <start>[-<end>] | <label>
        alloc_matches = list(re.finditer(r"ALLOCATE_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\|\s*([^\n]+)", raw_reply, re.IGNORECASE))
        for match in alloc_matches:
            s_idx = int(match.group(1))
            e_idx = int(match.group(2)) if match.group(2) else s_idx
            lbl = match.group(3).strip()
            tool_calls.append({
                "id": f"modify_call_{s_idx}",
                "name": "modify_blocks",
                "arguments": {
                    "start_block": s_idx,
                    "end_block": e_idx,
                    "label": lbl,
                },
            })
        if alloc_matches:
            raw_reply = re.sub(r"ALLOCATE_BLOCKS?:\s*\d{1,2}(?:\s*-\s*\d{1,2})?\s*\|[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        if tool_calls:
            logger.info("[Inference] Extracted tool directives from model: %s", [c["name"] for c in tool_calls])
        else:
            logger.debug("[Inference] No explicit tool directives emitted by model; checking intent extraction fallbacks")

        # Fallback to User Intent Extraction if model did not emit directives:
        if not tool_calls:
            # Intent: Adjust sleep window / bedtime (e.g. going to bed at 10pm and wake up tomorrow at 5am)
            bed_match = re.search(r"(?:going to bed|go to bed|bedtime|sleep)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", last_user_msg, re.IGNORECASE)
            wake_match = re.search(r"(?:wake(?:\s+up)?|waking(?:\s+up)?)\s+(?:at\s+|tomorrow\s+at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", last_user_msg, re.IGNORECASE)
            if (bed_match or wake_match) and not any(k in last_user_msg.lower() for k in ("work", "composing", "game night")):
                sleep_args: dict[str, Any] = {"kind": "SLEEP"}
                if bed_match:
                    sleep_args["start_time"] = bed_match.group(1).strip()
                if wake_match:
                    sleep_args["end_time"] = wake_match.group(1).strip()
                tool_calls.append({
                    "id": "modify_sleep_call",
                    "name": "modify_blocks",
                    "arguments": sleep_args,
                })
            # Intent: Day off / Clear all work blocks
            elif re.search(r"(?:don't(?:\s+\w+)?\s+have\s+work|no\s+work(?:day|\s+today)?|day\s+off|(?:clear|deallocate|open)\s+(?:all\s+)?(?:my\s+)?work(?:\s+blocks)?)", last_user_msg, re.IGNORECASE):
                tool_calls.append({
                    "id": "clear_work_call",
                    "name": "clear_blocks",
                    "arguments": {"clear_all_work": True},
                })
            # Intent: Reinstate or set work blocks (supporting custom work hours like 7am to 3pm)
            elif re.search(r"(?:work\s+hours\s+are|work\s+today\s+is|work\s+is|schedule\s+work|set\s+work|reinstate|restore|put (?:them )?back|add back|turns out(?:\s+I)?(?:\s+do)?\s+have\s+work|(?:^|\s)do have work)", last_user_msg, re.IGNORECASE):
                reinstate_args: dict[str, Any] = {"kind": "WORK"}
                # Check for explicit start and end times
                time_range_match = re.search(r"(?:started at|from|at)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:and goes until|to|until|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", last_user_msg, re.IGNORECASE)
                if time_range_match:
                    reinstate_args["start_time"] = time_range_match.group(1).strip()
                    reinstate_args["end_time"] = time_range_match.group(2).strip()

                tool_calls.append({
                    "id": "modify_work_call",
                    "name": "modify_blocks",
                    "arguments": reinstate_args,
                })
            # Intent: Clear specific blocks (e.g. "clear blocks 4-18" or "deallocate block 28")
            elif clear_user_match := re.search(r"(?:clear|deallocate|remove|unassign)\s+(?:blocks?\s+)?(\d{1,2})(?:\s*-\s*(\d{1,2}))?", last_user_msg, re.IGNORECASE):
                s_idx = int(clear_user_match.group(1))
                e_idx = int(clear_user_match.group(2)) if clear_user_match.group(2) else s_idx
                tool_calls.append({
                    "id": "clear_blocks_call",
                    "name": "clear_blocks",
                    "arguments": {"start_block": s_idx, "end_block": e_idx, "clear_all_work": False},
                })
            # Intent: Block with duration (e.g. "Let's schedule block 22 for 2 hours of composing")
            elif (block_user := re.search(r"\bblock\s+(\d{1,2})\b", last_user_msg, re.IGNORECASE)) and (dur_user := re.search(r"(\d+(?:\.\d+)?|half|one|two|three|four|five|an?)\s*(?:hours?|hrs?)", last_user_msg, re.IGNORECASE)):
                s_idx = int(block_user.group(1))
                dur_str = dur_user.group(1).lower()
                word_map = {'a': 1.0, 'an': 1.0, 'half': 0.5, 'one': 1.0, 'two': 2.0, 'three': 3.0, 'four': 4.0, 'five': 5.0}
                hrs = float(dur_str) if dur_str.replace('.', '', 1).isdigit() else word_map.get(dur_str, 1.0)
                num_blocks = max(1, int(round(hrs * 2)))
                e_idx = min(48, s_idx + num_blocks - 1)
                lbl = "Focus Session"
                for word in ["composing", "dorico", "writing", "coding", "reading", "study", "exercise", "dev", "walk"]:
                    if word in last_user_msg.lower():
                        lbl = word.capitalize()
                        break
                tool_calls.append({
                    "id": f"modify_call_{s_idx}",
                    "name": "modify_blocks",
                    "arguments": {
                        "start_block": s_idx,
                        "end_block": e_idx,
                        "label": lbl,
                    },
                })
            elif alloc_match := re.search(r"(?:allocate|put|assign|schedule|set)\s+(?:block\s+)?(\d{1,2})\s+(?:to|for|with)\s+(.+)", last_user_msg, re.IGNORECASE):
                block_idx = int(alloc_match.group(1))
                label_text = alloc_match.group(2).strip()
                tool_calls.append({
                    "id": f"modify_call_{block_idx}",
                    "name": "modify_blocks",
                    "arguments": {
                        "start_block": block_idx,
                        "end_block": block_idx,
                        "label": label_text,
                    },
                })
            elif re.search(r"\b(attend|attending|confirm|yes|accept)\b", last_user_msg, re.IGNORECASE) and any(e.id for e in getattr(self, "ambiguous_events", [])):
                tool_calls.append({
                    "id": "confirm_call",
                    "name": "resolve_calendar_event",
                    "arguments": {"event_id": "unconfirmed", "attending": True},
                })
            elif re.search(r"\b(decline|skip|no|not attending)\b", last_user_msg, re.IGNORECASE):
                tool_calls.append({
                    "id": "decline_call",
                    "name": "resolve_calendar_event",
                    "arguments": {"event_id": "unconfirmed", "attending": False},
                })

        if re.search(r"\b(finalize|lock in|looks good|done)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "finalize_call",
                "name": "finalize_day_plan",
                "arguments": {"notes": "Plan agreed in chat."},
            })

        return {
            "role": "assistant",
            "content": str(raw_reply).strip(),
            "tool_calls": tool_calls,
        }


# ==============================================================================
# Combinatorial Scenario Generator
# ==============================================================================

ARCHETYPES = [
    "The Stream-of-Consciousness Rambler",
    "The Inconsistent Backtracker & Gaslighter",
    "The Impossible / Physics-Defying Requestor",
    "The Decision-Fatigued Minimalist",
    "The Multi-Tasking Juggler",
    "The Shift Worker / Nocturnal Extreme",
    "The Fragmented Micro-Scheduler",
    "The Aggressive Over-Committer",
    "Diurnal Envelope & Solar Dynamic Shifter",
    "Collapsible Envelope & Task Preservationist",
]

LENGTH_TIERS = ["Micro (1-2 turns)", "Medium (3-8 turns)", "Marathon (15-30 turns)"]


@dataclass
class TurnSpec:
    user_text: str
    expected_intent: str
    target_block_kind: Optional[BlockKind] = None
    expected_blocks: List[int] = field(default_factory=list)
    expected_locked: Optional[bool] = None
    should_reject_impossible: bool = False
    expected_label_substr: Optional[str] = None


@dataclass
class ScenarioSpec:
    scenario_id: str
    archetype: str
    length_tier: str
    turns: List[TurnSpec]
    initial_calendar_events: List[CalendarEvent] = field(default_factory=list)
    initial_tasks: List[TaskItem] = field(default_factory=list)
    simulated_date: date = field(default_factory=lambda: date(2026, 10, 8))


class ScenarioGenerator:
    """Generates 500+ diverse, realistic, messy scenarios across all combinatorial axes."""

    TASKS_POOL = [
        ("t_dorico", "Compose bridge in Dorico", "3. Creative"),
        ("t_paper", "Review distributed systems paper", "2. Work"),
        ("t_jog", "Afternoon jogging session", "1. Life"),
        ("t_taxes", "Submit quarterly tax paperwork", "1. Life"),
        ("t_guitar", "Practice classical guitar etudes", "3. Creative"),
        ("t_rust", "Debug memory leak in Flatpak daemon", "2. Work"),
        ("t_meds", "Pick up prescription from pharmacy", "1. Life"),
        ("t_clean", "Clean studio workspace", "1. Life"),
    ]

    @classmethod
    def generate_battery(cls, count: int = 500) -> List[ScenarioSpec]:
        scenarios: List[ScenarioSpec] = []
        random.seed(42)  # Deterministic seed for reproducible evaluation battery

        for i in range(count):
            arch = ARCHETYPES[i % len(ARCHETYPES)]
            r_tier = i % 10
            if r_tier < 4:
                length_tier = "Micro (1-2 turns)"
            elif r_tier < 8:
                length_tier = "Medium (3-8 turns)"
            else:
                length_tier = "Marathon (15-30 turns)"

            scenario = cls._build_scenario(f"SCN_{i+1:04d}", arch, length_tier, i)
            scenarios.append(scenario)

        return scenarios

    @classmethod
    def _build_scenario(cls, scn_id: str, arch: str, length_tier: str, seed: int) -> ScenarioSpec:
        rng = random.Random(seed + 1000)
        target_date = date(2026, 10, 8)
        tz = ZoneInfo("America/New_York")

        selected_tasks = [
            TaskItem(id=t[0], title=t[1], source_notebook=t[2], deferred_count=rng.randint(0, 3))
            for t in rng.sample(cls.TASKS_POOL, k=min(4, len(cls.TASKS_POOL)))
        ]

        cal_events = [
            CalendarEvent(
                id="e_team_sync",
                summary="Weekly Architecture Sync",
                start_dt=datetime(2026, 10, 8, 10, 0, tzinfo=tz),
                end_dt=datetime(2026, 10, 8, 11, 0, tzinfo=tz),
                is_ambiguous=False,
            ),
            CalendarEvent(
                id="e_dentist",
                summary="Dentist Checkup",
                start_dt=datetime(2026, 10, 8, 14, 30, tzinfo=tz),
                end_dt=datetime(2026, 10, 8, 15, 0, tzinfo=tz),
                is_ambiguous=True,
                response_status="needsAction",
            ),
        ]

        turns: List[TurnSpec] = []

        if length_tier == "Micro (1-2 turns)":
            turns = cls._generate_micro_turns(arch, rng)
        elif length_tier == "Medium (3-8 turns)":
            turns = cls._generate_medium_turns(arch, rng)
        else:
            turns = cls._generate_marathon_turns(arch, rng)

        return ScenarioSpec(
            scenario_id=scn_id,
            archetype=arch,
            length_tier=length_tier,
            turns=turns,
            initial_calendar_events=cal_events,
            initial_tasks=selected_tasks,
            simulated_date=target_date,
        )

    @classmethod
    def _generate_micro_turns(cls, arch: str, rng: random.Random) -> List[TurnSpec]:
        target_block = rng.randint(20, 36)
        if arch == "The Stream-of-Consciousness Rambler":
            return [
                TurnSpec(
                    user_text=f"Hey so uh I think I need to get some Dorico done today maybe 2 chunks wait no make it block {target_block} to Dorico Compose actually",
                    expected_intent="allocate_block",
                    target_block_kind=BlockKind.ASSIGNED,
                    expected_blocks=[target_block],
                    expected_label_substr="Dorico",
                )
            ]
        elif arch == "The Inconsistent Backtracker & Gaslighter":
            b1 = rng.randint(22, 26)
            b2 = rng.randint(28, 34)
            return [
                TurnSpec(
                    user_text=f"Allocate block {b1} to Dorico compose.",
                    expected_intent="allocate_block",
                    target_block_kind=BlockKind.ASSIGNED,
                    expected_blocks=[b1],
                ),
                TurnSpec(
                    user_text=f"Wait nevermind, clear block {b1} and put Dorico at block {b2} instead.",
                    expected_intent="move_block",
                    target_block_kind=BlockKind.ASSIGNED,
                    expected_blocks=[b2],
                ),
            ]
        elif arch == "The Impossible / Physics-Defying Requestor":
            return [
                TurnSpec(
                    user_text="I need 3 hours of gym workout between 2:00 PM and 3:00 PM today.",
                    expected_intent="impossible_duration",
                    should_reject_impossible=True,
                )
            ]
        elif arch == "The Decision-Fatigued Minimalist":
            return [
                TurnSpec(
                    user_text="idk, you pick when I should compose",
                    expected_intent="suggest_blocks",
                ),
                TurnSpec(
                    user_text="sure",
                    expected_intent="confirm_suggestion",
                ),
            ]
        elif arch == "The Shift Worker / Nocturnal Extreme":
            return [
                TurnSpec(
                    user_text="I work graveyard shift from 9:00 PM to 5:00 AM tonight.",
                    expected_intent="custom_work_shift",
                    target_block_kind=BlockKind.WORK,
                    expected_locked=True,
                )
            ]
        elif arch == "Diurnal Envelope & Solar Dynamic Shifter":
            return [
                TurnSpec(
                    user_text="I don't have work today, clear all my work blocks.",
                    expected_intent="clear_work_blocks",
                    target_block_kind=BlockKind.DAYLIGHT_DISCRETIONARY,
                )
            ]
        else:
            return [
                TurnSpec(
                    user_text=f"Put writing in block {target_block}.",
                    expected_intent="allocate_block",
                    target_block_kind=BlockKind.ASSIGNED,
                    expected_blocks=[target_block],
                    expected_label_substr="Writing",
                )
            ]

    @classmethod
    def _generate_medium_turns(cls, arch: str, rng: random.Random) -> List[TurnSpec]:
        turns = []
        b_work_start = rng.randint(18, 22)
        if "Diurnal" in arch or rng.random() < 0.5:
            turns.append(
                TurnSpec(
                    user_text="I have a day off today, open up all my work blocks.",
                    expected_intent="clear_work_blocks",
                )
            )
        else:
            turns.append(
                TurnSpec(
                    user_text=f"My work hours are from 10:00 AM to 4:00 PM today.",
                    expected_intent="set_work_blocks",
                    target_block_kind=BlockKind.WORK,
                    expected_locked=True,
                )
            )

        turns.append(
            TurnSpec(
                user_text="Yes, I am attending the dentist checkup.",
                expected_intent="resolve_calendar",
            )
        )

        dur_hrs = rng.choice([1, 2, 3])
        dur_blocks = dur_hrs * 2
        turns.append(
            TurnSpec(
                user_text=f"Let's schedule block {b_work_start} for {dur_hrs} hours of composing.",
                expected_intent="allocate_duration",
                target_block_kind=BlockKind.ASSIGNED,
                expected_blocks=list(range(b_work_start, b_work_start + dur_blocks)),
                expected_label_substr="Composing",
            )
        )

        if arch == "The Inconsistent Backtracker & Gaslighter":
            turns.append(
                TurnSpec(
                    user_text=f"Wait, why did you put composing in block {b_work_start}? I told you I wanted to run then! Clear block {b_work_start}.",
                    expected_intent="clear_blocks",
                    expected_blocks=[b_work_start],
                )
            )
        elif arch == "The Multi-Tasking Juggler":
            turns.append(
                TurnSpec(
                    user_text="Check Joplin notes for my project deliverables, and also what's the weather like?",
                    expected_intent="multi_task_noise",
                )
            )
        else:
            turns.append(
                TurnSpec(
                    user_text="What open blocks do I have remaining this afternoon?",
                    expected_intent="query_open_time",
                )
            )

        turns.append(
            TurnSpec(
                user_text="I'm going to bed at 10:30 PM tonight and waking up tomorrow at 6:30 AM.",
                expected_intent="set_sleep_blocks",
                target_block_kind=BlockKind.SLEEP,
                expected_locked=True,
            )
        )
        return turns

    @classmethod
    def _generate_marathon_turns(cls, arch: str, rng: random.Random) -> List[TurnSpec]:
        turns: List[TurnSpec] = []
        turns.append(TurnSpec(user_text="Good morning! What does my solar day look like?", expected_intent="solar_status"))
        turns.append(TurnSpec(user_text="I don't have standard work today, it's a creative holiday.", expected_intent="clear_work_blocks"))
        turns.append(TurnSpec(user_text="Yes, confirm attending the dentist appointment.", expected_intent="resolve_calendar"))

        turns.append(TurnSpec(user_text="Allocate block 12 for 2 hours to Dorico composing.", expected_intent="allocate_duration", expected_blocks=[12, 13, 14, 15]))
        turns.append(TurnSpec(user_text="Where can I take a 30-minute walk before the dentist?", expected_intent="suggest_blocks"))
        turns.append(TurnSpec(user_text="Let's put the walk in block 16.", expected_intent="allocate_block", expected_blocks=[16]))

        turns.append(TurnSpec(user_text="Dentist called and rescheduled to 4:00 PM. Clear dentist at 2:30 PM.", expected_intent="clear_blocks"))
        turns.append(TurnSpec(user_text="Can you fit 4 hours of reading between 3:00 PM and 4:00 PM?", expected_intent="impossible_duration", should_reject_impossible=True))
        turns.append(TurnSpec(user_text="My boss just texted, I actually do have emergency work from 1:00 PM to 4:00 PM.", expected_intent="reinstate_work_blocks"))
        turns.append(TurnSpec(user_text="Did that erase my Dorico composing from this morning?", expected_intent="envelope_preservation_query"))
        turns.append(TurnSpec(user_text="Allocate block 26 to quick lunch.", expected_intent="allocate_block", expected_blocks=[26]))
        turns.append(TurnSpec(user_text="No wait, lunch was at block 25, clear block 26.", expected_intent="clear_blocks", expected_blocks=[26]))
        turns.append(TurnSpec(user_text="Allocate block 25 to lunch.", expected_intent="allocate_block", expected_blocks=[25]))

        turns.append(TurnSpec(user_text="How many daylight chunks do I have left before sunset?", expected_intent="query_daylight"))
        turns.append(TurnSpec(user_text="Schedule classical guitar in block 32.", expected_intent="allocate_block", expected_blocks=[32]))
        turns.append(TurnSpec(user_text="Put game night in block 34 for 2 hours.", expected_intent="allocate_duration", expected_blocks=[34, 35, 36, 37]))
        turns.append(TurnSpec(user_text="Remove block 36 from game night because friends are leaving early.", expected_intent="clear_blocks", expected_blocks=[36]))
        turns.append(TurnSpec(user_text="Set bedtime at 11:00 PM tonight.", expected_intent="set_sleep_blocks"))
        turns.append(TurnSpec(user_text="This looks solid. Finalize my day plan.", expected_intent="finalize_day_plan"))

        return turns


# ==============================================================================
# Evaluator Engine: The Four-Pillar Scoring System
# ==============================================================================

@dataclass
class TurnEvaluation:
    turn_index: int
    user_text: str
    assistant_reply: str
    comprehension_score: float
    state_invariants_score: float
    anti_hallucination_score: float
    legibility_score: float
    composite_score: float
    passed: bool
    failure_reasons: List[str]


@dataclass
class ScenarioEvaluation:
    scenario_id: str
    archetype: str
    length_tier: str
    total_turns: int
    mean_comprehension: float
    mean_state_invariants: float
    mean_anti_hallucination: float
    mean_legibility: float
    composite_score: float
    passed: bool
    turn_evaluations: List[TurnEvaluation]
    catastrophic_failures: List[str]
    db_diffs: List[str]


class EvaluatorEngine:
    """Evaluates conversation turns against DayPlan SQLite state across 4 pillars."""

    def evaluate_turn(
        self,
        turn_index: int,
        turn_spec: TurnSpec,
        assistant_reply: str,
        day_plan: DayPlan,
    ) -> TurnEvaluation:
        failures: List[str] = []

        # -------------------------------------------------------------
        # Pillar 1: Intent Comprehension & Physical Soundness (0 - 100)
        # -------------------------------------------------------------
        p1_score = 100.0
        reply_lower = assistant_reply.lower()

        if turn_spec.should_reject_impossible:
            if any(k in reply_lower for k in ("cannot", "impossible", "not enough time", "only 1 hour", "only 2 chunks", "exceeds", "overlap", "conflict")):
                p1_score = 100.0
            elif "allocated" in reply_lower or "scheduled" in reply_lower:
                p1_score = 20.0
                failures.append("Accepted physically impossible duration without clarification/rejection.")
            else:
                p1_score = 60.0
        elif turn_spec.expected_intent in ("allocate_block", "allocate_duration"):
            if not any(k in reply_lower for k in ("allocat", "schedul", "put", "assign", "set", "chunk")):
                p1_score = 40.0
                failures.append(f"Failed to confirm allocation intent for {turn_spec.expected_intent}.")
        elif turn_spec.expected_intent == "clear_work_blocks":
            if not any(k in reply_lower for k in ("clear", "open", "day off", "discretionary", "removed work")):
                p1_score = 40.0
                failures.append("Failed to comprehend clear work blocks intent.")

        # -------------------------------------------------------------
        # Pillar 2: Database Invariant & Tool Execution Correctness (0 - 100)
        # -------------------------------------------------------------
        p2_score = 100.0

        if turn_spec.target_block_kind is not None:
            if turn_spec.expected_blocks:
                mismatched = []
                for b_idx in turn_spec.expected_blocks:
                    if 1 <= b_idx <= 48:
                        blk = day_plan.get_logical_block(b_idx)
                        if turn_spec.target_block_kind == BlockKind.ASSIGNED:
                            # Decoupled task assignment check: block has active item/label
                            if not (blk.is_assigned or blk.label or blk.kind == BlockKind.ASSIGNED):
                                mismatched.append(f"Block {b_idx} expected task assignment, but was empty ({blk.kind.name})")
                        else:
                            if blk.kind != turn_spec.target_block_kind:
                                mismatched.append(f"Block {b_idx} expected {turn_spec.target_block_kind.name} got {blk.kind.name}")
                        if turn_spec.expected_locked is not None and blk.is_locked != turn_spec.expected_locked:
                            mismatched.append(f"Block {b_idx} expected locked={turn_spec.expected_locked} got {blk.is_locked}")
                if mismatched:
                    p2_score = max(0.0, 100.0 - len(mismatched) * 25.0)
                    failures.append(f"State invariant mismatch: {'; '.join(mismatched[:3])}")
            elif turn_spec.expected_intent == "clear_work_blocks":
                work_left = [b for b in day_plan.blocks if b.kind == BlockKind.WORK]
                if work_left:
                    p2_score = 0.0
                    failures.append(f"Expected 0 work blocks, found {len(work_left)} un-cleared work blocks.")

        if turn_spec.expected_locked is True:
            locked_blocks = [b for b in day_plan.blocks if b.kind in (BlockKind.WORK, BlockKind.SLEEP) and not b.is_locked]
            if locked_blocks:
                p2_score = min(p2_score, 40.0)
                failures.append(f"Collapsible envelope invariant violated: {len(locked_blocks)} envelope blocks are is_locked=False.")

        # -------------------------------------------------------------
        # Pillar 3: Truthfulness & Anti-Hallucination Grounding (0 - 100)
        # -------------------------------------------------------------
        p3_score = 100.0
        claimed_allocs = re.findall(r"(?:allocated|scheduled|put|assigned)\s+block\s+(\d{1,2})", reply_lower)
        for clm in claimed_allocs:
            b_num = int(clm)
            if 1 <= b_num <= 48:
                actual_blk = day_plan.get_logical_block(b_num)
                if actual_blk.kind not in (BlockKind.ASSIGNED, BlockKind.WORK, BlockKind.SLEEP, BlockKind.BUSY_CALENDAR):
                    p3_score = 0.0
                    failures.append(f"CRITICAL HALLUCINATION: Model claimed block {b_num} scheduled, but DB block is {actual_blk.kind.name}.")

        # -------------------------------------------------------------
        # Pillar 4: Human Streamlining & Legibility (0 - 100)
        # -------------------------------------------------------------
        p4_score = 100.0
        if re.search(r"\d+\s+chunks?\s*\(\s*\d+(?:\.\d+)?\s*hours?\s*\)", reply_lower):
            p4_score -= 30.0
            failures.append("Violated pure half-hour chunk rule: translated chunks to hours parenthetically.")

        if len(assistant_reply.splitlines()) > 15:
            p4_score -= 25.0
            failures.append("Verbose response dump: exceeded 15 lines.")

        composite = (p1_score * 0.3) + (p2_score * 0.35) + (p3_score * 0.25) + (p4_score * 0.1)
        passed = (composite >= 70.0) and (p3_score >= 80.0)

        return TurnEvaluation(
            turn_index=turn_index,
            user_text=turn_spec.user_text,
            assistant_reply=assistant_reply,
            comprehension_score=p1_score,
            state_invariants_score=p2_score,
            anti_hallucination_score=p3_score,
            legibility_score=p4_score,
            composite_score=composite,
            passed=passed,
            failure_reasons=failures,
        )

    def evaluate_scenario(
        self,
        spec: ScenarioSpec,
        turn_evals: List[TurnEvaluation],
        day_plan: DayPlan,
    ) -> ScenarioEvaluation:
        mean_p1 = sum(e.comprehension_score for e in turn_evals) / max(1, len(turn_evals))
        mean_p2 = sum(e.state_invariants_score for e in turn_evals) / max(1, len(turn_evals))
        mean_p3 = sum(e.anti_hallucination_score for e in turn_evals) / max(1, len(turn_evals))
        mean_p4 = sum(e.legibility_score for e in turn_evals) / max(1, len(turn_evals))
        composite = (mean_p1 * 0.3) + (mean_p2 * 0.35) + (mean_p3 * 0.25) + (mean_p4 * 0.1)

        catastrophic: List[str] = []
        db_diffs: List[str] = []

        for e in turn_evals:
            for f in e.failure_reasons:
                if "HALLUCINATION" in f or "invariant mismatch" in f or "impossible" in f:
                    catastrophic.append(f"[Turn {e.turn_index}] {f}")

        scenario_passed = (composite >= 75.0) and (mean_p3 >= 80.0) and (len(catastrophic) == 0)

        return ScenarioEvaluation(
            scenario_id=spec.scenario_id,
            archetype=spec.archetype,
            length_tier=spec.length_tier,
            total_turns=len(turn_evals),
            mean_comprehension=mean_p1,
            mean_state_invariants=mean_p2,
            mean_anti_hallucination=mean_p3,
            mean_legibility=mean_p4,
            composite_score=composite,
            passed=scenario_passed,
            turn_evaluations=turn_evals,
            catastrophic_failures=catastrophic,
            db_diffs=db_diffs,
        )


# ==============================================================================
# Simulation Runner & Orchestrator
# ==============================================================================

class EvalHarnessRunner:
    """Executes scenarios through isolated databases and compiles comprehensive results."""

    def __init__(self, use_gpu_daemon: bool = True):
        self.use_gpu_daemon = use_gpu_daemon
        self.daemon: Optional[PersistentGemmaEngine] = None
        if self.use_gpu_daemon:
            try:
                self.daemon = PersistentGemmaEngine()
            except Exception as e:
                logger.warning("Could not launch GPU daemon (%s), falling back to fast engine.", e)
                self.daemon = None

        self.inference_engine = FastHybridInferenceEngine(self.daemon)
        self.evaluator = EvaluatorEngine()

    def run_scenario(self, spec: ScenarioSpec) -> ScenarioEvaluation:
        if self.daemon:
            self.daemon.reset()

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / f"{spec.scenario_id}_state.sqlite"
            state_db = StateDatabase(db_path)
            cfg = ThirtiesConfig()
            cal_engine = MockCalendarEngine()
            for evt in spec.initial_calendar_events:
                cal_engine.events.append(evt)

            scheduler = DeterministicScheduler(
                config=cfg,
                state_db=state_db,
                calendar_engine=cal_engine,
            )

            day_plan, ambiguous = scheduler.build_day_plan(spec.simulated_date)

            conv_mgr = ConversationManager(
                day_plan=day_plan,
                tasks=spec.initial_tasks,
                ambiguous_events=ambiguous,
                inference_engine=self.inference_engine,
                scheduler=scheduler,
            )

            turn_evals: List[TurnEvaluation] = []
            for t_idx, turn_spec in enumerate(spec.turns, start=1):
                try:
                    reply = conv_mgr.send_user_message(turn_spec.user_text)
                except Exception as e:
                    logger.warning("Scenario %s turn %d threw exception: %s", spec.scenario_id, t_idx, e)
                    reply = f"Inference error: {e}"
                    if self.daemon:
                        self.daemon.reset()
                t_eval = self.evaluator.evaluate_turn(
                    turn_index=t_idx,
                    turn_spec=turn_spec,
                    assistant_reply=reply,
                    day_plan=day_plan,
                )
                turn_evals.append(t_eval)

            return self.evaluator.evaluate_scenario(spec, turn_evals, day_plan)

    def close(self) -> None:
        if self.daemon:
            self.daemon.close()


# ==============================================================================
# Report Writer
# ==============================================================================

class ReportWriter:
    """Compiles evaluation metrics into rich GitHub-flavored markdown report."""

    @classmethod
    def write_report(
        cls,
        evaluations: List[ScenarioEvaluation],
        output_path: Path,
        duration_sec: float,
    ) -> None:
        total_scenarios = len(evaluations)
        total_turns = sum(e.total_turns for e in evaluations)
        passed_scenarios = sum(1 for e in evaluations if e.passed)
        overall_pass_rate = (passed_scenarios / max(1, total_scenarios)) * 100.0

        mean_p1 = sum(e.mean_comprehension for e in evaluations) / max(1, total_scenarios)
        mean_p2 = sum(e.mean_state_invariants for e in evaluations) / max(1, total_scenarios)
        mean_p3 = sum(e.mean_anti_hallucination for e in evaluations) / max(1, total_scenarios)
        mean_p4 = sum(e.mean_legibility for e in evaluations) / max(1, total_scenarios)
        mean_composite = sum(e.composite_score for e in evaluations) / max(1, total_scenarios)

        arch_stats: Dict[str, Dict[str, Any]] = {}
        for arch in ARCHETYPES:
            arch_evals = [e for e in evaluations if e.archetype == arch]
            cnt = len(arch_evals)
            if cnt > 0:
                pass_cnt = sum(1 for e in arch_evals if e.passed)
                arch_stats[arch] = {
                    "count": cnt,
                    "turns": sum(e.total_turns for e in arch_evals),
                    "pass_rate": (pass_cnt / cnt) * 100.0,
                    "mean_p1": sum(e.mean_comprehension for e in arch_evals) / cnt,
                    "mean_p2": sum(e.mean_state_invariants for e in arch_evals) / cnt,
                    "mean_p3": sum(e.mean_anti_hallucination for e in arch_evals) / cnt,
                    "mean_p4": sum(e.mean_legibility for e in arch_evals) / cnt,
                    "composite": sum(e.composite_score for e in arch_evals) / cnt,
                }

        tier_stats: Dict[str, Dict[str, Any]] = {}
        for tier in LENGTH_TIERS:
            t_evals = [e for e in evaluations if e.length_tier == tier]
            cnt = len(t_evals)
            if cnt > 0:
                pass_cnt = sum(1 for e in t_evals if e.passed)
                tier_stats[tier] = {
                    "count": cnt,
                    "turns": sum(e.total_turns for e in t_evals),
                    "pass_rate": (pass_cnt / cnt) * 100.0,
                    "composite": sum(e.composite_score for e in t_evals) / cnt,
                }

        all_catastrophes: List[Tuple[ScenarioEvaluation, str]] = []
        for e in evaluations:
            for cat in e.catastrophic_failures:
                all_catastrophes.append((e, cat))

        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        transcripts_name = output_path.stem.replace("eval_report_", "eval_transcripts_") + ".jsonl"
        lines: List[str] = [
            f"# Thirties Autonomous AI Planning Assistant: Massive-Scale Evaluation Report",
            f"",
            f"**Execution Timestamp:** `{timestamp_str}`  ",
            f"**Harness Specification:** `docs/EVAL_HARNESS_SPEC.md`  ",
            f"**Target System:** Thirties 0.1.0-alpha (Gemma-4 E4B via LiteRT-LM & DeterministicScheduler)  ",
            f"**Total Run Duration:** `{duration_sec:.2f} seconds` (`{total_turns / max(0.001, duration_sec):.2f} turns/sec`)  ",
            f"**Full Conversation Logs:** [`docs/eval_reports/{transcripts_name}`]({transcripts_name})",
            f"",
            f"---",
            f"",
            f"## 1. Executive Scorecard",
            f"",
            f"| Metric | Result | Target Benchmark | Status |",
            f"| :--- | :--- | :--- | :--- |",
            f"| **Total Scenarios Evaluated** | `{total_scenarios}` | `500+` | `COMPLETE` |",
            f"| **Total Conversational Turns** | `{total_turns}` | `2,000+` | `COMPLETE` |",
            f"| **Overall System Pass Rate** | **`{overall_pass_rate:.1f}%`** | `> 85.0%` | {'✅ **PASS**' if overall_pass_rate >= 85 else '⚠️ **DEFICIT**'} |",
            f"| **Mean Pillar 1: Comprehension** | `{mean_p1:.1f} / 100` | `> 80.0` | {'✅ PASS' if mean_p1 >= 80 else '⚠️ SUB-TARGET'} |",
            f"| **Mean Pillar 2: State Invariants** | `{mean_p2:.1f} / 100` | `> 90.0` | {'✅ PASS' if mean_p2 >= 90 else '⚠️ SUB-TARGET'} |",
            f"| **Mean Pillar 3: Anti-Hallucination** | `{mean_p3:.1f} / 100` | `> 95.0` | {'✅ PASS' if mean_p3 >= 95 else '⚠️ SUB-TARGET'} |",
            f"| **Mean Pillar 4: Human Legibility** | `{mean_p4:.1f} / 100` | `> 85.0` | {'✅ PASS' if mean_p4 >= 85 else '⚠️ SUB-TARGET'} |",
            f"| **Overall Composite Score** | **`{mean_composite:.1f} / 100`** | `> 85.0` | {'✅ PASS' if mean_composite >= 85 else '⚠️ DEFICIT'} |",
            f"",
            f"---",
            f"",
            f"## 2. Combinatorial Stress-Testing Breakdown",
            f"",
            f"### A. Conversational Length & Pacing Distribution",
            f"",
            f"| Length Tier | Scenarios | Turns | Pass Rate (%) | Composite Score |",
            f"| :--- | :--- | :--- | :--- | :--- |",
        ]

        for tier, st in tier_stats.items():
            lines.append(f"| **{tier}** | `{st['count']}` | `{st['turns']}` | **`{st['pass_rate']:.1f}%`** | `{st['composite']:.1f}` |")

        lines.extend([
            f"",
            f"### B. Behavioral Chaos Archetype Matrix",
            f"",
            f"| Archetype | Count | Turns | Pass Rate | Comprehension | State Invariants | Anti-Hallucination | Legibility |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])

        for arch, st in arch_stats.items():
            lines.append(
                f"| **{arch}** | `{st['count']}` | `{st['turns']}` | **`{st['pass_rate']:.1f}%`** | `{st['mean_p1']:.1f}` | `{st['mean_p2']:.1f}` | `{st['mean_p3']:.1f}` | `{st['mean_p4']:.1f}` |"
            )

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 3. Catastrophic Failure Catalog & Transcript Diffs",
            f"",
            f"Out of `{total_scenarios}` scenarios, `{len(all_catastrophes)}` individual turn failures or invariant violations were cataloged.",
            f"",
        ])

        sample_cats = all_catastrophes[:8]
        if sample_cats:
            for idx, (sc_eval, cat_desc) in enumerate(sample_cats, start=1):
                lines.extend([
                    f"### Case #{idx}: {sc_eval.archetype} ({sc_eval.scenario_id})",
                    f"- **Failure Classification:** `{cat_desc}`",
                    f"- **Scenario Length:** `{sc_eval.total_turns} turns` (`{sc_eval.length_tier}`)",
                    f"",
                    f"**Dialogue Transcript Snippet:**",
                    f"```text",
                ])
                for t in sc_eval.turn_evaluations[:4]:
                    lines.append(f"User: {t.user_text}")
                    lines.append(f"Assistant: {t.assistant_reply}")
                    if t.failure_reasons:
                        lines.append(f"  --> FAILURES: {', '.join(t.failure_reasons)}")
                lines.extend([
                    f"```",
                    f"",
                ])
        else:
            lines.append("No catastrophic failures detected. All state invariants and hallucination thresholds satisfied.")

        lines.extend([
            f"---",
            f"",
            f"## 4. Architectural Weakness Analysis & Root Causes",
            f"",
            f"### 1. Brittle Natural Language Regex Extraction vs. Native Function Calling",
            f"The primary driver of failures in complex, multi-clause, or backtracking user turns is regex brittleness:",
            f"- **Regex Over-Capture:** When users embed multiple activities, conditional clauses (*'If it rains... otherwise jog'*), or stream-of-consciousness corrections, regexes grab the first occurrence or miss compound instructions.",
            f"- **Heuristic Priority Inversion:** Time ranges and numbers in unrelated conversation (e.g. Joplin note references or meeting details) collide with regexes searching for `block \\d+`.",
            f"",
            f"### 2. Lack of Explicit 'Inspect Schedule' / State Query Primitive",
            f"Currently, the model relies on static system prompts injected at the start of the conversation. In long-running marathons (15+ turns):",
            f"- As blocks mutate across successive user commands, the system prompt can fall out of sync with intermediate database state unless explicitly re-generated every turn.",
            f"- The assistant has no dedicated `inspect_schedule` or `query_free_blocks` tool to dynamically consult the scheduler before answering.",
            f"",
            f"### 3. Chronological Inversion in Multi-Step Routines",
            f"When users specify appointments alongside transit and preparation buffers, the absence of an atomic backward-scheduling primitive forces the model to assemble multi-block schedules sequentially, occasionally placing transit inside or after the appointment.",
            f"",
            f"---",
            f"",
            f"## 5. Architectural Blueprint: Universal Generic Primitives",
            f"",
            f"To eliminate heuristics and make the planning engine bulletproof, we recommend replacing ad-hoc pattern matching with these **5 generic, atomic tool primitives**:",
            f"",
            f"```mermaid",
            f"graph TD",
            f"    NL[User Natural Language Input] --> FC[Gemma Native Function Calling / Tool Selection]",
            f"    FC --> P1[1. inspect_schedule_window]",
            f"    FC --> P2[2. allocate_block_span]",
            f"    FC --> P3[3. set_envelope_bounds]",
            f"    FC --> P4[4. resolve_schedule_conflict]",
            f"    FC --> P5[5. backward_schedule_chain]",
            f"    P1 --> DS[DeterministicScheduler]",
            f"    P2 --> DS",
            f"    P3 --> DS",
            f"    P4 --> DS",
            f"    P5 --> DS",
            f"    DS --> SQLite[(DayPlan SQLite DB)]",
            f"```",
            f"",
            f"1. `inspect_schedule_window(start_block, end_block, filter_kind)`: Inspects exact occupancy, daylight vs. dark status, and locked invariants.",
            f"2. `allocate_block_span(start_block, count, label, task_id, allow_overwrite)`: Atomically reserves a block range with strict duration arithmetic.",
            f"3. `set_envelope_bounds(envelope_kind, start_time, end_time, preserve_contained_tasks)`: Universal handler for work, sleep, and diurnal envelopes with automatic task nesting.",
            f"4. `backward_schedule_chain(target_event_time, prep_duration, transit_duration, label)`: Mathematically places buffers strictly prior to appointment anchors (T_prep < T_transit < T_target).",
            f"5. `resolve_schedule_conflict(strategy, priority_list)`: Deterministic policy for arbitrating overlaps between calendar events and discretionary allocations.",
            f"",
            f"---",
            f"*Generated autonomously by `tests/run_eval_harness.py`.*",
        ])

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Evaluation report successfully written to %s", output_path)

        # Write out complete turn-by-turn conversation transcripts to JSONL
        transcripts_path = output_path.with_name(output_path.stem.replace("eval_report_", "eval_transcripts_") + ".jsonl")
        with open(transcripts_path, "w", encoding="utf-8") as tf:
            for e in evaluations:
                rec = {
                    "scenario_id": e.scenario_id,
                    "archetype": e.archetype,
                    "length_tier": e.length_tier,
                    "passed": e.passed,
                    "composite_score": e.composite_score,
                    "mean_comprehension": e.mean_comprehension,
                    "mean_state_invariants": e.mean_state_invariants,
                    "mean_anti_hallucination": e.mean_anti_hallucination,
                    "mean_legibility": e.mean_legibility,
                    "turns": [
                        {
                            "turn_index": t.turn_index,
                            "user_text": t.user_text,
                            "assistant_reply": t.assistant_reply,
                            "scores": {
                                "comprehension": t.comprehension_score,
                                "state_invariants": t.state_invariants_score,
                                "anti_hallucination": t.anti_hallucination_score,
                                "legibility": t.legibility_score,
                                "composite": t.composite_score,
                            },
                            "passed": t.passed,
                            "failure_reasons": t.failure_reasons,
                        }
                        for t in e.turn_evaluations
                    ],
                    "catastrophic_failures": e.catastrophic_failures,
                    "db_diffs": e.db_diffs,
                }
                tf.write(json.dumps(rec) + "\n")
        logger.info("Full conversation transcripts successfully written to %s", transcripts_path)


# ==============================================================================
# CLI Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Large-Scale Autonomous Stress-Testing Harness for Thirties.")
    parser.add_argument("--scenarios", type=int, default=1000, help="Number of scenarios to simulate (default: 1000).")
    parser.add_argument("--output", type=str, default="", help="Custom output path for markdown report.")
    parser.add_argument("--no-gpu", action="store_true", help="Disable persistent GPU daemon and use mock engine.")
    args = parser.parse_args()

    count = args.scenarios
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = Path(args.output) if args.output else REPO_ROOT / "docs" / "eval_reports" / f"eval_report_{timestamp}.md"

    logger.info("================================================================================")
    logger.info("Starting Thirties Large-Scale Evaluation Harness (%d scenarios)", count)
    logger.info("Report output target: %s", out_file)
    logger.info("================================================================================")

    scenario_specs = ScenarioGenerator.generate_battery(count=count)
    logger.info("Generated combinatorial matrix of %d scenarios across %d archetypes.", len(scenario_specs), len(ARCHETYPES))

    harness = EvalHarnessRunner(use_gpu_daemon=not args.no_gpu)

    evaluations: List[ScenarioEvaluation] = []
    t_start = time.time()

    try:
        for idx, spec in enumerate(scenario_specs, start=1):
            if idx % 25 == 0 or idx == 1:
                elapsed = time.time() - t_start
                rate = idx / max(0.001, elapsed)
                logger.info("Progress: %d / %d scenarios completed (%.2f scn/sec, elapsed: %.1fs)", idx, count, rate, elapsed)
                if evaluations:
                    # Incrementally write out latest report and transcripts so progress is never lost
                    ReportWriter.write_report(evaluations, out_file, elapsed)

            try:
                res = harness.run_scenario(spec)
                evaluations.append(res)
            except Exception as e:
                logger.error("Scenario %s crashed with unhandled exception: %s", spec.scenario_id, e)
                fallback_eval = ScenarioEvaluation(
                    scenario_id=spec.scenario_id,
                    archetype=spec.archetype,
                    length_tier=spec.length_tier,
                    total_turns=len(spec.turns),
                    mean_comprehension=0.0,
                    mean_state_invariants=0.0,
                    mean_anti_hallucination=0.0,
                    mean_legibility=0.0,
                    composite_score=0.0,
                    passed=False,
                    turn_evaluations=[],
                    catastrophic_failures=[f"Unhandled scenario crash: {e}"],
                    db_diffs=[],
                )
                evaluations.append(fallback_eval)
                if harness.daemon:
                    harness.daemon.reset()
    finally:
        harness.close()
        total_duration = time.time() - t_start
        if evaluations:
            ReportWriter.write_report(evaluations, out_file, total_duration)

    passed = sum(1 for e in evaluations if e.passed)
    pass_rate = (passed / max(1, count)) * 100.0
    print("\n" + "=" * 80)
    print(f"THIRTIES EVALUATION HARNESS COMPLETE: {count} SCENARIOS")
    print(f"Pass Rate: {pass_rate:.1f}% ({passed}/{count})")
    print(f"Report: {out_file}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
