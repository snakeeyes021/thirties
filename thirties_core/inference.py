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

        cleaned_content, tool_calls = parse_model_directives(raw_reply)
        if tool_calls:
            logger.info("[Inference] Extracted tool directives from model: %s", [c["name"] for c in tool_calls])

        return {
            "role": "assistant",
            "content": cleaned_content,
            "tool_calls": tool_calls,
        }


def _parse_modify_attributes(rest: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if not rest:
        return args
    rest = rest.strip()

    km = re.search(r"\bkind=([A-Za-z_]+)\b", rest, re.IGNORECASE)
    if km:
        args["kind"] = km.group(1).upper()
        rest = rest[:km.start()] + " " + rest[km.end():]

    lm = re.search(r"\blocked=(true|false)\b", rest, re.IGNORECASE)
    if lm:
        args["is_locked"] = (lm.group(1).lower() == "true")
        rest = rest[:lm.start()] + " " + rest[lm.end():]

    tm = re.search(r"\btask_id=([^\s|]+)", rest, re.IGNORECASE)
    if tm:
        args["task_id"] = tm.group(1).strip()
        rest = rest[:tm.start()] + " " + rest[tm.end():]

    label_m = re.search(r"\blabel=\s*([^|]+)", rest, re.IGNORECASE)
    if label_m:
        lbl = label_m.group(1).strip()
    else:
        lbl = re.sub(r"^[|\s]+|[|\s]+$", "", rest).strip()

    if lbl:
        args["label"] = lbl
    return args


def parse_model_directives(raw_reply: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract explicit tool directives emitted by model text and return cleaned reply and tool calls.
    
    Supports multiple directives per turn in order of occurrence.
    """
    tool_calls: list[dict[str, Any]] = []
    clean_lines: list[str] = []

    for line in str(raw_reply).splitlines():
        trimmed = line.strip()
        if not trimmed:
            clean_lines.append(line)
            continue

        # 1. MODIFY_BLOCKS / ALLOCATE_BLOCKS
        mod_m = re.match(r"^(?:MODIFY|ALLOCATE)_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?(?:\s*\|\s*(.*))?$", trimmed, re.IGNORECASE)
        if mod_m:
            s_idx = int(mod_m.group(1))
            e_idx = int(mod_m.group(2)) if mod_m.group(2) else s_idx
            rest = mod_m.group(3) or ""
            attrs = _parse_modify_attributes(rest)
            tool_calls.append({
                "id": f"modify_call_{s_idx}_{len(tool_calls)}",
                "name": "modify_blocks",
                "arguments": {
                    "start_block": s_idx,
                    "end_block": e_idx,
                    "kind": attrs.get("kind"),
                    "label": attrs.get("label"),
                    "task_id": attrs.get("task_id"),
                    "is_locked": attrs.get("is_locked"),
                },
            })
            continue

        # 2. CLEAR_BLOCKS / DEALLOCATE_BLOCKS
        clr_kind_m = re.match(r"^(?:CLEAR|DEALLOCATE)_BLOCKS?:\s*(WORK|SLEEP|ALL)\b", trimmed, re.IGNORECASE)
        if clr_kind_m:
            target_k = clr_kind_m.group(1).upper()
            tool_calls.append({
                "id": f"clear_blocks_call_{len(tool_calls)}",
                "name": "clear_blocks",
                "arguments": {"clear_kind": target_k, "clear_all_work": (target_k == "WORK")},
            })
            continue

        if re.match(r"^(?:CLEAR|DEALLOCATE)_WORK_BLOCKS\b", trimmed, re.IGNORECASE):
            tool_calls.append({
                "id": f"clear_work_call_{len(tool_calls)}",
                "name": "clear_blocks",
                "arguments": {"clear_kind": "WORK", "clear_all_work": True},
            })
            continue

        clr_range_m = re.match(r"^(?:CLEAR|DEALLOCATE)_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?", trimmed, re.IGNORECASE)
        if clr_range_m:
            s_idx = int(clr_range_m.group(1))
            e_idx = int(clr_range_m.group(2)) if clr_range_m.group(2) else s_idx
            tool_calls.append({
                "id": f"clear_blocks_call_{len(tool_calls)}",
                "name": "clear_blocks",
                "arguments": {"start_block": s_idx, "end_block": e_idx, "clear_all_work": False},
            })
            continue

        # 3. RESOLVE_EVENT
        res_m = re.match(r"^RESOLVE_EVENT:\s*([^|\n]+)\s*\|\s*(attend|decline)\b", trimmed, re.IGNORECASE)
        if res_m:
            ev_id = res_m.group(1).strip()
            action_val = res_m.group(2).strip().lower()
            tool_calls.append({
                "id": f"resolve_call_{ev_id}_{len(tool_calls)}",
                "name": "resolve_event",
                "arguments": {"event_id": ev_id, "action": action_val},
            })
            continue

        # 4. INSPECT_BLOCKS
        insp_m = re.match(r"^INSPECT_BLOCKS?:\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?", trimmed, re.IGNORECASE)
        if insp_m:
            s_idx = int(insp_m.group(1))
            e_idx = int(insp_m.group(2)) if insp_m.group(2) else s_idx
            tool_calls.append({
                "id": f"inspect_blocks_call_{len(tool_calls)}",
                "name": "inspect_blocks",
                "arguments": {"start_block": s_idx, "end_block": e_idx},
            })
            continue

        # 5. FINALIZE_PLAN
        fin_m = re.match(r"^FINALIZE_(?:DAY_)?PLAN(?:\s*\|\s*(.*))?", trimmed, re.IGNORECASE)
        if fin_m:
            notes = fin_m.group(1).strip() if fin_m.group(1) else "Plan agreed in chat."
            tool_calls.append({
                "id": f"finalize_call_{len(tool_calls)}",
                "name": "finalize_day_plan",
                "arguments": {"notes": notes},
            })
            continue

        # 6. Backward compatibility REINSTATE_WORK_BLOCKS
        if re.match(r"^(?:REINSTATE|RESTORE)_WORK_BLOCKS\b", trimmed, re.IGNORECASE):
            tool_calls.append({
                "id": f"modify_work_call_{len(tool_calls)}",
                "name": "modify_blocks",
                "arguments": {"kind": "WORK"},
            })
            continue

        clean_lines.append(line)

    clean_text = "\n".join(clean_lines).strip()
    return clean_text, tool_calls


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
                        "name": "modify_blocks",
                        "arguments": {
                            "start_block": idx,
                            "end_block": idx,
                            "block_index": idx,
                            "label": "Dorico Compose",
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
                        "name": "resolve_event",
                        "arguments": {
                            "event_id": "e_doc",
                            "action": "attend",
                        },
                    }
                ],
            }

        if "work" in last_msg and "7am" in last_msg and "3pm" in last_msg:
            return {
                "role": "assistant",
                "content": "Work is now scheduled from 7:00 AM to 3:00 PM (Blocks 1–16) for 16 chunks.",
                "tool_calls": [
                    {
                        "id": "call_work",
                        "name": "modify_blocks",
                        "arguments": {
                            "start_block": 1,
                            "end_block": 16,
                            "kind": "WORK",
                            "is_locked": True,
                        },
                    }
                ],
            }

        return {
            "role": "assistant",
            "content": "I'm ready to help you plan your Thirties today.",
            "tool_calls": [],
        }
