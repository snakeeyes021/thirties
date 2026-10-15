"""Inference engine protocol and backend implementations for Thirties.

Supports:
- Antigravity CLI bridge (Gemini 3.8 Flash via agy host tool)
- Direct Google Gemini REST API (Gemini 3.8 Flash via API key)
- Google LiteRT-LM (on-device local Gemma 4B on GPU/CPU)
- MockInferenceEngine (deterministic unit testing)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from thirties_core.config import InferenceConfig, ThirtiesConfig

logger = logging.getLogger(__name__)


def split_thinking_level(model: str) -> Tuple[str, Optional[str]]:
    """Split agy-style 'gemini-3.8-flash-low' into ('gemini-3.8-flash', 'low')."""
    for level in ("low", "medium", "high"):
        if model.endswith(f"-{level}"):
            return model[: -len(level) - 1], level
    return model, None


def message_text(msg: Dict[str, Any]) -> str:
    """Render one transcript message as plain text for text-only transports.

    Tool results are included so the model can see what its tool calls did.
    """
    role = msg.get("role", "")
    content = msg.get("content", "") or ""
    if role == "tool":
        return f"[Result of {msg.get('name', 'tool')}]\n{content}"
    return content


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


def find_agy_binary(custom_path: Optional[str] = None) -> Optional[str]:
    """Locate the agy CLI binary on host or in environment."""
    if custom_path:
        expanded = Path(os.path.expanduser(custom_path))
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return str(expanded)
        if shutil.which(custom_path):
            return custom_path

    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy

    host_home = get_host_home()
    candidates = [
        Path.home() / ".local" / "bin" / "agy",
        host_home / ".local" / "bin" / "agy",
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
    ]
    for h in (Path.home(), host_home):
        if str(h).startswith("/home/"):
            candidates.append(Path("/var") / h.relative_to("/") / ".local" / "bin" / "agy")
        elif str(h).startswith("/var/home/"):
            candidates.append(Path("/home") / h.relative_to("/var/home") / ".local" / "bin" / "agy")

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)

    # If inside Flatpak, check on host via flatpak-spawn
    if Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID")):
        for host_candidate in [
            str(host_home / ".local" / "bin" / "agy"),
            "/usr/local/bin/agy",
            "/usr/bin/agy",
        ]:
            try:
                res = subprocess.run(
                    ["flatpak-spawn", "--host", "test", "-x", host_candidate],
                    capture_output=True,
                    timeout=2,
                )
                if res.returncode == 0:
                    return host_candidate
            except Exception:
                pass

    return None


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


class AntigravityInferenceEngine:
    """Dispatches conversation turns to Gemini (e.g. Gemini 3.8 Flash) via the Antigravity CLI."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.inf_cfg: InferenceConfig = self.config.inference
        self.model = self.inf_cfg.gemini_model or "gemini-3.8-flash-medium"
        self.custom_agy_path = self.inf_cfg.agy_path
        self.timeout = self.inf_cfg.timeout_seconds

    def is_available(self) -> bool:
        """Return True if agy binary is discoverable and executable."""
        return find_agy_binary(self.custom_agy_path) is not None

    def _format_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Format message history into a clear prompt for agy non-interactive print mode."""
        system_prompt = None
        turn_lines = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                system_prompt = content
            elif role == "user":
                turn_lines.append(f"User: {content}")
            elif role == "assistant":
                turn_lines.append(f"Assistant: {content}")
            elif role == "tool":
                turn_lines.append(message_text(msg))

        dialogue = "\n\n".join(turn_lines)
        if system_prompt:
            return f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nCONVERSATION HISTORY:\n{dialogue}\n\nAssistant:"
        return f"{dialogue}\n\nAssistant:"

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute chat turn against Antigravity CLI with Gemini model."""
        agy_bin = find_agy_binary(self.custom_agy_path)
        if not agy_bin:
            raise RuntimeError("Antigravity CLI (agy) binary not found.")

        prompt_text = self._format_prompt(messages)
        is_flatpak = Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))

        cmd = [
            agy_bin,
            "--print", prompt_text,
            "--output-format", "json",
            "--model", self.model,
            "--disable-slash-commands",
        ]
        if is_flatpak:
            cmd = ["flatpak-spawn", "--host"] + cmd

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("Antigravity CLI timed out after %s seconds.", self.timeout)
            return {
                "role": "assistant",
                "content": "The Antigravity assistant timed out waiting for a response.",
                "tool_calls": [],
            }
        except Exception as e:
            logger.error("Failed executing Antigravity CLI: %s", e)
            return {
                "role": "assistant",
                "content": f"Failed invoking Antigravity CLI: {e}",
                "tool_calls": [],
            }

        raw_output = res.stdout
        raw_reply = ""
        if res.returncode == 0 and raw_output.strip():
            try:
                data = json.loads(raw_output)
                raw_reply = data.get("response", "")
            except Exception:
                raw_reply = raw_output
        else:
            err_msg = res.stderr or raw_output or "Unknown CLI failure"
            logger.error("Antigravity CLI returned exit code %s: %s", res.returncode, err_msg)
            return {
                "role": "assistant",
                "content": f"Error contacting model via Antigravity CLI: {err_msg}",
                "tool_calls": [],
            }

        clean_text, tool_calls = parse_model_directives(raw_reply)
        return {
            "role": "assistant",
            "content": clean_text,
            "tool_calls": tool_calls,
        }


class GeminiInferenceEngine:
    """Dispatches conversation turns directly to the Google Gemini REST API."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.inf_cfg: InferenceConfig = self.config.inference
        # Config names follow agy's "<model>-<thinking level>" convention; the REST API
        # takes the level as a separate parameter.
        self.model, self.thinking_level = split_thinking_level(self.inf_cfg.gemini_model or "gemini-3.8-flash-medium")
        self.api_key = self.inf_cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        self.temperature = self.inf_cfg.temperature
        self.timeout = self.inf_cfg.timeout_seconds
        self.max_retries = 4

    def is_available(self) -> bool:
        """Return True if an API key is configured."""
        return bool(self.api_key and self.api_key.strip())

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute chat turn directly against Google Gemini REST API."""
        if not self.is_available():
            raise RuntimeError("Gemini API key is not configured.")

        system_instruction = None
        contents: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                system_instruction = {"parts": [{"text": msg.get("content", "")}]}
                continue
            text = message_text(msg)
            if not text.strip():
                continue  # the API rejects empty parts
            gemini_role = "model" if role == "assistant" else "user"
            if contents and contents[-1]["role"] == gemini_role:
                contents[-1]["parts"].append({"text": text})
            else:
                contents.append({"role": gemini_role, "parts": [{"text": text}]})

        generation_config: Dict[str, Any] = {"temperature": self.temperature}
        if self.thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Thirties/0.1",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )

        try:
            resp_json = self._post_with_retries(req)

            raw_text = ""
            candidates = resp_json.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                raw_text = "".join(p.get("text", "") for p in parts)

            clean_text, tool_calls = parse_model_directives(raw_text)
            return {
                "role": "assistant",
                "content": clean_text,
                "tool_calls": tool_calls,
            }
        except Exception as e:
            logger.error("Gemini API request failed: %s", e)
            return {
                "role": "assistant",
                "content": f"Encountered an error communicating with Gemini API: {e}",
                "tool_calls": [],
            }


    def _post_with_retries(self, req: urllib.request.Request) -> Dict[str, Any]:
        """POST to the API, retrying rate limits and transient server errors with backoff."""
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == self.max_retries:
                    detail = e.read().decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from e
                delay = min(60.0, 5.0 * (2 ** attempt))
                logger.warning("Gemini API returned %s; retrying in %.0fs (attempt %d/%d).",
                               e.code, delay, attempt + 1, self.max_retries)
                time.sleep(delay)
        raise RuntimeError("unreachable")


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
                logger.warning("In-process LiteRT inference failed: %s; falling back to host runner.", e)
                has_litert = False

        if not has_litert and host_runner:
            try:
                is_flatpak = Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))
                cmd = [
                    host_runner["python"],
                    host_runner["script"],
                    "--prompt",
                    last_user_msg,
                ]
                if is_flatpak:
                    cmd = ["flatpak-spawn", "--host"] + cmd

                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if res.returncode == 0:
                    raw_stdout = res.stdout
                    marker_start = "---THIRTIES_RESPONSE_START---"
                    marker_end = "---THIRTIES_RESPONSE_END---"
                    if marker_start in raw_stdout and marker_end in raw_stdout:
                        s_idx = raw_stdout.find(marker_start) + len(marker_start)
                        e_idx = raw_stdout.find(marker_end)
                        raw_reply = raw_stdout[s_idx:e_idx].strip()
                    else:
                        raw_reply = raw_stdout.strip()
                else:
                    logger.error("Host runner exited with code %s: %s", res.returncode, res.stderr)
            except Exception as runner_err:
                logger.error("Host runner execution failed: %s", runner_err)

        if not raw_reply:
            raw_reply = "I'm ready to help you plan your day. Let me know what you'd like to work on!"

        clean_text, tool_calls = parse_model_directives(raw_reply)
        return {
            "role": "assistant",
            "content": clean_text,
            "tool_calls": tool_calls,
        }


def _parse_modify_attributes(attr_str: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    rest = attr_str.strip()

    km = re.search(r"\bkind=([a-zA-Z_]+)", rest, re.IGNORECASE)
    if km:
        args["kind"] = km.group(1).upper()
        rest = rest[:km.start()] + " " + rest[km.end():]

    lm = re.search(r"\b(?:is_)?locked=(true|false)\b", rest, re.IGNORECASE)
    if lm:
        args["is_locked"] = lm.group(1).lower() == "true"
        rest = rest[:lm.start()] + " " + rest[lm.end():]

    tm = re.search(r"\btask_id=([^\s|]+)", rest, re.IGNORECASE)
    if tm:
        args["task_id"] = tm.group(1).strip()
        rest = rest[:tm.start()] + " " + rest[tm.end():]

    stm = re.search(r"\bstart_time=([^\s|]+)", rest, re.IGNORECASE)
    if stm:
        args["start_time"] = stm.group(1).strip()
        rest = rest[:stm.start()] + " " + rest[stm.end():]

    etm = re.search(r"\bend_time=([^\s|]+)", rest, re.IGNORECASE)
    if etm:
        args["end_time"] = etm.group(1).strip()
        rest = rest[:etm.start()] + " " + rest[etm.end():]

    label_m = re.search(r"\blabel=\s*([^|]+)", rest, re.IGNORECASE)
    if label_m:
        lbl = label_m.group(1).strip()
    else:
        lbl = re.sub(r"^[|\s]+|[|\s]+$", "", rest).strip()

    if lbl:
        args["label"] = lbl
    return args


def _parse_range_or_time(target_str: str) -> dict[str, Any]:
    target = target_str.strip()
    if not target:
        return {}
    if re.search(r":|\d\s*(?:am|pm)\b", target, re.IGNORECASE):
        parts = re.split(r"\s*(?:-|–|—|\bto\b|\bthrough\b)\s*", target, flags=re.IGNORECASE)
        if len(parts) >= 2:
            return {"start_time": parts[0].strip(), "end_time": parts[1].strip()}
        else:
            return {"start_time": parts[0].strip(), "end_time": parts[0].strip()}
    m = re.match(r"^(\d{1,2})(?:\s*(?:-|–|—|\bto\b|\bthrough\b)\s*(\d{1,2}))?$", target, re.IGNORECASE)
    if m:
        s_idx = int(m.group(1))
        e_idx = int(m.group(2)) if m.group(2) else s_idx
        return {"start_block": s_idx, "end_block": e_idx}
    return {}


def parse_model_directives(raw_reply: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract explicit tool directives emitted by model text and return cleaned reply and tool calls."""
    tool_calls: list[dict[str, Any]] = []
    clean_lines: list[str] = []

    for line in str(raw_reply).splitlines():
        trimmed = line.strip()
        if not trimmed:
            clean_lines.append(line)
            continue

        # 1. MODIFY_BLOCKS
        mod_m = re.match(r"^MODIFY_BLOCKS?:\s*([^|\n]+)?(?:\s*\|\s*(.*))?$", trimmed, re.IGNORECASE)
        if mod_m:
            target_str = (mod_m.group(1) or "").strip()
            rest = mod_m.group(2) or ""
            parsed_range = _parse_range_or_time(target_str)
            attrs = _parse_modify_attributes(rest)
            args = {
                "start_block": parsed_range.get("start_block"),
                "end_block": parsed_range.get("end_block"),
                "start_time": parsed_range.get("start_time") or attrs.get("start_time"),
                "end_time": parsed_range.get("end_time") or attrs.get("end_time"),
                "kind": attrs.get("kind"),
                "label": attrs.get("label"),
                "task_id": attrs.get("task_id"),
                "is_locked": attrs.get("is_locked"),
            }
            args = {k: v for k, v in args.items() if v is not None}
            call_id = f"modify_call_{parsed_range.get('start_block') or parsed_range.get('start_time') or len(tool_calls)}_{len(tool_calls)}"
            tool_calls.append({
                "id": call_id,
                "name": "modify_blocks",
                "arguments": args,
            })
            continue

        # 2. CLEAR_BLOCKS
        clr_kind_m = re.match(r"^CLEAR_BLOCKS?:\s*(WORK|SLEEP|ALL|TASKS|EVENTS)\b", trimmed, re.IGNORECASE)
        if clr_kind_m:
            target_k = clr_kind_m.group(1).upper()
            tool_calls.append({
                "id": f"clear_blocks_call_{len(tool_calls)}",
                "name": "clear_blocks",
                "arguments": {"clear_kind": target_k, "clear_all_work": (target_k == "WORK")},
            })
            continue

        clr_target_m = re.match(r"^CLEAR_BLOCKS?:\s*([^|\n]+)", trimmed, re.IGNORECASE)
        if clr_target_m:
            target_str = clr_target_m.group(1).strip()
            parsed_range = _parse_range_or_time(target_str)
            if parsed_range:
                args = {
                    "start_block": parsed_range.get("start_block"),
                    "end_block": parsed_range.get("end_block"),
                    "start_time": parsed_range.get("start_time"),
                    "end_time": parsed_range.get("end_time"),
                    "clear_all_work": False,
                }
                args = {k: v for k, v in args.items() if v is not None}
                tool_calls.append({
                    "id": f"clear_blocks_call_{len(tool_calls)}",
                    "name": "clear_blocks",
                    "arguments": args,
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
        insp_m = re.match(r"^INSPECT_BLOCKS?:\s*([^|\n]+)", trimmed, re.IGNORECASE)
        if insp_m:
            target_str = insp_m.group(1).strip()
            parsed_range = _parse_range_or_time(target_str)
            args = {
                "start_block": parsed_range.get("start_block"),
                "end_block": parsed_range.get("end_block"),
                "start_time": parsed_range.get("start_time"),
                "end_time": parsed_range.get("end_time"),
            }
            args = {k: v for k, v in args.items() if v is not None}
            tool_calls.append({
                "id": f"inspect_blocks_call_{len(tool_calls)}",
                "name": "inspect_blocks",
                "arguments": args,
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

    def is_available(self) -> bool:
        return True

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


def get_inference_engine(config: Optional[ThirtiesConfig] = None) -> InferenceEngine:
    """Factory creating the appropriate inference engine backend based on configuration."""
    cfg = config or ThirtiesConfig()
    backend = (cfg.inference.backend or "gemini").lower()

    if backend in ("gemini", "antigravity"):
        agy = AntigravityInferenceEngine(cfg)
        if agy.is_available():
            logger.info("Using Antigravity CLI inference backend (model: %s).", agy.model)
            return agy

        gemini_api = GeminiInferenceEngine(cfg)
        if gemini_api.is_available():
            logger.info("Using direct Gemini API inference backend (model: %s).", gemini_api.model)
            return gemini_api

        logger.warning(
            "Configured backend is '%s', but neither agy CLI nor GEMINI_API_KEY is available. "
            "Checking LiteRT fallback.",
            backend,
        )
        litert = LiteRTInferenceEngine(cfg)
        if litert.is_available():
            return litert
        return MockInferenceEngine()

    elif backend == "litert":
        litert = LiteRTInferenceEngine(cfg)
        if litert.is_available():
            return litert
        logger.warning("LiteRT backend requested but unavailable. Falling back to Mock.")
        return MockInferenceEngine()

    elif backend == "mock":
        return MockInferenceEngine()

    else:
        logger.warning("Unknown backend '%s'. Falling back to Mock.", backend)
        return MockInferenceEngine()
