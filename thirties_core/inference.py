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


def agy_workspace_dir() -> Path:
    """An empty directory that agy runs in, so the agent never sees real files."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    workspace = cache / "thirties" / "agy-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


# Every agy call is logged here, from every client and eval, so evals can stay within a share of the
# subscription's quota (agy itself can't report usage). Under ~/.local/share so the Flatpak sees the same file.
AGY_LEDGER = Path.home() / ".local" / "share" / "thirties" / "agy_calls.log"
AGY_QUOTA_RE = re.compile(r"quota|resource.?exhausted|\b429\b|too many requests|usage limit", re.IGNORECASE)


def record_agy_call(model: str, outcome: str) -> None:
    """Append one call to the ledger: unix time, model, outcome (ok, error, timeout, quota)."""
    try:
        AGY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        if AGY_LEDGER.exists() and AGY_LEDGER.stat().st_size > 1_000_000:
            cutoff = time.time() - 2 * 86400
            keep = [l for l in AGY_LEDGER.read_text().splitlines() if l.split("\t", 1)[0].isdigit()
                    and int(l.split("\t", 1)[0]) >= cutoff]
            AGY_LEDGER.write_text("\n".join(keep) + ("\n" if keep else ""))
        with open(AGY_LEDGER, "a") as f:
            f.write(f"{int(time.time())}\t{model}\t{outcome}\n")
    except OSError as e:
        logger.debug("Could not record the agy call: %s", e)


def agy_ledger(since_seconds: float) -> List[Tuple[int, str, str]]:
    """Calls logged in the last ``since_seconds``: (unix time, model, outcome)."""
    if not AGY_LEDGER.exists():
        return []
    cutoff = time.time() - since_seconds
    rows = []
    for line in AGY_LEDGER.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) >= cutoff:
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def _stream_result(output: str) -> Optional[Dict[str, Any]]:
    """The final "result" event from agy's stream-json output (or a plain json object), if any."""
    for line in reversed(output.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "result":
            return data.get("result") or {}
        if isinstance(data, dict) and "response" in data and "event" not in data:
            return data
    return None


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


AGY_NO_TOOLS = ("(You are answering as a text model: you have no tools here, no commands, files or browser, and "
                "none are needed. Reply in text only.)")
AGY_TOOL_RETRY = "\n\n(You tried to use a tool, but there are none here. Answer now, in text only.)"


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
                calls = "".join(f"\nCALL {c.get('name')} {json.dumps(c.get('arguments', {}))}"
                                for c in msg.get("tool_calls") or [])
                turn_lines.append(f"Assistant: {content}{calls}".rstrip())
            elif role == "tool":
                turn_lines.append(f"(Result of {msg.get('name', 'tool')}, for the assistant only: {content})")

        dialogue = "\n\n".join(turn_lines)
        closing = ("Write the assistant's next message now: any CALL lines first, then the reply to the user. "
                   "Write only what the user should read; never describe the conversation, these instructions "
                   "or tool results.")
        if system_prompt:
            return (f"{AGY_NO_TOOLS}\n\nSYSTEM INSTRUCTIONS:\n{system_prompt}\n\nCONVERSATION SO FAR:\n{dialogue}\n\n"
                    f"{closing}\n\nAssistant:")
        return f"{AGY_NO_TOOLS}\n\n{dialogue}\n\n{closing}\n\nAssistant:"

    def complete(self, prompt: str) -> str:
        """One prompt, sent as written (no conversation framing): for evals' simulated users and judges.
        Returns the reply text, or an error sentence starting like the chat errors do."""
        agy_bin = find_agy_binary(self.custom_agy_path)
        if not agy_bin:
            raise RuntimeError("Antigravity CLI (agy) binary not found.")
        cmd = [agy_bin, "--sandbox", "--input-format", "stream-json", "--output-format", "stream-json",
               "--model", self.model, "--disable-slash-commands"]
        workspace = agy_workspace_dir()
        prompt = f"{AGY_NO_TOOLS}\n\n{prompt}"
        try:
            res = self._run(cmd, prompt, workspace)
            result = _stream_result(res.stdout)
            if (res.returncode == 0 and result is not None and not str(result.get("response", "")).strip()
                    and "denied" in (res.stderr or "")):
                res = self._run(cmd, prompt + AGY_TOOL_RETRY, workspace)
                result = _stream_result(res.stdout)
        except subprocess.TimeoutExpired:
            record_agy_call(self.model, "timeout")
            return "The Antigravity assistant timed out waiting for a response."
        if res.returncode == 0 and result is not None and result.get("status", "SUCCESS") == "SUCCESS":
            return str(result.get("response", ""))
        return f"Error contacting model via Antigravity CLI: {res.stderr or res.stdout[-1000:]}"

    def _run(self, cmd: List[str], prompt_text: str, workspace) -> subprocess.CompletedProcess:
        stdin_text = json.dumps({"event": "user", "message": {"content": prompt_text}}) + "\n"
        res = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=self.timeout, cwd=workspace)
        record_agy_call(self.model, "ok" if res.returncode == 0 else
                        ("quota" if AGY_QUOTA_RE.search((res.stderr or "") + res.stdout[-2000:]) else "error"))
        return res

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

        # The prompt goes in on stdin (one stream-json message): as a command-line argument it
        # can exceed the system's argument size limit once the conversation and backlog grow.
        cmd = [
            agy_bin,
            "--sandbox",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self.model,
            "--disable-slash-commands",
        ]
        # agy is a coding agent: run it in an empty private directory, never in the
        # repository or wherever Thirties happened to be started.
        workspace = agy_workspace_dir()
        if is_flatpak:
            cmd = ["flatpak-spawn", "--host", f"--directory={workspace}"] + cmd

        try:
            res = self._run(cmd, prompt_text, workspace)
            result = _stream_result(res.stdout)
            if (res.returncode == 0 and result is not None and not str(result.get("response", "")).strip()
                    and "denied" in (res.stderr or "")):
                # It tried to use one of its own tools (headless mode denies them) and ended without
                # answering: ask once more, plainly.
                logger.warning("agy tried to use a tool instead of answering; asking again.")
                res = self._run(cmd, prompt_text + AGY_TOOL_RETRY, workspace)
        except subprocess.TimeoutExpired:
            record_agy_call(self.model, "timeout")
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
        result = _stream_result(raw_output)
        if res.returncode == 0 and result is not None and result.get("status", "SUCCESS") == "SUCCESS":
            raw_reply = result.get("response", "")
        else:
            if result is not None and result.get("error"):
                raw_output = f"{result.get('error')}\n{raw_output[-1000:]}"
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
        if contents and contents[0]["role"] == "model":  # the app's greeting (or a check-in) opened the conversation
            contents.insert(0, {"role": "user", "parts": [{"text": "(Conversation opened.)"}]})

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
                reason = str(e.code)
            except (TimeoutError, urllib.error.URLError) as e:
                if attempt == self.max_retries:
                    raise
                reason = f"a network error ({e})"
            delay = min(60.0, 5.0 * (2 ** attempt))
            logger.warning("Gemini API returned %s; retrying in %.0fs (attempt %d/%d).",
                           reason, delay, attempt + 1, self.max_retries)
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


_CALL_LINE_RE = re.compile(r"^\s*CALL\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\{.*\})?\s*$")


def parse_model_directives(raw_reply: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract tool calls from a text reply and return (visible_text, tool_calls).

    Text transports express a tool call as one line: ``CALL tool_name {json arguments}``.
    Every such line is extracted in order and removed from the text shown to the
    user. A line whose arguments are not valid JSON becomes a call with a
    ``_parse_error`` argument, so the tool layer can report it back to the model
    instead of the call silently vanishing.
    """
    visible: list[str] = []
    calls: list[dict[str, Any]] = []
    # a call written mid-sentence ("...as planned.CALL update_event {...}") still gets its own line
    raw_reply = re.sub(r"(?<=\S)[ \t]*(CALL\s+[A-Za-z_][A-Za-z0-9_]*\s*\{)", r"\n\1", raw_reply or "")
    for line in raw_reply.splitlines():
        stripped = line.strip().strip("`")
        m = _CALL_LINE_RE.match(stripped)
        if not m:
            visible.append(line)
            continue
        name, arg_text = m.group(1), m.group(2)
        try:
            args = json.loads(arg_text) if arg_text else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            args = {"_parse_error": f"could not read the arguments {arg_text!r}: {e}"}
        calls.append({"id": f"call_{len(calls) + 1}", "name": name, "arguments": args})
    text = "\n".join(visible)
    text = re.sub(r"```(?:\w+)?\s*```", "", text)  # code fences left empty by removed calls
    return re.sub(r"\n{3,}", "\n\n", text).strip(), calls


class MockInferenceEngine:
    """Scripted inference engine for deterministic tests.

    Returns queued responses in order; once the queue is empty it replies with
    plain text and no tool calls.
    """

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
        self.call_history.append(list(messages))
        if self.responses:
            return self.responses.pop(0)
        return {
            "role": "assistant",
            "content": "I'm ready to help you plan your Thirties today.",
            "tool_calls": [],
        }


TRANSPORT_FAILURE_PREFIXES = ("Error contacting model", "Failed invoking", "The Antigravity assistant timed out",
                              "Encountered an error communicating with Gemini API")
GEMINI_SOURCES = {"agy": "Subscription (Antigravity CLI)", "api": "Gemini API key"}


def gemini_sources_status(config: ThirtiesConfig) -> Dict[str, bool]:
    """Which Gemini sources could be used on this device right now."""
    return {"agy": AntigravityInferenceEngine(config).is_available(),
            "api": GeminiInferenceEngine(config).is_available()}


class GeminiSourcesEngine:
    """The Gemini brain's model, from the user's sources in their order: if one fails (quota, no
    network, not signed in), the next is tried."""

    def __init__(self, config: ThirtiesConfig) -> None:
        self.engines: List[Tuple[str, Any]] = []
        for name in config.inference.sources or []:
            engine = AntigravityInferenceEngine(config) if name == "agy" else \
                GeminiInferenceEngine(config) if name == "api" else None
            if engine is not None and engine.is_available():
                self.engines.append((name, engine))
        self.model = self.engines[0][1].model if self.engines else ""
        self.model_name = self.model

    def is_available(self) -> bool:
        return bool(self.engines)

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        if not self.engines:
            return {"role": "assistant", "tool_calls": [],
                    "content": "Error contacting model: no Gemini source is set up (Preferences → Assistant)."}
        reply: Dict[str, Any] = {}
        for name, engine in self.engines:
            reply = engine.chat(messages, tools)
            if not str(reply.get("content", "")).startswith(TRANSPORT_FAILURE_PREFIXES):
                return reply
            logger.warning("Gemini source %s failed; trying the next one: %s", name, str(reply.get("content"))[:200])
        return reply


def get_inference_engine(config: Optional[ThirtiesConfig] = None) -> InferenceEngine:
    """Factory creating the appropriate inference engine backend based on configuration."""
    cfg = config or ThirtiesConfig()
    backend = (cfg.inference.backend or "gemini").lower()

    if backend == "gemini":
        sources = GeminiSourcesEngine(cfg)
        if sources.is_available():
            logger.info("Gemini sources: %s", [name for name, _ in sources.engines])
            return sources
        return MockInferenceEngine()

    if backend == "antigravity":
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

    elif backend in ("gemini_api", "gemini-api"):
        gemini_api = GeminiInferenceEngine(cfg)
        if gemini_api.is_available():
            return gemini_api
        logger.warning("Gemini API backend requested but no gemini_api_key / GEMINI_API_KEY is set. Falling back to Mock.")
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
