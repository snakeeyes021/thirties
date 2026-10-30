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
import json
import os
import random
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

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
                                  "-rea", "off", "--no-webui"]
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
        (the user, 2026-10-30: slower is fine in exchange for a smarter model).
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
        fitted = lines[-1].split()
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


def litert_runtime(model: Path):
    """GemmaRuntime on any .litertlm file, with its compile cache kept apart from the app's."""
    os.environ["XDG_CACHE_HOME"] = str(SPIKE / "cache")
    from thirties_core.brains.slm.runtime import GemmaRuntime
    return GemmaRuntime(Path(model))


def parse_bench(rt: Any, quiet: bool = True, limit: int = 0, constrained: bool = False) -> Dict[str, Any]:
    """tests/slm_parse_eval.py's benchmark against any runtime: model alone, and after Python's reading."""
    from tests.slm_parse_eval import SCHEDULE, all_cases, check, interpret, make_interpreter
    cases = all_cases()
    random.Random(0).shuffle(cases)  # the same order as slm_parse_eval
    if limit:
        cases = cases[:limit]
    session = rt.parser(SYSTEM_PROMPT)
    brain = make_interpreter()
    by_cat: Dict[str, List[bool]] = defaultdict(list)
    final_cat: Dict[str, List[bool]] = defaultdict(list)
    failures, times, unparsed = [], [], 0
    for c in cases:
        extra = c["context"]
        context = build_context("Thursday 3:00 PM", SCHEDULE, extra.get("assistant", ""), extra.get("pending"),
                                [], c["message"], last_item=extra.get("last_item", ""))
        start = time.time()
        if constrained:
            raw = rt.json_call(SYSTEM_PROMPT, context, ACTION_SCHEMA)
            text = json.dumps(raw)
        else:
            text = session.ask(context)
            raw = extract_json(text)
        times.append(time.time() - start)
        unparsed += raw is None or "actions" not in raw
        raw = raw or {}
        ok, why = check(parse_actions(raw), c["types"], c["fields"], c["context"])
        final_ok, final_why = check(interpret(brain, raw, c["message"], c["context"]), c["types"], c["fields"],
                                     c["context"])
        by_cat[c["category"]].append(ok)
        final_cat[c["category"]].append(final_ok)
        if not ok or not final_ok:
            failures.append({**c, "why": why, "final_why": final_why, "final_ok": final_ok, "got": text[:600]})
        if not quiet or not final_ok:
            print(f"{'PASS' if final_ok else 'FAIL'} {times[-1]:.1f}s [{c['category']}] {c['message'][:100]}"
                  + ("" if final_ok else f"  -> {final_why}"), flush=True)
    session.close()
    n = len(cases)
    model_alone = sum(sum(v) for v in by_cat.values())
    after = sum(sum(v) for v in final_cat.values())
    return {
        "cases": n, "model_alone": model_alone, "after_python": after,
        "model_alone_pct": round(100 * model_alone / n, 1), "after_python_pct": round(100 * after / n, 1),
        "unparsed": unparsed, "median_seconds": round(statistics.median(times), 2),
        "p90_seconds": round(sorted(times)[int(0.9 * (n - 1))], 2),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(final_cat.items())},
        "failures": failures,
    }


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


def table(paths: List[Path]) -> str:
    rows = ["| model | runtime | VRAM budget | load s | GPU MiB | on GPU | prompt tok/s (2k / 4k) | output tok/s | "
            "s per message (parse + voice) | parse s (median) | schema ok | benchmark: model / after Python |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for path in sorted(paths):
        r = json.loads(path.read_text())
        s, p, b, srv = r.get("speed") or {}, r.get("probe") or {}, r.get("parse") or {}, r.get("server") or {}
        on_gpu = str(srv.get("gpu_layers", "all" if r.get("runtime") == "litert" else ""))
        if srv.get("layers") and on_gpu != "all":
            on_gpu += f"/{srv['layers']}"
        if srv.get("n_cpu_moe"):
            on_gpu += f" (+{srv['n_cpu_moe']} MoE on CPU)"
        prompt = f"{s.get('parse_2k', {}).get('prompt_tps', '')} / {s.get('parse_4k', {}).get('prompt_tps', '')}" if s else ""
        bench = f"{b['model_alone_pct']}% / {b['after_python_pct']}%" if b else ""
        rows.append(" | ".join(["", r.get("model", path.stem), r.get("runtime", ""), str(r.get("vram_gb", "")),
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
        report["first_pass"] = prime(rt, args.probe_messages)
        if not args.parse_only:
            report["speed"] = raw_speed(rt)
            print(json.dumps(report["speed"]), flush=True)
            report["probe"] = probe(rt, messages=PROBE_MESSAGES[:args.probe_messages])
            report["probe"]["describe"] = describe(report["probe"])
            print(json.dumps(report["probe"]), flush=True)
        report["memory"] = server.memory()
        if args.parse or args.parse_only:
            report["parse"] = parse_bench(rt, quiet=True, limit=args.limit, constrained=args.constrained)
            print({k: v for k, v in report["parse"].items() if k != "failures"}, flush=True)
        report["calls"] = rt.calls[-50:]
    print("Saved", save(report, report_name(model, args)))


def cmd_litert(args) -> None:
    model = Path(os.path.expanduser(args.model))
    before = gpu_memory()
    rt = litert_runtime(model)
    after = gpu_memory()
    report: Dict[str, Any] = {"model": model.name, "runtime": "litert", "backend": rt.backend,
                              "size_gb": round(model.stat().st_size / 1e9, 2), "when": time.strftime("%Y-%m-%d %H:%M"),
                              "load_seconds": round(rt.load_seconds, 2)}
    if before and after:
        report["vram_used_mib"] = after["used"] - before["used"]
    report["first_pass"] = prime(rt, args.probe_messages)
    report["probe"] = probe(rt, messages=PROBE_MESSAGES[:args.probe_messages])
    report["probe"]["describe"] = describe(report["probe"])
    print(json.dumps(report["probe"]), flush=True)
    now = gpu_memory()
    if before and now:
        report["vram_used_mib_after_probe"] = now["used"] - before["used"]
    report["memory"] = proc_memory(os.getpid())
    if args.parse:
        report["parse"] = parse_bench(rt, quiet=True, limit=args.limit, constrained=args.constrained)
        print({k: v for k, v in report["parse"].items() if k != "failures"}, flush=True)
    print("Saved", save(report, report_name(model, args)))


def cmd_probe(args) -> None:
    """The device probe on its own: what the app would run to say how long each message takes here."""
    model = Path(os.path.expanduser(args.model))
    if model.suffix == ".litertlm":
        result = probe(litert_runtime(model))
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
        p.add_argument("--parse", action="store_true", help="also run the 546-case parse benchmark")
        p.add_argument("--parse-only", action="store_true", help="only the parse benchmark (one slot)")
        p.add_argument("--constrained", action="store_true", help="benchmark with JSON-schema decoding on every case")
        p.add_argument("--limit", type=int, default=0, help="only the first N benchmark cases")
        p.add_argument("--ctx", type=int, default=0, help="context per slot (default 4096; 8192 for --parse-only)")
        p.add_argument("--slots", type=int, default=3)
        p.add_argument("--extra", default="", help="extra llama-server arguments")
        p.add_argument("--tag", default="")
    p = sub.add_parser("table")
    p.add_argument("reports", nargs="*")
    args = ap.parse_args()
    if getattr(args, "ctx", None) == 0:
        args.ctx = 8192 if args.parse_only else 4096  # parse-only matches ParserSession's 8192-token window
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
