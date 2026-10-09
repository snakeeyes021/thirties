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
        username = os.environ.get("USER", "matt")
        base_dirs = [
            Path.home() / ".local" / "share" / "thirties" / "models",
            Path(f"/var/home/{username}/.local/share/thirties/models"),
            Path(f"/home/{username}/.local/share/thirties/models"),
            Path("/var/home/matt/.local/share/thirties/models"),
            Path("/home/matt/.local/share/thirties/models"),
        ]
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
        username = os.environ.get("USER", "matt")
        python_candidates = [
            f"/var/home/{username}/dev/Thirties/.venv/bin/python",
            f"/home/{username}/dev/Thirties/.venv/bin/python",
            "/var/home/matt/dev/Thirties/.venv/bin/python",
            "/home/matt/dev/Thirties/.venv/bin/python",
        ]
        script_candidates = [
            f"/var/home/{username}/dev/Thirties/thirties_core/runner.py",
            f"/home/{username}/dev/Thirties/thirties_core/runner.py",
            "/var/home/matt/dev/Thirties/thirties_core/runner.py",
            "/home/matt/dev/Thirties/thirties_core/runner.py",
        ]

        found_python: Optional[str] = None
        for py in python_candidates:
            if Path(py).is_file():
                found_python = py
                break

        # Inside Flatpak, test host existence via flatpak-spawn
        if not found_python and shutil.which("flatpak-spawn"):
            for py in python_candidates:
                try:
                    res = subprocess.run(
                        ["flatpak-spawn", "--host", "test", "-f", py],
                        capture_output=True,
                        timeout=2,
                    )
                    if res.returncode == 0:
                        found_python = py
                        break
                except Exception:
                    pass

        if not found_python:
            return None

        found_script: Optional[str] = None
        for sc in script_candidates:
            if Path(sc).is_file():
                found_script = sc
                break

        if not found_script and shutil.which("flatpak-spawn"):
            for sc in script_candidates:
                try:
                    res = subprocess.run(
                        ["flatpak-spawn", "--host", "test", "-f", sc],
                        capture_output=True,
                        timeout=2,
                    )
                    if res.returncode == 0:
                        found_script = sc
                        break
                except Exception:
                    pass

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
            tool_calls.append({
                "id": "reinstate_work_call",
                "name": "reinstate_work_blocks",
                "arguments": {},
            })
            raw_reply = re.sub(r"(?:REINSTATE|RESTORE)_WORK_BLOCKS[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

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
            raw_reply = re.sub(r"(?:CLEAR|DEALLOCATE)_BLOCKS?:\s*\d{1,2}(?:\s*-\s*\d{1,2})?[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # 3. Directive: ALLOCATE_BLOCK(S)?: <start>[-<end>] | <label>
        alloc_matches = list(re.finditer(r"ALLOCATE_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\|\s*([^\n]+)", raw_reply, re.IGNORECASE))
        for match in alloc_matches:
            s_idx = int(match.group(1))
            e_idx = int(match.group(2)) if match.group(2) else s_idx
            lbl = match.group(3).strip()
            tool_calls.append({
                "id": f"alloc_call_{s_idx}",
                "name": "allocate_thirty_block",
                "arguments": {
                    "block_index": s_idx,
                    "start_block": s_idx,
                    "end_block": e_idx,
                    "custom_label": lbl,
                },
            })
        if alloc_matches:
            raw_reply = re.sub(r"ALLOCATE_BLOCKS?:\s*\d{1,2}(?:\s*-\s*\d{1,2})?\s*\|[^\n]*(\n|$)", "", raw_reply, flags=re.IGNORECASE).strip()

        # Fallback to User Intent Extraction if model did not emit directives:
        if not tool_calls:
            # Intent: Day off / Clear all work blocks
            # Intent: Reinstate work blocks
            if re.search(r"(?:reinstate|restore|put (?:them )?back|add back|turns out I (?:do )?have work|do have work)", last_user_msg, re.IGNORECASE):
                tool_calls.append({
                    "id": "reinstate_work_call",
                    "name": "reinstate_work_blocks",
                    "arguments": {},
                })
            # Intent: Day off / Clear all work blocks
            elif re.search(r"(?:don't(?:\s+\w+)?\s+have\s+work|no\s+work(?:day|\s+today)?|day\s+off|(?:clear|deallocate|open)\s+(?:all\s+)?(?:my\s+)?work(?:\s+blocks)?)", last_user_msg, re.IGNORECASE):
                tool_calls.append({
                    "id": "clear_work_call",
                    "name": "clear_blocks",
                    "arguments": {"clear_all_work": True},
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
            # Intent: Block with duration (e.g. "Let's go block 28, I'll probably want to go for at least two hours")
            # Intent: Block with duration (e.g. "Let's go block 28, I'll probably want to go for at least two hours")
            elif (block_user := re.search(r"\bblock\s+(\d{1,2})\b", last_user_msg, re.IGNORECASE)) and (dur_user := re.search(r"(\d+(?:\.\d+)?|half|one|two|three|four|five|an?)\s*(?:hours?|hrs?)", last_user_msg, re.IGNORECASE)):
                s_idx = int(block_user.group(1))
                dur_str = dur_user.group(1).lower()
                word_map = {'a': 1.0, 'an': 1.0, 'half': 0.5, 'one': 1.0, 'two': 2.0, 'three': 3.0, 'four': 4.0, 'five': 5.0}
                hrs = float(dur_str) if dur_str.replace('.', '', 1).isdigit() else word_map.get(dur_str, 1.0)
                num_blocks = max(1, int(round(hrs * 2)))
                e_idx = min(48, s_idx + num_blocks - 1)
                
                # Activity extraction
                lbl = "Focus Session"
                for word in ["composing", "writing", "coding", "reading", "study", "exercise", "dev"]:
                    if word in last_user_msg.lower():
                        lbl = word.capitalize()
                        break

                tool_calls.append({
                    "id": f"alloc_call_{s_idx}",
                    "name": "allocate_thirty_block",
                    "arguments": {
                        "block_index": s_idx,
                        "start_block": s_idx,
                        "end_block": e_idx,
                        "custom_label": lbl,
                    },
                })
            # Intent: Standard block allocation
            elif alloc_match := re.search(r"(?:allocate|put|assign|schedule|set)\s+(?:block\s+)?(\d{1,2})\s+(?:to|for|with)\s+(.+)", last_user_msg, re.IGNORECASE):
                block_idx = int(alloc_match.group(1))
                label_text = alloc_match.group(2).strip()
                tool_calls.append({
                    "id": "alloc_call",
                    "name": "allocate_thirty_block",
                    "arguments": {"block_index": block_idx, "start_block": block_idx, "end_block": block_idx, "custom_label": label_text},
                })
            elif alloc_match := re.search(r"(?:allocate|put|assign|schedule|set)\s+(.+?)\s+(?:to|at|in|for)\s+(?:block\s+)?(\d{1,2})", last_user_msg, re.IGNORECASE):
                label_text = alloc_match.group(1).strip()
                block_idx = int(alloc_match.group(2))
                tool_calls.append({
                    "id": "alloc_call",
                    "name": "allocate_thirty_block",
                    "arguments": {"block_index": block_idx, "start_block": block_idx, "end_block": block_idx, "custom_label": label_text},
                })

        # Intent: confirm attendance
        if re.search(r"\b(yes|attending|confirm)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "attend_call",
                "name": "resolve_calendar_event",
                "arguments": {"event_id": "unconfirmed", "attending": True},
            })
        elif re.search(r"\b(decline|skip|no|not attending)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "decline_call",
                "name": "resolve_calendar_event",
                "arguments": {"event_id": "unconfirmed", "attending": False},
            })

        # Intent: finalize plan
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
