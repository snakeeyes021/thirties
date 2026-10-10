"""On-device inference engine using Google LiteRT-LM.

Runs quantized Gemma models (.litertlm) directly on-device with GPU acceleration
and automatic CPU fallback. Strictly self-contained without external daemons.
Supports both in-process execution and host GPU execution from Flatpak sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from thirties_core.config import InferenceConfig, ThirtiesConfig

logger = logging.getLogger(__name__)


def get_host_home() -> Path:
    """Discover host home directory, working inside or outside Flatpak sandbox."""
    if shutil.which("flatpak-spawn"):
        try:
            res = subprocess.run(
                ["flatpak-spawn", "--host", "printenv", "HOME"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                return Path(res.stdout.strip())
        except Exception:
            pass
    return Path.home()


class InferenceEngine(Protocol):
    """Protocol for LLM inference backends."""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Perform a conversational chat turn with optional tool schemas.
        
        Returns a dict matching:
        {
            "role": "assistant",
            "content": str,
            "tool_calls": List[{"id": str, "name": str, "arguments": Dict[str, Any]}]
        }
        """
        ...


class LiteRTInferenceEngine:
    """LiteRT-LM on-device inference engine leveraging local GPU acceleration."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.inf_cfg: InferenceConfig = self.config.inference
        self.temperature = self.inf_cfg.temperature
        self._engine_instance: Any = None
        self._conv_session: Any = None
        self._is_initialized = False

    def _find_model_path(self) -> Optional[Path]:
        """Check standard paths for available Gemma .litertlm models."""
        host_home = get_host_home()
        base_dirs = [
            Path.home() / ".local" / "share" / "thirties" / "models",
            host_home / ".local" / "share" / "thirties" / "models",
        ]
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            base_dirs.insert(0, Path(xdg_data) / "thirties" / "models")
        for h in (Path.home(), host_home):
            if str(h).startswith("/home/"):
                base_dirs.append(Path("/var") / h.relative_to("/") / ".local" / "share" / "thirties" / "models")
            elif str(h).startswith("/var/home/"):
                base_dirs.append(Path("/home") / h.relative_to("/var/home") / ".local" / "share" / "thirties" / "models")

        candidates = [Path(os.path.expanduser(self.inf_cfg.litert_model_path))]
        for b in base_dirs:
            candidates.extend([
                b / "gemma-4-E4B-it-gpu.litertlm",
                b / "gemma-4-e4b.litertlm",
                b / "gemma-4-e2b.litertlm",
                b / "gemma-4-E4B-it.litertlm",
            ])
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _find_host_runner(self) -> Optional[Dict[str, str]]:
        """Find host python interpreter and runner script for host GPU execution."""
        host_home = get_host_home()
        repo_root = Path(__file__).resolve().parent.parent
        is_flatpak = Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))

        if is_flatpak:
            # Ensure host-accessible copy of runner.py exists in shared XDG data dir
            xdg_share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "thirties"
            xdg_share.mkdir(parents=True, exist_ok=True)
            shared_runner = xdg_share / "runner.py"
            bundled_runner = Path(__file__).resolve().parent / "runner.py"
            if bundled_runner.is_file():
                try:
                    shutil.copy2(bundled_runner, shared_runner)
                except Exception as e:
                    logger.debug("Failed to copy bundled runner: %s", e)

            python_candidates = [
                str(host_home / "dev" / "Thirties" / ".venv" / "bin" / "python"),
                str(host_home / ".local" / "share" / "thirties" / "venv" / "bin" / "python"),
            ]
            script_candidates = [
                str(host_home / ".local" / "share" / "thirties" / "runner.py"),
                str(host_home / "dev" / "Thirties" / "thirties_core" / "runner.py"),
            ]
            for h in (host_home,):
                if str(h).startswith("/home/"):
                    alt = Path("/var") / h.relative_to("/")
                elif str(h).startswith("/var/home/"):
                    alt = Path("/home") / h.relative_to("/var/home")
                else:
                    alt = None
                if alt:
                    python_candidates.append(str(alt / "dev" / "Thirties" / ".venv" / "bin" / "python"))
                    script_candidates.append(str(alt / ".local" / "share" / "thirties" / "runner.py"))
                    script_candidates.append(str(alt / "dev" / "Thirties" / "thirties_core" / "runner.py"))

            def _test_host(path_str: str) -> bool:
                try:
                    res = subprocess.run(
                        ["flatpak-spawn", "--host", "test", "-f", path_str],
                        capture_output=True,
                        timeout=2,
                    )
                    return res.returncode == 0
                except Exception:
                    return False

            found_python = next((py for py in python_candidates if _test_host(py)), None)
            if not found_python:
                return None
            found_script = next((sc for sc in script_candidates if _test_host(sc)), None)
            if found_python and found_script:
                return {"python": found_python, "script": found_script}
            return None
        else:
            python_candidates = [
                str(repo_root / ".venv" / "bin" / "python"),
                str(host_home / "dev" / "Thirties" / ".venv" / "bin" / "python"),
                str(host_home / ".local" / "share" / "thirties" / "venv" / "bin" / "python"),
            ]
            script_candidates = [
                str(repo_root / "thirties_core" / "runner.py"),
                str(host_home / ".local" / "share" / "thirties" / "runner.py"),
                str(host_home / "dev" / "Thirties" / "thirties_core" / "runner.py"),
            ]
            found_python = next((py for py in python_candidates if Path(py).is_file()), None)
            if not found_python:
                return None
            found_script = next((sc for sc in script_candidates if Path(sc).is_file()), None)
            if found_python and found_script:
                return {"python": found_python, "script": found_script}
            return None

    def is_available(self) -> bool:
        """Return True if in-process LiteRT-LM or host runner is available."""
        try:
            import litert_lm  # noqa: F401
            if self._find_model_path():
                return True
        except ImportError:
            pass

        if self._find_host_runner():
            return True

        return False

    def _ensure_initialized(self) -> None:
        if self._is_initialized and self._engine_instance is not None:
            return

        model_path = self._find_model_path()
        if not model_path:
            raise FileNotFoundError(
                "LiteRT-LM model bundle not found. Download a Gemma model into ~/.local/share/thirties/models/"
            )

        import litert_lm

        logger.info("Initializing LiteRT-LM from %s", model_path)
        try:
            # Try GPU acceleration first (e.g. Vulkan / NVIDIA RTX / AMD / Intel)
            self._engine_instance = litert_lm.Engine(
                str(model_path),
                backend=litert_lm.Backend.GPU(),
            )
            logger.info("LiteRT-LM initialized successfully on GPU.")
        except Exception as gpu_err:
            logger.warning("GPU acceleration unavailable (%s); falling back to CPU.", gpu_err)
            self._engine_instance = litert_lm.Engine(
                str(model_path),
                backend=litert_lm.Backend.CPU(),
            )
            logger.info("LiteRT-LM initialized on CPU.")

        self._conv_session = self._engine_instance.create_conversation()
        self._is_initialized = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute chat turn against local LiteRT-LM model."""
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break

        if not last_user_msg:
            return {"role": "assistant", "content": "How can I help you plan your Thirties today?", "tool_calls": []}

        has_litert = False
        try:
            import litert_lm  # noqa: F401
            has_litert = True
        except ImportError:
            has_litert = False

        raw_reply = ""
        host_runner = self._find_host_runner()

        if has_litert:
            try:
                self._ensure_initialized()
                raw_reply = str(self._conv_session.send_message(last_user_msg)).strip()
            except Exception as e:
                logger.error("In-process inference error: %s", e)
                return {"role": "assistant", "content": f"Inference notice: {e}", "tool_calls": []}
        elif host_runner:
            cmd: List[str] = []
            msg_json = json.dumps(messages)
            if shutil.which("flatpak-spawn") and (os.environ.get("FLATPAK_ID") or not os.path.exists(host_runner["python"])):
                cmd = ["flatpak-spawn", "--host", host_runner["python"], host_runner["script"], "--messages", msg_json]
            else:
                cmd = [host_runner["python"], host_runner["script"], "--messages", msg_json]

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                stdout = proc.stdout
                if "---THIRTIES_RESPONSE_START---" in stdout and "---THIRTIES_RESPONSE_END---" in stdout:
                    start_idx = stdout.find("---THIRTIES_RESPONSE_START---") + len("---THIRTIES_RESPONSE_START---")
                    end_idx = stdout.find("---THIRTIES_RESPONSE_END---")
                    raw_reply = stdout[start_idx:end_idx].strip()
                else:
                    raw_reply = stdout.strip() or proc.stderr.strip()
            except Exception as e:
                logger.error("Host runner execution error: %s", e)
                return {"role": "assistant", "content": f"Inference runner error: {e}", "tool_calls": []}
        else:
            return {"role": "assistant", "content": "LiteRT-LM model runner not available.", "tool_calls": []}

        # Parse potential intent/tool calls from user prompt or model text
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


class MockInferenceEngine:
    """Mock inference engine for deterministic unit testing."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self.responses: List[Dict[str, Any]] = list(responses) if responses else []
        self.call_history: List[List[Dict[str, Any]]] = []

    def set_next_response(self, response: Dict[str, Any]) -> None:
        self.responses.append(response)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        self.call_history.append(messages)

        if self.responses:
            return self.responses.pop(0)

        last_msg = messages[-1]["content"].lower() if messages else ""

        if "dorico" in last_msg:
            match = re.search(r"\b(\d{1,2})\b", last_msg)
            idx = int(match.group(1)) if match else 19
            return {
                "role": "assistant",
                "content": f"I allocated block {idx} to Dorico Compose.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "allocate_thirty_block",
                        "arguments": {
                            "block_index": idx,
                            "custom_label": "Dorico Compose",
                        },
                    }
                ],
            }

        if "attending" in last_msg or "yes" in last_msg:
            return {
                "role": "assistant",
                "content": "Marked doctor appointment as attending.",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "name": "resolve_calendar_event",
                        "arguments": {
                            "event_id": "e_doc",
                            "attending": True,
                        },
                    }
                ],
            }

        return {
            "role": "assistant",
            "content": "I'm ready to help you plan your Thirties today.",
            "tool_calls": [],
        }
