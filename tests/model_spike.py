"""Which local models can run the SLM brain on this device: speed, memory and parse accuracy.

The spike for ROADMAP "After 0.1" item 3 (2026-10-30). Any GGUF model runs through
llama.cpp's llama-server (prebuilt Vulkan release, user space, no install) behind
LlamaServerRuntime, the same json_call / text_call / parser interface the brain uses;
the app's own LiteRT-LM runtime runs .litertlm files for the baseline. Per model:

- load time, GPU and CPU memory, and what llama.cpp offloads when the budget is
  16 GB or 8 GB (``--vram 8`` leaves the rest of a bigger GPU unused, via --fit);
- prompt and output tokens per second at the brain's context sizes (~2k and ~4k);
- whether JSON-schema-constrained output works, and what the grammar costs;
- the device probe (thirties_core/brains/slm/speed_probe.py): seconds per message
  in the brain's real call pattern, the number a settings page would show;
- the 546-case parse benchmark (tests/slm_parse_eval.py), scored the same way.

    PY=~/.local/share/thirties/venv/bin/python
    $PY -m tests.model_spike inventory
    $PY -m tests.model_spike run --model ~/.local/share/thirties/spike/models/gemma-4-12b-it-Q4_K_M.gguf --vram 16 --parse
    $PY -m tests.model_spike run --model ...gguf --vram 8            # speed only, as on an 8 GB card
    $PY -m tests.model_spike litert --model ~/.local/share/thirties/models/gemma-4-E4B-it-gpu.litertlm --parse
    $PY -m tests.model_spike probe --model ...gguf                  # "how long would each message take here"
    $PY -m tests.model_spike table                                  # summary of all reports

llama-server is found in $THIRTIES_LLAMA_SERVER or under ~/.local/share/thirties/spike/bin.
Reports go to docs/eval_reports/models/ (gitignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.brains.slm.llama_server import LlamaServerRuntime
from thirties_core.brains.slm.parse import ACTION_SCHEMA, SYSTEM_PROMPT, build_context, extract_json, parse_actions
from thirties_core.brains.slm.speed_probe import PROBE_MESSAGES, PROBE_SCHEDULE, describe, probe

SPIKE = Path.home() / ".local" / "share" / "thirties" / "spike"
REPORTS = REPO_ROOT / "docs" / "eval_reports" / "models"
VRAM_HEADROOM_MIB = 1024  # what --fit leaves free on the GPU (llama.cpp's default), for the display and driver


# --- the machine ---

def gpu_memory() -> Optional[Dict[str, int]]:
    """Used and total MiB on the first NVIDIA GPU (None without nvidia-smi; Vulkan alone doesn't report usage)."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,name", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        used, total, name = [x.strip() for x in out.split(",", 2)]
        return {"used": int(used), "total": int(total), "name": name}
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def proc_memory(pid: int) -> Dict[str, int]:
    """Resident and anonymous (not file-backed) MiB of a process."""
    out = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("VmRSS", "RssAnon", "VmHWM"):
                out[key] = int(value.split()[0]) // 1024
    except OSError:
        pass
    return out


def find_llama_server() -> Optional[str]:
    if os.environ.get("THIRTIES_LLAMA_SERVER"):
        return os.environ["THIRTIES_LLAMA_SERVER"]
    found = sorted(SPIKE.glob("bin/vulkan/llama-*/llama-server"))
    return str(found[-1]) if found else shutil.which("llama-server")


def list_devices(binary: str) -> List[Dict[str, Any]]:
    """llama.cpp's devices: [{'id': 'Vulkan1', 'name': ..., 'total_mib': ..., 'free_mib': ...}]."""
    out = subprocess.run([binary, "--list-devices"], capture_output=True, text=True, timeout=60)
    devices = []
    for m in re.finditer(r"^\s*(\w+\d+): (.+?) \((\d+) MiB, (\d+) MiB free\)", out.stdout + out.stderr, re.M):
        devices.append({"id": m[1], "name": m[2], "total_mib": int(m[3]), "free_mib": int(m[4])})
    return devices


def pick_device(devices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The discrete GPU: integrated and software devices report shared system RAM, which would win on size."""
    integrated = re.compile(r"Intel|llvmpipe|Radeon\(TM\) Graphics|Radeon Graphics|Apple", re.I)
    discrete = [d for d in devices if not integrated.search(d["name"])]
    return max(discrete or devices, key=lambda d: d["total_mib"], default=None)


def inventory() -> Dict[str, Any]:
    binary = find_llama_server()
    info: Dict[str, Any] = {"llama_server": binary, "gpu": gpu_memory()}
    if binary:
        info["devices"] = list_devices(binary)
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
        info["llama_version"] = (version.stdout + version.stderr).strip().splitlines()[0]
    mem = {k: int(v.split()[0]) // 1024 for k, v in
           (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()[:3])}
    info["ram_mib"] = mem
    info["disk_free_gib"] = shutil.disk_usage(SPIKE if SPIKE.exists() else Path.home()).free // 2**30
    info["cpu"] = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                        if line.startswith("model name")), "")
    try:
        import litert_lm  # noqa: F401
        info["litert_lm"] = True
    except ImportError:
        info["litert_lm"] = False
    return info


# --- a llama-server process ---

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    """Starts llama-server on one GPU with a memory budget, and reports what it loaded where."""

    def __init__(self, model: Path, vram_gb: Optional[float] = None, ctx_per_slot: int = 4096, slots: int = 3,
                 device: str = "", extra: Optional[List[str]] = None, binary: Optional[str] = None,
                 ram_gb: Optional[float] = None) -> None:
        self.model = Path(model)
        self.vram_gb = vram_gb
        self.ram_gb = ram_gb
        self.ctx_per_slot = ctx_per_slot
        self.slots = slots
        self.binary = binary or find_llama_server()
        if not self.binary:
            raise SystemExit("llama-server not found: set THIRTIES_LLAMA_SERVER or unpack a release under " + str(SPIKE))
        self.device = device
        self.extra = extra or []
        self.port = free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = SPIKE / "logs" / f"server_{self.model.stem}_{vram_gb}_{ram_gb}.log"
        self.info: Dict[str, Any] = {}

    def command(self) -> List[str]:
        devices = list_devices(self.binary)
        dev = next((d for d in devices if d["id"] == self.device), None) if self.device else pick_device(devices)
        cmd = self.ram_limit() + [self.binary, "-m", str(self.model), "-c", str(self.ctx_per_slot * self.slots),
                                  "-np", str(self.slots), "--host", "127.0.0.1", "--port", str(self.port),
                                  "-rea", "off", "--no-webui", "-cram", "0"]
        # -cram 0: no host-RAM prompt cache. Sessions are pinned to their own slots, so it only cost time:
        # on Gemma 12B it copied 1-5 GB of slot state to RAM after every call, ~3 s each (2026-10-30)
        self.info["device"] = dev
        if dev is None or self.vram_gb == 0:
            return cmd + ["-ngl", "0", "-dev", "none"] + self.extra
        cmd += ["-dev", dev["id"]]
        margin = VRAM_HEADROOM_MIB
        if self.vram_gb:
            # --fit leaves this margin free; a smaller card is simulated by leaving the rest of this one unused
            margin = max(VRAM_HEADROOM_MIB, dev["free_mib"] - (int(self.vram_gb * 1024) - VRAM_HEADROOM_MIB))
        fitted = self.fit(["-c", str(self.ctx_per_slot * self.slots), "-np", str(self.slots), "-dev", dev["id"]]
                          + self.extra, margin)
        if fitted is None:
            return cmd + ["-fit", "on", "-fitt", str(margin)] + self.extra
        return cmd + fitted + ["-fit", "off"] + self.extra

    def ram_limit(self) -> List[str]:
        """Run inside a systemd scope capped at ram_gb, as on a machine with less memory.

        Model weights are mmapped, so once the cap is reached the kernel drops their
        pages and reads them back from disk: the "spill to disk" configuration
        (chosen 2026-10-30: slower is fine in exchange for a smarter model).
        """
        if not self.ram_gb or not shutil.which("systemd-run"):
            return []
        return ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={int(self.ram_gb * 1024)}M",
                "-p", "MemorySwapMax=0", "--"]

    def fit(self, args: List[str], margin: int) -> Optional[List[str]]:
        """What llama.cpp's --fit would choose (layers on the GPU, MoE experts on the CPU), made explicit for the report."""
        tool = Path(self.binary).with_name("llama-fit-params")
        if not tool.exists():
            return None
        out = subprocess.run([str(tool), "-m", str(self.model), "-fitt", str(margin)] + args,
                             capture_output=True, text=True, timeout=300)
        lines = out.stdout.strip().splitlines()
        if out.returncode != 0 or not lines:
            return None
        fitted = shlex.split(lines[-1])  # its -ot value comes quoted; .split() kept the quotes ("unknown buffer type")
        self.info["fitted"] = " ".join(fitted)
        layers = [int(n) for n in re.findall(r"n_layer=\s*(\d+)", out.stderr)]
        if layers:
            self.info["layers"] = max(layers)
        m = re.search(r"projected to use (\d+) MiB", out.stderr)
        if m:
            self.info["projected_full_mib"] = int(m[1])
        # drop the context size (already set); keep -ngl, -ncmoe / -ot and the like
        keep, skip = [], False
        for tok in fitted:
            if skip:
                skip = False
                continue
            if tok in ("-c", "--ctx-size"):
                skip = True
                continue
            keep.append(tok)
        return keep

    def start(self, timeout: float = 900) -> "LlamaServer":
        before = gpu_memory()
        cmd = self.command()
        self.info["command"] = " ".join(cmd)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w")
        start = time.time()
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        rt = LlamaServerRuntime(f"http://127.0.0.1:{self.port}", slots=self.slots, ctx_per_slot=self.ctx_per_slot)
        while not rt.healthy():
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited ({self.proc.returncode}); see {self.log_path}")
            if time.time() - start > timeout:
                self.stop()
                raise RuntimeError(f"llama-server didn't load in {timeout}s; see {self.log_path}")
            time.sleep(0.25)
        self.info["load_seconds"] = round(time.time() - start, 2)
        after = gpu_memory()
        if before and after:
            self.info["vram_used_mib"] = after["used"] - before["used"]
        self.info.update(self.offload_summary())
        return self

    def offload_summary(self) -> Dict[str, Any]:
        """What the server's log says it put on the GPU."""
        text = self.log_path.read_text(errors="replace")
        out: Dict[str, Any] = {}
        fitted = self.info.get("fitted", "")
        m = re.search(r"-ngl (-?\d+)", fitted)
        if m:
            out["gpu_layers"] = "all" if int(m[1]) < 0 or int(m[1]) >= self.info.get("layers", 10**6) else int(m[1])
        m = re.search(r"(?:-ncmoe|--n-cpu-moe) (\d+)", fitted)
        if m:
            out["n_cpu_moe"] = int(m[1])
        buffers = defaultdict(float)
        for name, size in re.findall(r"(\S+) (?:model|KV|compute) buffer size\s*=\s*([\d.]+) MiB", text):
            buffers[name] += float(size)
        if buffers:
            out["buffers_mib"] = {k: round(v) for k, v in buffers.items()}
        return out

    def runtime(self) -> LlamaServerRuntime:
        return LlamaServerRuntime(f"http://127.0.0.1:{self.port}", slots=self.slots, ctx_per_slot=self.ctx_per_slot,
                                  load_seconds=self.info.get("load_seconds", 0.0))

    def memory(self) -> Dict[str, Any]:
        out = proc_memory(self.proc.pid) if self.proc else {}
        now = gpu_memory()
        if now:
            out["gpu_used_total_mib"] = now["used"]
        return out

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()

    def __enter__(self) -> "LlamaServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# --- measurements ---

def raw_speed(rt: LlamaServerRuntime) -> Dict[str, Any]:
    """Prompt and output tokens/s at ~2k and ~4k tokens of context, uncached; and the JSON grammar's cost."""
    context = build_context("Thursday 3:00 PM", PROBE_SCHEDULE, "", None, [], "push composing back half an hour")
    out: Dict[str, Any] = {}

    def run(name: str, system: str, user: str, **kw) -> None:
        nonce = f"[{random.random():.8f}] "  # a fresh prefix, so nothing comes from the cache
        start = time.time()
        text, t = rt.chat([{"role": "system", "content": nonce + system}, {"role": "user", "content": user}],
                          cache_prompt=False, **kw)
        out[name] = {"seconds": round(time.time() - start, 2), "prompt_tokens": t.get("prompt_n"),
                     "prompt_tps": round(t.get("prompt_per_second") or 0, 1), "output_tokens": t.get("predicted_n"),
                     "output_tps": round(t.get("predicted_per_second") or 0, 1)}
        if kw.get("schema"):
            out[name]["valid"] = bool(extract_json(text) is not None and "actions" in (extract_json(text) or {}))

    run("warmup", "Say hi.", "hi", max_tokens=4)
    run("parse_2k", SYSTEM_PROMPT, context, max_tokens=120)
    slow = out["parse_2k"]["output_tps"] < 5  # spilling to RAM or disk: keep the run to minutes, not hours
    run("parse_2k_schema", SYSTEM_PROMPT, context, max_tokens=120, schema=ACTION_SCHEMA)
    long_system = SYSTEM_PROMPT + "\nMore examples:\n" + "\n".join(SYSTEM_PROMPT.split("\n")[16:-1]) * 2
    budget = rt.ctx_per_slot - 300
    if len(long_system) / 3.2 > budget:  # stay inside one slot's context
        long_system = long_system[: int(budget * 3.2)]
    run("parse_4k", long_system, context, max_tokens=120)
    n = 40 if slow else 200
    run("generate_200", "You are a helpful writer.", "Write about 180 words on why short breaks help focus.",
        max_tokens=n, temperature=0.0)
    p, s = out["parse_2k"], out["parse_2k_schema"]
    out["schema_overhead_seconds"] = round(s["seconds"] - p["seconds"], 2)
    return out


def prime(rt: Any, messages: int = 6) -> Dict[str, Any]:
    """One untimed pass of the brain's calls before measuring.

    The first use of a model on a GPU compiles its shaders (Vulkan on NVIDIA: about
    a minute for Gemma E4B, 2026-10-30); the driver caches them, so later starts
    take seconds. The pass's wall time is kept: on a model's first run it is that
    one-off cost, on later runs a few seconds.
    """
    start = time.time()
    probe(rt, messages=PROBE_MESSAGES[:max(1, min(messages, 2))])
    return {"seconds": round(time.time() - start, 2)}


LITERT_CACHE = "shared"  # --litert-cache: shared (the spike's cache), fresh (a new empty one per load), off (none)


def _litert_cache_home() -> str:
    """Where this load's compile cache goes. fresh: a new empty directory per load, the previous one deleted (Qwen3.5-4B's
    WebGPU program cache failed to deserialize, DATA_LOSS, on every load and was rewritten each time, 11-04)."""
    if LITERT_CACHE != "fresh":
        return str(SPIKE / "cache")
    root = SPIKE / "cache" / "fresh"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    return str(root)


def litert_runtime(model: Path, ctx: int = 0, cpu: bool = False):
    """GemmaRuntime on any .litertlm file, with its compile cache kept apart from the app's.

    ``ctx`` is the context the model really holds; ParserSession budgets against it. Past it a model doesn't fail,
    it returns garbage and then nothing (Phi-4-mini's export says ekv4096 but breaks at ~2048 tokens: --ctx 2048).
    The default is the ekv size in the file name, if any, else Gemma's 8192.
    """
    os.environ["XDG_CACHE_HOME"] = _litert_cache_home()
    from thirties_core.brains.slm.runtime import GemmaRuntime
    kv = re.search(r"ekv(\d+)", Path(model).name)
    tokens = ctx or (min(8192, int(kv.group(1))) if kv else 8192)
    cache = LITERT_CACHE != "off"
    if not cpu:
        return GemmaRuntime(Path(model), max_tokens=tokens, cache=cache)
    import litert_lm  # --cpu: LiteRT on the CPU, as on a phone without a usable GPU (GemmaRuntime tries the GPU first)
    gpu = litert_lm.Backend.GPU
    litert_lm.Backend.GPU = litert_lm.Backend.CPU
    try:
        rt = GemmaRuntime(Path(model), max_tokens=tokens, cache=cache)
    finally:
        litert_lm.Backend.GPU = gpu
    rt.backend = "cpu"
    return rt



class FreshEngineRuntime:
    """A LiteRT runtime that reloads its engine before every conversation after the first.

    For exports whose conversations leak into each other (Qwen3.5-4B in LiteRT-LM 0.18 and 0.19: the first
    conversation after a load answers well; later ones drift, then repeat "</think>" to the token limit or come back
    empty; a reload clears it, a unique system prompt doesn't). Only ParserSession's sequential use fits: anything
    that opens a second conversation (json_call, the probe's voice session) would reload under the first.
    """

    def __init__(self, model: Path, ctx: int = 0) -> None:
        self._model, self._ctx = model, ctx
        self.rt = litert_runtime(model, ctx)
        self.reloads, self.reload_seconds, self._opened = 0, 0.0, False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.rt, name)

    def parser(self, system: str) -> Any:
        from thirties_core.brains.slm.runtime import ParserSession
        return ParserSession(self, system)

    def open_conversation(self, system: str) -> Any:
        if self._opened:
            start = time.time()
            calls = self.rt.calls
            self.rt.engine.close()
            self.rt = litert_runtime(self._model, self._ctx)
            self.rt.calls = calls
            self.reloads += 1
            self.reload_seconds += time.time() - start
        self._opened = True
        return self.rt.open_conversation(system)


def _bare_actions(text: str) -> Optional[Dict[str, Any]]:
    """A reply that's the actions without their wrapper: a list of actions, or one action ({"type": ...})."""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(cleaned[start:end + 1])
        except ValueError:
            continue
        if isinstance(value, dict) and "type" in value and "actions" not in value:
            value = [value]
        if isinstance(value, list) and value and all(isinstance(a, dict) and "type" in a for a in value):
            return {"actions": value}
    return None


def shared_repair(text: str) -> Optional[Dict[str, Any]]:
    """The same format help for every model (the fair comparison, 11-02), applied before Python's reading: the
    actions without their {"actions": ...} wrapper are wrapped (Gemma E2B answers with a bare list), a reply one to
    three closing brackets short is closed (LiteRT drops Qwen's last "}"), and a value of the wrong type for a
    true/false or number field is read as meant ("alongside": "workout" is alongside=true)."""
    raw = extract_json(text)
    if raw is None or "actions" not in raw:
        raw = _bare_actions(text) or raw
    if raw is None or "actions" not in raw:
        for tail in ("}", "]}", "}]}"):
            fixed = extract_json((text or "").rstrip() + tail)
            if fixed is not None and "actions" in fixed:
                raw = fixed
                break
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        return raw
    props = ACTION_SCHEMA["properties"]["actions"]["items"]["properties"]
    for action in raw["actions"]:
        if not isinstance(action, dict):
            continue
        for key, value in list(action.items()):
            kind = props.get(key, {}).get("type")
            if kind == "boolean" and not isinstance(value, bool):
                action[key] = str(value).strip().lower() not in ("", "false", "no", "0", "none", "null")
            elif kind == "integer" and isinstance(value, str) and value.strip().isdigit():
                action[key] = int(value.strip())
    return raw


SMOKE_CASES = 5           # --smoke: one case from each of the first five request types, on one load
SMOKE_LOAD_SECONDS = 300  # a smoke run aborts on a load slower than this,
SMOKE_CASE_SECONDS = 60   # on a case slower than this, or when no reply can be read at all
PROJECT_AFTER = 10        # a sized run projects its total after this many cases, and stops if it won't fit the budget
EMPTY_STREAK = 5          # any run stops after this many empty replies in a row (Qwen LiteRT past its real context, 11-04)


class BenchAbort(RuntimeError):
    """A run stopped early on purpose (smoke failure, over budget): its finished cases are kept."""


def case_id(c: Dict[str, Any]) -> str:
    return f"{c['category']}:{hashlib.sha1(c['message'].encode()).hexdigest()[:10]}"


def bench_cases(limit: int = 0, offset: int = 0, sample: int = 0, smoke: bool = False) -> List[Dict[str, Any]]:
    """The benchmark's cases in slm_parse_eval's order. sample: the same number from every request type (96 = 3 of
    each of 32), the first ones in that order, so every model gets the same set; smoke: one each from five types."""
    from tests.slm_parse_eval import all_cases
    cases = all_cases()
    random.Random(0).shuffle(cases)  # the same order as slm_parse_eval
    if smoke or sample:
        kinds = list(dict.fromkeys(c["category"] for c in cases))
        per = 1 if smoke else max(1, sample // len(kinds))
        wanted = set(kinds[:SMOKE_CASES]) if smoke else set(kinds)
        taken: Dict[str, int] = defaultdict(int)
        picked = []
        for c in cases:
            if c["category"] in wanted and taken[c["category"]] < per:
                taken[c["category"]] += 1
                picked.append(c)
        cases = picked
    return cases[offset:offset + limit] if limit else cases[offset:]  # --offset/--limit split a long run into parts


def _load_records(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if path and path.exists():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                out[r["id"]] = r
            except (ValueError, KeyError):
                continue  # a line cut short by a crash
    return out


def summarize(records: List[Dict[str, Any]], offset: int = 0) -> Dict[str, Any]:
    """The report's parse section, from per-case records (finished cases only, in run order)."""
    n = len(records)
    if not n:
        return {"cases": 0, "offset": offset, "failures": []}
    final_cat: Dict[str, List[bool]] = defaultdict(list)
    for r in records:
        final_cat[r["category"]].append(r["final_ok"])
    times = [r["seconds"] for r in records]
    model_alone, after = sum(r["ok"] for r in records), sum(r["final_ok"] for r in records)
    with_retry = sum(r["brain_ok"] for r in records)
    return {
        "cases": n, "model_alone": model_alone, "after_python": after, "with_retry": with_retry,
        "model_alone_pct": round(100 * model_alone / n, 1), "after_python_pct": round(100 * after / n, 1),
        "with_retry_pct": round(100 * with_retry / n, 1),
        "unparsed": sum(r["unparsed"] for r in records), "median_seconds": round(statistics.median(times), 2),
        "p90_seconds": round(sorted(times)[int(0.9 * (n - 1))], 2),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(final_cat.items())},
        "seconds": [round(t, 2) for t in times], "offset": offset,
        "failures": [{k: v for k, v in r.items() if k not in ("ok", "brain_ok", "unparsed", "seconds")}
                     for r in records if not r["ok"] or not r["final_ok"]],
    }


def parse_bench(rt: Any, quiet: bool = True, limit: int = 0, constrained: bool = False,
                retry: bool = True, offset: int = 0, repair: bool = False, sample: int = 0, smoke: bool = False,
                records: Optional[Path] = None, budget_minutes: float = 0) -> Dict[str, Any]:
    """tests/slm_parse_eval.py's benchmark against any runtime: model alone, and after Python's reading.
    ``repair``: shared_repair before Python's reading, the same for every model. ``records``: each case's result is
    appended there (JSONL) as it finishes, and a rerun skips the cases already in it, so nothing finished is lost.
    ``smoke`` raises BenchAbort on a case slower than SMOKE_CASE_SECONDS or when no reply can be read; a
    ``budget_minutes`` run raises it once its projected total won't fit; any run raises it after EMPTY_STREAK empty
    replies in a row. Either way the finished cases are kept."""
    from tests.slm_parse_eval import SCHEDULE, check, interpret, make_interpreter
    cases = bench_cases(limit, offset, sample, smoke)
    done = _load_records(records)
    todo = [c for c in cases if case_id(c) not in done]
    session = rt.parser(SYSTEM_PROMPT)
    brain = make_interpreter()
    started, new, empty = time.time(), 0, 0
    out = open(records, "a") if records else None
    try:
        for c in todo:
            extra = c["context"]
            context = build_context("Thursday 3:00 PM", SCHEDULE, extra.get("assistant", ""), extra.get("pending"),
                                    [], c["message"], last_item=extra.get("last_item", ""))
            start = time.time()
            if constrained:
                raw = rt.json_call(SYSTEM_PROMPT, context, ACTION_SCHEMA)
                text = json.dumps(raw)
            else:
                text = session.ask(context)
                raw = shared_repair(text) if repair else extract_json(text)
            seconds = time.time() - start
            bad = raw is None or "actions" not in raw
            raw = raw or {}
            ok, why = check(parse_actions(raw), c["types"], c["fields"], c["context"])
            final_ok, final_why = check(interpret(brain, raw, c["message"], c["context"]), c["types"], c["fields"],
                                         c["context"])
            if bad and not constrained and retry:
                # what the brain does next (SlmBrain._parse): ask again with JSON-schema decoding
                again = rt.json_call(SYSTEM_PROMPT, context, ACTION_SCHEMA)
                brain_ok, _ = check(interpret(brain, again, c["message"], c["context"]), c["types"], c["fields"],
                                    c["context"])
            else:
                brain_ok = final_ok
            rec = {**c, "id": case_id(c), "ok": ok, "final_ok": final_ok, "brain_ok": brain_ok, "unparsed": bad,
                   "seconds": round(seconds, 2), "why": why, "final_why": final_why, "got": text[:600]}
            done[rec["id"]] = rec
            new += 1
            if out:
                out.write(json.dumps(rec, default=str) + "\n")
                out.flush()
                os.fsync(out.fileno())
            if not quiet or not final_ok:
                print(f"{'PASS' if final_ok else 'FAIL'} {seconds:.1f}s [{c['category']}] {c['message'][:100]}"
                      + ("" if final_ok else f"  -> {final_why}"), flush=True)
            empty = empty + 1 if not (text or "").strip() else 0
            if empty >= EMPTY_STREAK:
                raise BenchAbort(f"{EMPTY_STREAK} empty replies in a row")
            if smoke and seconds > SMOKE_CASE_SECONDS:
                raise BenchAbort(f"smoke: a case took {seconds:.0f} s (limit {SMOKE_CASE_SECONDS} s)")
            if budget_minutes and new == PROJECT_AFTER and len(todo) > new:
                total = (time.time() - started) / new * len(todo) / 60  # wall time, reloads included
                print(f"PROJECTION {total:.0f} min for {len(todo)} cases (budget {budget_minutes:g} min)", flush=True)
                if total > budget_minutes:
                    raise BenchAbort(f"projected {total:.0f} min for {len(todo)} cases, over the "
                                     f"{budget_minutes:g} min budget")
        if smoke:
            ran = [done[case_id(c)] for c in cases if case_id(c) in done]
            if ran and all(r["unparsed"] for r in ran):
                raise BenchAbort("smoke: no reply could be read")
    finally:
        if out:
            out.close()
        session.close()
    return summarize([done[case_id(c)] for c in cases if case_id(c) in done], offset)


def run_bench(rt: Any, args, name: str, retry: bool = True) -> Tuple[Dict[str, Any], str]:
    """parse_bench with the command's options, its per-case records next to the report; (parse section, status)."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    records = REPORTS / f"{name}.cases.jsonl"
    try:
        parse = parse_bench(rt, quiet=True, limit=args.limit, constrained=args.constrained, repair=args.repair,
                            retry=retry, offset=args.offset, sample=args.sample, smoke=args.smoke, records=records,
                            budget_minutes=args.budget_min)
        return parse, "ok"
    except BenchAbort as e:
        print("ABORTED", e, flush=True)
        return summarize(list(_load_records(records).values()), args.offset), f"aborted: {e}"


def finish(report: Dict[str, Any], name: str) -> None:
    print("Saved", save(report, name))
    if report.get("status", "ok") != "ok":
        raise SystemExit(3)  # stopped on purpose; the runner reads the reason from the report


# --- reports ---

def save(report: Dict[str, Any], name: str) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{name}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    return path


def report_name(model: Path, args) -> str:
    tag = f"_{args.tag}" if getattr(args, "tag", "") else ""
    tag = (f"_ram{args.ram:g}" if getattr(args, "ram", None) else "") + tag
    vram = getattr(args, "vram", None)
    return f"{model.stem}_{'litert' if model.suffix == '.litertlm' else ('vram' + format(vram, 'g') if vram is not None else 'all')}{tag}"


LICENCES = [("gemma-4", "Apache-2.0"), ("qwen3", "Apache-2.0"), ("phi-4", "MIT")]  # from each repo's card, 2026-10-30


def licence(model: str) -> str:
    return next((lic for prefix, lic in LICENCES if model.lower().startswith(prefix)), "?")


def table(paths: List[Path]) -> str:
    rows = ["| model | GB | licence | runtime | VRAM budget | RAM cap | load s | GPU MiB | on GPU | prompt tok/s (2k / 4k) | output tok/s | "
            "s per message (parse + voice) | parse s (median) | schema ok | benchmark: model / after Python / with retry |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for path in sorted(paths):
        r = json.loads(path.read_text())
        s, p, b, srv = r.get("speed") or {}, r.get("probe") or {}, r.get("parse") or {}, r.get("server") or {}
        on_gpu = str(srv.get("gpu_layers", "all" if r.get("runtime") == "litert" else ""))
        if srv.get("layers") and on_gpu != "all":
            on_gpu += f"/{srv['layers']}"
        if srv.get("n_cpu_moe"):
            on_gpu += f" (+{srv['n_cpu_moe']} MoE on CPU)"
        prompt = f"{s.get('parse_2k', {}).get('prompt_tps', '')} / {s.get('parse_4k', {}).get('prompt_tps', '')}" if s else ""
        bench = f"{b['model_alone_pct']}% / {b['after_python_pct']}% / {b.get('with_retry_pct', '?')}%" if b else ""
        rows.append(" | ".join(["", r.get("model", path.stem), str(r.get("size_gb", "")), licence(r.get("model", "")),
                                r.get("runtime", ""), str(r.get("vram_gb", "")), str(r.get("ram_gb") or ""),
                                str(srv.get("load_seconds", p.get("load_seconds", ""))),
                                str(srv.get("vram_used_mib", r.get("vram_used_mib", ""))), on_gpu, prompt,
                                str(s.get("generate_200", {}).get("output_tps", "")) if s else "",
                                str(p.get("message_seconds_median", "")), str(p.get("parse_seconds_median", "")),
                                str(p.get("json_call_ok", "")), bench, ""]).strip())
    return "\n".join(rows)


# --- commands ---

def cmd_run(args) -> None:
    model = Path(os.path.expanduser(args.model))
    report: Dict[str, Any] = {"model": model.name, "runtime": "llama.cpp", "vram_gb": args.vram,
                              "size_gb": round(model.stat().st_size / 1e9, 2), "when": time.strftime("%Y-%m-%d %H:%M")}
    extra = args.extra.split() if args.extra else []
    slots = 1 if args.parse_only else args.slots
    report["ram_gb"] = args.ram
    with LlamaServer(model, vram_gb=args.vram, ctx_per_slot=args.ctx, slots=slots, device=args.device,
                     extra=extra, ram_gb=args.ram) as server:
        report["server"] = server.info
        print(json.dumps({k: v for k, v in server.info.items() if k != "command"}, default=str), flush=True)
        rt = server.runtime()
        report["status"] = "ok"
        if args.smoke and (server.info.get("load_seconds") or 0) > SMOKE_LOAD_SECONDS:
            report["status"] = f"aborted: smoke: loading took {server.info['load_seconds']:.0f} s"
            finish(report, report_name(model, args))
        report["first_pass"] = prime(rt, args.probe_messages)
        if not args.parse_only:
            report["speed"] = raw_speed(rt)
            print(json.dumps(report["speed"]), flush=True)
            report["probe"] = probe(rt, messages=PROBE_MESSAGES[:args.probe_messages])
            report["probe"]["describe"] = describe(report["probe"])
            print(json.dumps(report["probe"]), flush=True)
        report["memory"] = server.memory()
        if args.parse or args.parse_only:
            report["parse"], report["status"] = run_bench(rt, args, report_name(model, args))
            print({k: v for k, v in report["parse"].items() if k not in ("failures", "seconds")}, flush=True)
        report["calls"] = rt.calls[-50:]
    finish(report, report_name(model, args))


def cmd_litert(args) -> None:
    model = Path(os.path.expanduser(args.model))
    if args.reload_per_session:
        cmd_litert_fresh(model, args)
        return
    before = gpu_memory()
    rt = litert_runtime(model, args.ctx, args.cpu)
    after = gpu_memory()
    report: Dict[str, Any] = {"model": model.name, "runtime": "litert", "backend": rt.backend,
                              "size_gb": round(model.stat().st_size / 1e9, 2), "when": time.strftime("%Y-%m-%d %H:%M"),
                              "load_seconds": round(rt.load_seconds, 2), "ctx": rt.context_tokens}
    if before and after:
        report["vram_used_mib"] = after["used"] - before["used"]
    report["status"] = "ok"
    if args.smoke and rt.load_seconds > SMOKE_LOAD_SECONDS:
        report["status"] = f"aborted: smoke: loading took {rt.load_seconds:.0f} s"
        finish(report, report_name(model, args))
    if not args.parse_only:
        report["first_pass"] = prime(rt, args.probe_messages)
        report["probe"] = probe(rt, messages=PROBE_MESSAGES[:args.probe_messages])
        report["probe"]["describe"] = describe(report["probe"])
        print(json.dumps(report["probe"]), flush=True)
    now = gpu_memory()
    if before and now:
        report["vram_used_mib_after_probe"] = now["used"] - before["used"]
    report["memory"] = proc_memory(os.getpid())
    if args.parse or args.parse_only:
        report["parse"], report["status"] = run_bench(rt, args, report_name(model, args))
        print({k: v for k, v in report["parse"].items() if k not in ("failures", "seconds")}, flush=True)
    finish(report, report_name(model, args))



def cmd_litert_fresh(model: Path, args) -> None:
    """The parse benchmark alone, on a fresh engine per session (see FreshEngineRuntime); no probe, no retries."""
    rt = FreshEngineRuntime(model, args.ctx)
    report: Dict[str, Any] = {"model": model.name, "runtime": "litert", "backend": rt.backend,
                              "size_gb": round(model.stat().st_size / 1e9, 2), "when": time.strftime("%Y-%m-%d %H:%M"),
                              "load_seconds": round(rt.load_seconds, 2), "ctx": rt.context_tokens,
                              "note": "fresh engine per session: conversations leak into each other in this runtime; "
                                      "no probe (it keeps two conversations open) and no constrained retries"}
    report["status"] = "ok"
    if args.smoke and rt.load_seconds > SMOKE_LOAD_SECONDS:
        report["status"] = f"aborted: smoke: loading took {rt.load_seconds:.0f} s"
        finish(report, report_name(model, args))
    report["parse"], report["status"] = run_bench(rt, args, report_name(model, args), retry=False)
    report["reloads"], report["reload_seconds"] = rt.reloads, round(rt.reload_seconds, 1)
    report["memory"] = proc_memory(os.getpid())
    print({k: v for k, v in report["parse"].items() if k not in ("failures", "seconds")}, flush=True)
    finish(report, report_name(model, args))

def cmd_probe(args) -> None:
    """The device probe on its own: what the app would run to say how long each message takes here."""
    model = Path(os.path.expanduser(args.model))
    if model.suffix == ".litertlm":
        result = probe(litert_runtime(model, args.ctx, args.cpu))
    else:
        with LlamaServer(model, vram_gb=args.vram, device=args.device) as server:
            result = probe(server.runtime())
    print(json.dumps(result, indent=1))
    print(describe(result))


def _terminate(signum, frame) -> None:
    raise SystemExit(128 + signum)  # unwinds the with-blocks, so llama-server is stopped too


def main() -> None:
    signal.signal(signal.SIGTERM, _terminate)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="GPUs, llama.cpp devices, RAM, disk")
    for name in ("run", "litert", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--model", required=True)
        p.add_argument("--vram", type=float, default=None, help="GPU memory budget in GB (8 simulates an 8 GB card; "
                                                                "0 = CPU only); default: all of it")
        p.add_argument("--ram", type=float, default=None, help="cap the server's RAM in GB (weights beyond it are "
                                                               "read back from disk through mmap)")
        p.add_argument("--probe-messages", type=int, default=6, help="messages in the probe (fewer for slow setups)")
        p.add_argument("--device", default="", help="llama.cpp device id (default: the largest discrete GPU)")
        p.add_argument("--parse", action="store_true", help="also run the parse benchmark (561 cases)")
        p.add_argument("--parse-only", action="store_true", help="only the parse benchmark (llama.cpp: one slot; litert: no probe)")
        p.add_argument("--constrained", action="store_true", help="benchmark with JSON-schema decoding on every case")
        p.add_argument("--repair", action="store_true",
                       help="the shared format help before Python's reading (shared_repair), for a fair comparison")
        p.add_argument("--limit", type=int, default=0, help="only the first N benchmark cases")
        p.add_argument("--offset", type=int, default=0, help="skip the first N benchmark cases (with --limit: a part)")
        p.add_argument("--ctx", type=int, default=0, help="context per slot (default 4096; 8192 for --parse-only); for litert, the tokens the model holds")
        p.add_argument("--slots", type=int, default=3)
        p.add_argument("--extra", default="", help="extra llama-server arguments")
        p.add_argument("--tag", default="")
        p.add_argument("--cpu", action="store_true", help="litert: run on the CPU (as a phone without a usable GPU)")
        p.add_argument("--sample", type=int, default=0,
                       help="a stratified sample: the same number of cases from every request type (96 = 3 x 32)")
        p.add_argument("--smoke", action="store_true",
                       help=f"{SMOKE_CASES} cases on one load; stop (exit 3) on a slow load or case, or no readable reply")
        p.add_argument("--budget-min", type=float, default=0,
                       help=f"after {PROJECT_AFTER} cases, stop (exit 3) if the projected total exceeds this many minutes")
        p.add_argument("--litert-cache", choices=["shared", "fresh", "off"], default="shared",
                       help="litert: the compile cache (fresh: a new empty one per engine load; off: none)")
        p.add_argument("--reload-per-session", action="store_true",
                       help="litert: a fresh engine per conversation, for exports whose conversations leak")
    p = sub.add_parser("table")
    p.add_argument("reports", nargs="*")
    args = ap.parse_args()
    if args.cmd == "run" and args.ctx == 0:
        args.ctx = 8192 if args.parse_only else 4096  # parse-only matches ParserSession's 8192-token window
    # (litert keeps 0: litert_runtime's default, the file's ekv size or 8192. Until 11-04 litert got 4096 here too.)
    global LITERT_CACHE
    LITERT_CACHE = getattr(args, "litert_cache", "shared")
    if args.cmd == "inventory":
        print(json.dumps(inventory(), indent=1))
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "litert":
        cmd_litert(args)
    elif args.cmd == "probe":
        cmd_probe(args)
    elif args.cmd == "table":
        print(table([Path(p) for p in args.reports] or list(REPORTS.glob("*.json"))))


if __name__ == "__main__":
    main()
