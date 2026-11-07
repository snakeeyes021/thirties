"""The on-device model spike: the same tasks on each small model, side by side (ROADMAP "After 0.1", item 3).

Candidates are the catalog's (thirties_core/brains/slm/catalog.py): Gemma 4 E4B (the shipped one) and E2B through
LiteRT-LM; Phi-4 Mini and Qwen3 (4B, and 8B where memory allows) through llama.cpp. Nothing here downloads a model:
put its file in the models folder by hand first (``--dry-run`` says what's missing and where).

Each model runs in its own process, one after another, so GPU memory is freed between them and a crash costs one
row. Per model it measures:

- **Parse benchmark** (tests/slm_parse_eval.py, the brain's own steps): accuracy from the model's parses and after
  Python's reading, the parts it would ask about, latency (median, p90), model calls per message;
- **load time** and **peak memory** of the process (resident; GPU memory isn't visible from here: read it from
  ``nvidia-smi`` / ``radeontop`` while it runs);
- the **licence** as listed in the catalog (verify before choosing).

Not run here yet, with how to run them by hand on a chosen model:

- **List sorting against Gemini's answers**: ``score_sorting`` compares a model's roles and urgencies with labels
  made by the Frontier sorter; there's no labelled set yet (making one costs agy calls: the lead's call).
- **The day sim**: ``THIRTIES_SLM_MODEL=<name> ~/.local/share/thirties/venv/bin/python -m tests.sim_eval ...`` runs
  the SLM brain on that model, since the brain loads its runtime through the catalog.

    ~/.local/share/thirties/venv/bin/python -m tests.slm_model_compare --dry-run
    ~/.local/share/thirties/venv/bin/python -m tests.slm_model_compare --models gemma-4-e4b qwen3-4b --limit 100
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thirties_core.brains.slm import catalog  # noqa: E402


def peak_memory_mb() -> Optional[float]:
    try:
        import resource
    except ImportError:
        return None
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # kilobytes on Linux
    return round(kb / 1024, 1)


def measure_model(name: str, runtime=None, cases: Optional[List[Dict[str, Any]]] = None, pipeline: str = "steps",
                  log: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    """One model's row: load it (unless a runtime is given), run the parse benchmark, and describe it."""
    from tests.slm_parse_eval import run_benchmark, select_cases
    spec = catalog.MODELS[name]
    row: Dict[str, Any] = {"model": name, "label": spec.label, "backend": spec.backend, "params": spec.params,
                           "licence": spec.licence}
    start = time.time()
    try:
        runtime = runtime or catalog.load_runtime(model=name)
    except catalog.SlmUnavailable as e:
        return {**row, "error": str(e)}
    row["load_s"] = round(getattr(runtime, "load_seconds", 0.0) or (time.time() - start), 1)
    row["device"] = getattr(runtime, "device", "")
    result = run_benchmark(runtime, cases if cases is not None else select_cases(), pipeline, quiet=True, log=log)
    row["parse"] = {k: v for k, v in result.items() if k != "failures"}
    row["failures"] = result["failures"]
    row["peak_mb"] = peak_memory_mb()
    return row


def score_sorting(sorter, labelled: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Agreement with Gemini's list sorting. ``labelled``: [{"note": {title, body}, "role": ..., "items": [TaskItem],
    "urgencies": {task_id: urgency}}], made with FrontierListSorter. Roles and urgencies are scored separately."""
    from datetime import date
    roles = urgencies = role_total = urgency_total = 0
    for case in labelled:
        role_total += 1
        roles += sorter.role(case["note"]) == case["role"]
        got = sorter.urgencies(case["note"], case["role"], case["items"], date.today())
        for task_id, want in case["urgencies"].items():
            urgency_total += 1
            urgencies += (got.get(task_id) or (None,))[0] == want
    return {"roles": [roles, role_total], "urgencies": [urgencies, urgency_total]}


def _pct(good: int, total: int) -> str:
    return f"{good / total:.0%}" if total else "–"


def table(rows: List[Dict[str, Any]]) -> str:
    """The comparison, as a markdown table, then each model's weakest categories."""
    lines = ["| Model | Backend | Size | Licence | Model alone | After Python | Asked | Median | p90 | Calls/msg | "
             "Load | Peak RAM |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    notes = []
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['label']} | {r['backend']} | {r['params']} | {r['licence']} | not run: {r['error']} "
                         "| | | | | | | |")
            continue
        p = r["parse"]
        lines.append(f"| {r['label']} | {r['backend']} ({r.get('device', '')}) | {r['params']} | {r['licence']} | "
                     f"{_pct(p['model'], p['cases'])} | {_pct(p['final'], p['cases'])} | {p['asked']} | "
                     f"{p['median_s']} s | {p['p90_s']} s | {p['calls_per_message']} | {r['load_s']} s | "
                     f"{r['peak_mb'] or '?'} MB |")
        worst = sorted(p["by_category"].items(), key=lambda kv: kv[1][0] / kv[1][1])[:5]
        notes.append(f"- {r['label']}, weakest after Python: " +
                     ", ".join(f"{cat} {_pct(good, total)}" for cat, (good, total, _) in worst))
    return "\n".join(lines + [""] + notes)


def _run_one_in_subprocess(name: str, args: argparse.Namespace) -> Dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = Path(f.name)
    cmd = [sys.executable, "-m", "tests.slm_model_compare", "--one", name, "--json", str(out),
           "--pipeline", args.pipeline, "--limit", str(args.limit)]
    if args.category:
        cmd += ["--category", *args.category]
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    try:
        return json.loads(out.read_text()) if proc.returncode == 0 else {
            **_describe(name), "error": f"the run exited with {proc.returncode}"}
    except ValueError:
        return {**_describe(name), "error": "no result written"}
    finally:
        out.unlink(missing_ok=True)


def _describe(name: str) -> Dict[str, Any]:
    spec = catalog.MODELS[name]
    return {"model": name, "label": spec.label, "backend": spec.backend, "params": spec.params, "licence": spec.licence}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", default=None, help=f"default: all ({', '.join(catalog.MODELS)})")
    ap.add_argument("--pipeline", choices=("steps", "whole"), default="steps")
    ap.add_argument("--category", nargs="*", help="only these parse categories")
    ap.add_argument("--limit", type=int, default=0, help="only the first N parse cases (the same N for every model)")
    ap.add_argument("--dry-run", action="store_true", help="say which models can run here; load nothing")
    ap.add_argument("--one", help=argparse.SUPPRESS)   # internal: measure one model in this process
    ap.add_argument("--json", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.one:
        from tests.slm_parse_eval import select_cases
        row = measure_model(args.one, cases=select_cases(args.category, args.limit), pipeline=args.pipeline,
                            log=lambda s: print(f"  [{args.one}] {s}", flush=True))
        Path(args.json).write_text(json.dumps(row, indent=1, default=str))
        return
    names = args.models or list(catalog.MODELS)
    unknown = [n for n in names if n not in catalog.MODELS]
    if unknown:
        sys.exit(f"Unknown models: {', '.join(unknown)}. Known: {', '.join(catalog.MODELS)}.")
    if args.dry_run:
        for n in names:
            reason = catalog.model_unavailable_reason(n)
            print(f"{n:<14} {'ready' if reason is None else 'not ready: ' + reason}")
        from thirties_core.brains.slm.runtime import model_homes
        print(f"\nModels folder: {model_homes()[0]}")
        return
    rows = []
    for n in names:
        print(f"== {n}", flush=True)
        rows.append(_run_one_in_subprocess(n, args))
    report = table(rows)
    print("\n" + report)
    out_dir = REPO_ROOT / "docs" / "eval_reports" / "model_spike"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"compare_{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    (out_dir / f"compare_{stamp}.md").write_text(report + "\n")
    print(f"\nReport: {(out_dir / f'compare_{stamp}.md').relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
