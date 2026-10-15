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
- **`tests/test_slm_brain.py`: the Python side, with a scripted stand-in for Gemma.** It covers references, loose times, relative placement, sliding, conflict options, swaps, proposals that survive a restart, outcomes, memory and profile. It runs in the normal unit suite.
- **`tests/frontier_eval.py`: deterministic multi-turn day scenarios.** These cover the Frontier Brain only.
- **`tests/interactive_eval.py`: step through turns by hand,** with state deltas and an agent or human as the judge.
- **`tests/legacy_slm/`: the old block-number harnesses.** Source material only; they don't run.

### 4.2 Results

See `docs/ROADMAP.md` (Where We Are) for the latest numbers. Raw reports go to `docs/eval_reports/sim/` (gitignored).

### 4.3 Next experiments

- **A "voice" pass for natural replies:** Gemma rewrites the template reply, and Python verifies that every clock time and title survived, falling back to the template otherwise.
- **Per-stage accuracy on a labelled utterance set:** parse accuracy by action type, with Gemini producing labels at scale.
- **A smaller model (E2B) for speed, and a check on what accuracy it costs.**
- **Feeding statistics into suggestions** (real durations, completion by time of day).

---

## 5. Running It

The SLM brain needs the `litert-lm-api` package, plus a model in `~/.local/share/thirties/models/` (`gemma-4-E4B-it-gpu.litertlm` from `litert-community/gemma-4-E4B-it-litert-lm` on Hugging Face). On the dev machine, both live in `~/.local/share/thirties/venv` (created with `--system-site-packages`, so GTK works from it too). Then:

- In config: `[assistant] brain = "slm"`.
- Run clients and evals with `~/.local/share/thirties/venv/bin/python`.
