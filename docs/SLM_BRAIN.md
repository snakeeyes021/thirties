# The SLM Brain: Design, Measurements and Evaluation

> The on-device brain: Gemma 4 E4B through LiteRT-LM, on the user's own laptop. `AGENTS.md` §4 explains how it sits next to the Frontier Brain on the shared core. Code: `thirties_core/brains/slm/`. Sequencing: `docs/ROADMAP.md`.

---

## 1. Premise

Gemma 4 E4B is *extremely* weak at this task. It can't hold a schedule in its head, do time math, rank 250 backlog items, or follow a long rule list. So the model contributes as little as possible, in the smallest pieces, and deterministic Python does everything else. This is a different brain from the Frontier one, not a smaller copy of its prompt.

---

## 2. What Was Measured (dev laptop: AMD Renoir iGPU, 14 GB RAM, 2026-10-14)

| Setup | Time per call |
|---|---|
| Engine load (GPU via WebGPU) | ~9 s, once per process |
| Fresh conversation, 3.2k-char instruction, constrained JSON | ~15 s |
| Fresh conversation, tiny prompt, constrained JSON | ~5–6 s (the constraint itself costs ~5 s) |
| Fresh conversation, free-text JSON, same instruction | ~10–15 s (reading the instruction dominates) |
| **Long-lived conversation reusing the KV cache** | **~12 s first call, then 2–3 s** |
| Short free-text reply | ~0.6 s |

Lessons:
- **Keep the instruction in a long-lived conversation.** Each later message pays only for its own tokens. The session restarts before the 4,096-token window fills, and is warmed in the background when a client starts.
- **Prefer free-text JSON plus validation to constrained decoding.** With good examples, Gemma's free-text JSON parsed 10/10 in testing. Constrained decoding (llguidance) stays as the fallback when the output doesn't parse.
- **One model call per message.** A separate intent router followed by slot filling would have doubled latency, and one combined parse proved accurate enough.
- **Memory pressure matters.** With browsers open (2 GB free, 11 GB swap in use), turns slowed to 30–45 s.

---

## 3. Per-Message Pipeline (as built)

```
user message
  │
  ├─▶ [python] context: time, today's items (compact), last item mentioned, last assistant line,
  │            pending proposal, 5 backlog items related to the message (keyword match)
  ├─▶ [gemma]  parse → {"actions": [...]}: add, move, remove, swap, outcome, confirm, reject,
  │            choose, suggest, question, remember, profile, energy, chat
  ├─▶ [python] sanity: drop adds unrelated to the message, a "move" of something absent becomes an "add",
  │            "removing" a suggestion becomes "no thanks"
  ├─▶ [python] resolve: "it" / "the second upgrades" / titles → items; "7:30" → AM or PM (future, awake,
  │            or near the item being moved); "right after dinner" → after that item; "3 chunks", "an hour"
  ├─▶ [python] act through PlanTools (the same tools as the Frontier Brain), offering concrete options when
  │            something clashes ("1. do it 5:30–6:00, 2. after the dentist, 3. at the same time")
  ├─▶ suggestions: Python finds open time and ranks the backlog (relevance, goals in the profile,
  │            deferrals, never "someday" sections, one per note); Gemma picks the best 3 of ~12
  │            in its own cached session; Python lays them into free time as a proposal
  └─▶ [python] reply from templates; Gemma writes free text only for small talk
```

Dialog state that must survive restarts (the pending proposal, what "it" refers to) is stored on the conversation's assistant messages.

---

## 4. Evaluation

### 4.1 Harnesses

- **`tests/sim_eval.py`: simulated users (the main driver).** Gemini, through `agy`, role-plays the user, with a persona and hidden goals, one message at a time, against either brain. It runs on the real Joplin backlog (~250 items) and a synthetic calendar. A Gemini judge then scores:
  - goal completion
  - accuracy against the real final schedule
  - understanding
  - clarity
  - efficiency
  - tone

  Deterministic checks also run on the final schedule. The scenarios come from real conversations (the user's evening of Oct 10 and Sunday of Oct 11) plus:
  - check-ins
  - "someday" items
  - calendar conflicts and invites
  - planning tomorrow
  - goals
  - a rambling message
  - decision fatigue
  - swaps and inserts

  Run `--brain both` to compare with the Frontier Brain on the same scenarios.
- **`tests/sim_generator.py`: generated scenarios for `sim_eval` (`--generated N --seed S`).** Each scenario combines:
  - a kind of user (6)
  - a talking style (10: terse, rambler, lowercase, counts in chunks, decision-fatigued, ...)
  - a day setup (7: workday morning or evening, day off, weekend, ...)
  - two to four of 21 goal kinds (move, shift, swap, insert and slide, fill a window, skipped item to tomorrow, invite, calendar conflict, ...)

  Times come from the day's real free time, so goals never collide, and the simulated user's situation is generated from the same setup the brain sees. Every goal brings its own deterministic checks, tagged by kind, so results come per goal kind. `--workers N` runs worker processes, each with its own Gemma (2.9 GB of GPU memory each). agy failures are left out of the scores. If agy's quota runs out, the run stops cleanly and reports how many conversations finished. `--only-from` / `--skip-from` a report reruns or finishes a run.
- **`tests/test_slm_brain.py`: the Python side, with a scripted stand-in for Gemma.** It covers references, loose times, relative placement, sliding, conflict options, swaps, proposals that survive a restart, outcomes, memory and profile. It runs in the normal unit suite.
- **`tests/frontier_eval.py`: deterministic multi-turn day scenarios.** These cover the Frontier Brain only.
- **`tests/interactive_eval.py`: step through turns by hand,** with state deltas and an agent or human as the judge.
- **`tests/legacy_slm/`: the old block-number harnesses.** Source material only; they don't run.

### 4.2 Eval budget

Evals use **at most 66% of agy's quota per 5-hour window**, leaving the rest for real use of the app. To raise it, pass `--budget 0.8` (or change the default in `tests/sim_eval.py`).

- **How it's enforced:** every agy call, from the app, the Telegram bridge or an eval, is logged to `~/.local/share/thirties/agy_calls.log` (agy can't report usage itself). Before each conversation, `sim_eval` counts the calls in the last 5 hours and stops cleanly at the budget, reporting how many conversations finished.
- **Capacity:** measured at about 1,650 calls on 2026-10-15, when the sim user and judge ran on medium thinking. They now default to `gemini-3.8-flash-low`, which probably costs less per call. Each time a quota stop shows up in the ledger, the capacity estimate recalibrates from it.
- **What fits in one window** (66% ≈ 1,090 calls; an SLM conversation costs about 6 calls, a Frontier one about 9):

| Run | Conversations per window |
|---|---|
| SLM only | ~180 |
| Frontier only | ~120 |
| Both brains on the same scenarios | ~70 scenarios |

  A 250-scenario run of one brain therefore takes two windows. Use `--skip-from` the first report to finish it in the next window.
- **Comparability:** results from a medium-thinking simulator (the 2026-10-15 baseline) aren't directly comparable with low-thinking runs. Compare runs that used the same `--sim-model`.

### 4.3 Results (2026-10-15)

All 13 simulated-user scenarios, judged by Gemini; the Frontier Brain was run on the same scenarios for comparison.

| Round | SLM goals met | Notes |
|---|---|---|
| 1 | 0.39–0.9 (2 scenarios) | robotic replies, "3 thirties" read as 30 min, stuck on "which one?" |
| 4 | 0.93 (13 scenarios) | after the persistent parser, dialog acts, retries, guards |
| 5 | **0.97** (13 scenarios) | 12/13 checks; judge 4.0–4.6; ~13 s per reply |
| Frontier | 0.98 | 12/13 checks; judge 4.3–4.9; ~9 s per reply |

Parse benchmark (`tests/slm_parse_eval.py`): 40/50 straight from the model, median 3.5 s per message. The misses are mostly yes/no/option replies to a pending question, which Python now handles deterministically.

**Scaled up (2026-10-15, desktop: RTX 3080 Ti Laptop GPU).**
- **Parse benchmark, 546 cases.** 496 of them were written and labelled by Gemini from the parser's own instruction, then spot-checked; disagreements are read by hand, since the label can be the wrong one. The benchmark scores both the model alone and Python's reading of it (`SlmBrain._interpret`).
  - Model alone: 84%. After Python: 88%. Median 0.5 s per message.
  - Weakest after Python: multi-request messages (50%), "no" to an offer (62%, up from 6%), confirm (75%), "before/after X" (75%).
- **Generated scenarios, baseline at commit e122160** (seed 1). agy's quota ran out after 235 of 500 conversations:

| Brain | Conversations | Goals met | All checks passed | Gave up | Judge (accuracy / understanding / clarity / efficiency / tone) | Time per reply |
|---|---|---|---|---|---|---|
| Frontier | 139 | 0.98 | 111/139 | 3 | 4.8 / 4.7 / 4.2 / 4.7 / 4.9 | 9.3 s |
| SLM | 90 | 0.88 | 63/90 | 12 | 4.2 / 3.4 / 3.2 / 3.4 / 4.2 | 2.2 s |

  Weakest SLM goal kinds: free time (0.71), work ran late (0.74), move (0.75), fill a window (0.76), shift (0.78), outcome done (0.78). The 13 hand-written scenarios had hidden this: they score 0.97. Fixes for most of the failures it found landed after this baseline and need a rerun on the same scenarios (`--only-from` the baseline report).

Raw reports go to `docs/eval_reports/sim/` (gitignored).

### 4.4 Next experiments

- **A "voice" pass for natural replies:** Gemma rewrites the template reply, and Python verifies that every clock time and title survived, falling back to the template otherwise.
- **A smaller model (E2B) for speed, and a check on what accuracy it costs.**
- **Feeding statistics into suggestions** (real durations, completion by time of day).

---

## 5. Running It

The SLM brain needs the `litert-lm-api` package, plus a model in `~/.local/share/thirties/models/` (`gemma-4-E4B-it-gpu.litertlm` from `litert-community/gemma-4-E4B-it-litert-lm` on Hugging Face). On the dev machine, both live in `~/.local/share/thirties/venv` (created with `--system-site-packages`, so GTK works from it too). Then:

- **Choosing it:**
  - In the desktop app: main menu → Assistant → Gemma (on this device).
  - In any chat, including Telegram: `/brain gemma` (or `/brain gemini` to go back).
  - The choice is this device's own (`local_settings` in the state database, never synced). Every client on the device follows it, and the conversation carries on.
  - `[assistant] brain = "slm"` in config is the default when nothing has been chosen.
- **The Flatpak** has no LiteRT-LM inside. It runs `thirties_core/brains/slm/gemma_server.py` on the host with the venv's Python, through `flatpak-spawn --host`, and talks to it over a pipe (`HostGemmaRuntime`). The server's log is at `~/.cache/thirties/gemma_server.log`. `THIRTIES_SLM_HOST=1` forces this path outside the Flatpak, for testing.
- Run evals, and clients from source, with `~/.local/share/thirties/venv/bin/python`.
