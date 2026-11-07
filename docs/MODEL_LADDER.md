# Local models for Thirties: the lay of the land

Measured 11-02 to 11-04 on the dev laptop (RTX 3080 Ti Laptop, 16 GB; Intel Iris Xe; i7-12700H; 64 GB RAM). Raw rows: `docs/model_ladder/results.csv` (full runs, speed runs) and `docs/model_ladder/results_failfast.csv` (smokes and samples). App side: ROADMAP 0.3 item 23.

## What the test is

Each case is one message a user might send Thirties ("push dinner back 45 minutes", "I took the day off"), with a sample day's schedule as context. The model turns it into Thirties' list of actions (add, move, skip, remember…), and Python reads that list the way the app does. A case passes when the action types and the key details (which item, what time, how long, which day) match what the user meant. The full set is 561 cases across 32 kinds of request. The sample is 96 of them (3 of each kind), the same 96 for every model. "s per message" is the brain's real call pattern for one message (read it, then write the reply), the median of a few messages.

## Is the comparison fair?

**Yes, for accuracy.** Every model got the same prompt, the same cases, the same llama.cpp build (b12017, Vulkan) or LiteRT-LM 0.18, temperature 0, and the same small format repair: a missing closing bracket, actions without their wrapper, or a name in a yes/no field are read as meant. No model is scored on formatting alone. Caveats:
- **The sample is noisy:** a score on 96 cases is good to about ±4–8 points. Use the full score where there is one; on 561 cases differences under about 3 points are noise.
- **Some engines needed workarounds**, listed in the status column. Without them those models don't run at all.
- **Speeds are from this laptop.** The NVIDIA card was clock-capped at 1200 MHz for the speed runs, after it dropped off the bus twice at full clocks. The "8 GB GPU" is this card limited to 8 GB. "CPU, 4 threads" stands in for a phone, and a real phone will be slower. The iGPU is this laptop's Intel Iris Xe, sharing system RAM.
- **GPU memory** is what llama.cpp or LiteRT took on the 16 GB card, including the 8,192-token context.

## The table

| Model | Engine | Status | Accuracy: full (561) | Accuracy: sample (96) | s per message: 16 GB GPU | 8 GB GPU | Intel iGPU | CPU, 4 threads | GPU memory | Peak °C |
|---|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 31B (Q3) | llama.cpp | OK; doesn't fit 16 GB, runs partly from system RAM | 88.2% | 81.2% | 24.62 | – | – | – | 14.3 GB | 60.0 |
| Qwen3.8 27B | llama.cpp | OK | 87.9% | 85.4% | 4.39 | – | – | – | 14.3 GB | 90.0 |
| Gemma 4 26B-A4B | llama.cpp | OK | 86.8% | 83.3% | 2.57 | 19.09 | – | – | 14.5 GB | – |
| Qwen3.6 35B-A3B | llama.cpp | OK; runs partly from system RAM | 86.5% | 86.5% | 3.82 | – | – | – | 14.6 GB | 55.0 |
| Gemma 4 E4B | llama.cpp | OK | 86.1% | 83.3% | 1.32 | 1.38 | 8.23 | 21.94 | 3.6 GB | – |
| Gemma 4 12B QAT | llama.cpp | OK | 85.9% | 80.2% | 3.9 | 13.14 | 22.14 | – | 8.5 GB | – |
| Nemotron 3.5 Bolt E4B | llama.cpp | OK | 85.7% | 81.2% | 1.24 | 1.19 | 9.85 | 19.71 | 4.8 GB | – |
| Gemma 4 12B | llama.cpp | OK | 85.6% | 79.2% | 4.13 | 9.21 | – | – | 8.6 GB | – |
| Gemma 4 E4B QAT | llama.cpp | OK | 85.0% | 80.2% | 1.24 | 1.19 | 8.86 | 23.41 | 4.0 GB | – |
| Gemma 4 26B-A4B QAT | llama.cpp | OK | 84.8% | 80.2% | 3.29 | 12.66 | – | – | 14.8 GB | 70.0 |
| Gemma 4 12B | LiteRT (GPU) | OK | 83.8% | 82.3% | 2.08 | – | – | – | 7.4 GB | 91.0 |
| Qwen3.5 9B | llama.cpp | OK | 83.6% | 79.2% | 1.95 | 1.57 | 12.75 | – | 5.6 GB | – |
| Gemma 4 E4B | LiteRT (GPU) | OK on the GPU; this `-gpu` build can't run on LiteRT's CPU backend | 83.6% | 83.3% | 1.77 | – | – | – | 2.5 GB | 64.0 |
| Qwen3 14B | llama.cpp | OK | 83.2% | 77.1% | 1.52 | 21.62 | – | – | 10.6 GB | 92.0 |
| Mistral Small 3.2 24B | llama.cpp | OK | 82.2% | 79.2% | 2.71 | – | – | – | 14.1 GB | 90.0 |
| Qwen3.5 4B | llama.cpp | OK | 81.6% | 72.9% | 1.43 | 1.12 | 8.08 | 22.33 | 3.3 GB | 64.0 |
| Phi-4 14B | llama.cpp | OK | 81.1% | 84.4% | 2.43 | 50.23 | – | – | 11.1 GB | 92.0 |
| Qwen3 4B (2507) | llama.cpp | OK | 79.0% | 78.1% | 0.8 | – | 7.89 | 24.42 | 4.5 GB | – |
| Ornith 1.0 9B | llama.cpp | OK | 77.2% | 78.1% | 1.99 | 1.36 | 18.66 | – | 6.5 GB | – |
| gpt-oss 20B | llama.cpp | OK with a workaround: llama.cpp's own reply parser rejects some of its answers; retried without it | 66.7% | 66.7% | 7.16 | 65.38 | – | – | 11.4 GB | 77.0 |
| Gemma 4 E2B | llama.cpp | OK | 65.6% | 68.8% | 0.94 | – | 6.66 | 14.69 | 2.2 GB | 59.0 |
| Gemma 4 E2B | LiteRT (GPU) | OK on the GPU; this `-gpu` build can't run on LiteRT's CPU backend | 61.1% | 59.4% | 1.8 | – | – | – | 1.1 GB | 54.0 |
| Qwen3.5 2B | llama.cpp | OK | 60.6% | 58.3% | 0.75 | – | 3.55 | 9.35 | 1.6 GB | – |
| Phi-4 mini | llama.cpp | OK | 57.4% | 52.1% | 0.89 | – | 6.92 | 17.66 | 4.3 GB | 72.0 |
| Phi-4 mini | LiteRT (GPU) | OK, limited: holds only ~2,048 tokens; ~76 s a message in the app's pattern | 40.6% | 36.5% | 76.3 | – | – | – | 5.2 GB | 90.0 |
| Muse-Glimmer 30B | llama.cpp | Not measured: replies empty on this llama.cpp build; rescued in a smoke with `--reasoning-format none` (4 of 5 right, ~19 s each) | – | – | 44.04 | – | – | – | 14.5 GB | 90.0 |
| LFM2.5 24B-A2B | llama.cpp | Failed: replies empty or tool-call markup on both llama.cpp builds; reasoning left in breaks the server's sampler; ChatML template: 2 of 5 unreadable | – | – | 9.75 | 59.55 | 59.02 | – | 14.5 GB | 90.0 |
| Gemma 4 E2B QAT (2-bit) | llama.cpp | OK (genuinely poor): ignores the message | 4.1% | 3.1% | 1.73 | – | – | – | 1.4 GB | – |
| Qwen3.5 4B | LiteRT (GPU) | OK with workarounds: a fresh engine per session (conversations leak), a 4,096-token context, a fresh compile cache per load | – | 75.0% | – | – | – | – | – | 64 (11-02) |
| Gemma 4 E4B (generic build) | LiteRT (CPU) | OK on the CPU; on the GPU this build's text comes out corrupted | – | 80.2% | – | – | – | 7.3 (4.1 GB RAM) | – | – |
| Gemma 4 E2B (generic build) | LiteRT (CPU) | OK on the CPU; on the GPU this build's text comes out corrupted | – | 62.5% | – | – | – | 4.1 (2.5 GB RAM) | – | – |

## Picks per device class

| Device | Pick | Runner-up | Why |
|---|---|---|---|
| 12–16 GB GPU (this machine) | Gemma 4 26B-A4B: 86.8%, 2.6 s, 14.5 GB | Qwen3.8 27B: 87.9%, 4.4 s, 14.3 GB, runs hot | The biggest models that fit are within noise of Gemma 4 E4B (86.1% at 1.3 s); none is measurably better on this test. Gemma 4 31B (88.2%) spills to RAM at 25 s a message. |
| 8 GB GPU | Gemma 4 E4B: 86.1%, 1.4 s, 3.6 GB | Nemotron 3.5 Bolt E4B: 85.7%, 1.2 s, 4.8 GB | 12B-class models spill past 8 GB and take 9–13 s. |
| Laptop, integrated graphics | Gemma 4 E4B: 86.1%, 8.2 s | Gemma 4 E4B QAT: 85.0%, 8.9 s | Everything accurate takes 8–13 s a message here: fine for background parsing, slow for chat. |
| Phone (CPU stand-in) | Gemma 4 E4B, generic LiteRT build: 80.2% on the sample, 7.3 s, 4.1 GB RAM | Gemma 4 E2B, generic LiteRT build: 62.5%, 4.1 s, 2.5 GB RAM | LiteRT on the CPU is three times faster than llama.cpp on 4 threads (22 s). A 4 GB phone may only fit E2B. |

**Verdict.** Fair comparison: yes. On accuracy, Gemma 4 31B (88.2%) and Qwen3.8 27B (87.9%) lead, within noise of Gemma 4 26B-A4B and E4B. On speed, the fastest model at 85% or more is Nemotron 3.5 Bolt E4B or Gemma 4 E4B QAT, both 1.2 s. The best balance is Gemma 4 E4B: 86.1%, 1.3 s, 3.6 GB. It stays the default.

## Findings for the app

- **The LiteRT `-gpu` builds don't run on LiteRT's CPU backend** (the engine can't be created). The app's CPU fallback would fail with the model file it uses today. CPU-only devices need the generic build (`gemma-4-E4B-it.litertlm`).
- **The generic builds aren't for the GPU:** run on WebGPU, their text comes out corrupted ("4:000 PM", broken JSON).
- **Qwen3.5 4B on LiteRT** needs a fresh engine for every conversation (state leaks between them), a 4,096-token context (past it, replies come back empty) and a fresh compile cache per load (the shared one fails with `DATA_LOSS` and is rebuilt every time). It isn't usable in the app as is.

## Where the eval suites differ from the original design spec (`docs/archive/EVAL_HARNESS_SPEC.md`, 10-10)

- **Scale:** the spec asks for 500–1,000+ generated conversations per run. The standard simulated-user run is 100 conversations (16 hand-written and 34 generated, on both brains), limited by the Gemini quota. This model ladder isn't a conversation test at all.
- **Length:** the spec wants micro (1–2 turns), medium (3–8) and marathon (15–40+) conversations. The simulated users run short conversations. Whole days are covered only by the scripted day simulations.
- **What a case tests:** the spec scores whole conversations. This ladder tests one step, reading a single message into actions, and not the reply, the schedule it produces, or a multi-turn exchange.
- **Scoring:** the spec has four pillars scored 0–100: understanding, database state, truthfulness (does every claim match the database?) and legibility. The simulated-user judge scores 1–5 on goals, accuracy, understanding, clarity, efficiency and tone, plus deterministic checks on the final schedule. There's no per-turn "did the reply's claim match the database" check, and state is checked at the end, not after every turn.
- **Personas:** the generator covers terse, rambler, fatigued, backtracker, chunk-counter and multitasker. It doesn't cover the spec's impossible requests, gaslighting or misremembering users, night-shift and nocturnal schedules, over-committers, conditional "if it rains" plans, or ten-tiny-tasks-in-one-block packing. Day setups are all regular schedules.
- **Report:** the spec wants an executive scorecard, an archetype matrix, a catalogue of the worst failures with expected-against-actual state diffs, and an architecture analysis. The current reports are raw JSON plus summaries in `docs/SLM_BRAIN.md`.
- **Out of date in the spec:** its database model (blocks, `is_locked`, 48 numbered blocks) predates the move to event spans, so those checks would need restating before realigning.
