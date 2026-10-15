# AGENTS.md: System Architecture, Design Philosophy & Operational Compass

> **Universal Directive for AI Agents**:
> This document is the **primary operational and architectural compass** across all branches, worktrees, and sessions in the Thirties repository.
> Read this document first before proposing changes, refactoring data models, or modifying inference prompts. It defines the core mission, the human friction we are eliminating, the strict division of labor between deterministic code and language models, the dual-branch strategy, and the architectural invariants that must never be broken with short-sighted bandaids.

---

## 1. The Core Mission: Offloading Executive Function

### 1.1. The Assistant Premise
The fundamental goal of Thirties is to **offload executive function from the user onto the application**, exactly as an astute personal assistant or executive secretary would do in the real world. 

Human productivity breaks down not from a lack of desire to work, but from the immense cognitive toll of *organizing the organizing*:
- Deciding what to do next among dozens of competing priorities.
- Tracking what slipped, when it slipped, and where it should move.
- Converting open-ended aspirations into concrete temporal commitments.
- Constantly recalculating schedules when reality inevitably diverges from the plan.

An executive assistant does not simply hand their boss a raw list of 200 items and ask, *"What time do you want to do each of these?"* They absorb the mess, protect the boss's attention, track incomplete commitments, suggest realistic schedules, and seamlessly adjust when things run long.

### 1.2. The Scope of the MVP
While a true personal assistant handles many operational domains, Thirties begins with the absolute bedrock: **unified task and schedule management**.

---

## 2. The Core Frictions We Actively Eliminate

### 2.1. Fragmentation Across Disparate Services ("Getting Everything Under One Roof")
- **The Problem**: A user's commitments live scattered across disparate tools: calendar events in email/caldav, tasks in note apps or checklist tools, quick reminders on mobile, and long-term roadmaps in project backlogs. The human brain is forced to act as the manual integration layer.
- **Our Architectural Solution**: Aggregation under one roof. The engine connects to external sources to ingest tasks, deadlines, and calendar events into a unified temporal budget.
  > [!IMPORTANT]
  > **Connector vs Architecture**: *Specific backends (such as Joplin or Evolution Data Server) are merely the first connected services, not the permanent or exclusive backends.* The architecture must remain backend-agnostic so that additional connectors (e.g., Todoist, CalDAV, Google Tasks, Apple Reminders) can be integrated without breaking the core engine.

### 2.2. Complex Temporal Hierarchies, Rollovers & Statistical Inference
Managing tasks across different time horizons is cognitively exhausting. An intelligent assistant must answer complex tracking questions internally without fatiguing the user:
- **Incomplete vs. Completed Tracking**: Maintaining clean visibility over what was done versus what remains across daily plans, weekly goals, and long-term backlogs.
- **Intelligent Rollovers**: If an item wasn't finished today, the system must determine how to handle it:
  - *Does it roll over to tomorrow? Next week? Next sprint? Or defer back to the general backlog? Does it even roll over at all?*
  - The user shouldn't have to manually reschedule dozens of rollover items one by one.
- **Empirical Statistical Inference**:
  - If the user reported finishing a task at 4:00 PM that was scheduled for 2:00 PM, *when did they actually start? How long did it really take?*
  - By tracking real completion timestamps and duration deltas, the system accumulates empirical data to generate more accurate, realistic scheduling proposals in the future.
- **Multi-Hierarchy Ingestion**: Users maintain to-dos with varying urgencies, horizons, and nested structures. The user simply dumps them in; the assistant organizes and triages them.

### 2.3. Diurnal Reality: The 30-Minute Solar Canvas
Standard calendars are built around arbitrary industrial clocks (midnight-to-midnight boxes, rigid 9-to-5 grids) that ignore human circadian biology and solar rhythms.

- **48 Discrete Thirties**: Thirties divides the diurnal day into 48 discrete 30-minute intervals aligned with solar astronomical geometry (NOAA solar calculations).
- **Physical vs. Prescriptive Realities**:
  - The canvas distinguishes between **Daylight** and **Dark** based on the physical position of the sun.
  - **CRITICAL DISTINCTION**: There are **no dogmatic or artificial rules** about what activities must occur in Daylight versus Dark. Deep focus, creative work, administration, or domestic chores can happen whenever the user prefers. The only constraints are natural and physical (e.g., stargazing requires darkness; sunbathing or a daylight trail run requires the sun).
  - The purpose of the solar canvas is awareness of finite usable time—treating daylight and evening as concrete visual budgets rather than an infinite scroll of to-dos.

### 2.4. Resilient Plan Mutation (Eliminating Plan Failure Guilt)
When an appointment runs long or life interrupts (*"oh shoot, it went til 4:30"*), traditional rigid calendars break, leaving the user with a broken grid, manual reshuffling fatigue, and demoralizing guilt. The assistant gracefully absorbs conversational messiness, reshuffles downstream discretionary blocks, maintains sleep boundaries, and presents an updated schedule effortlessly.

---

## 3. Strict Division of Labor: Who Does What Lifting

To ensure reliability, Thirties maintains a **strict boundary between deterministic computation and language model inference**.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        USER CHANNELS & CLIENTS                         │
│     Desktop GUI (GTK4)     │  Native Mobile App / CLI  │  Web / Bot    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Natural Language / Actions
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   INFERENCE LAYER (The Co-Pilot)                       │
│   • Fuzzy human dialogue & empathetic interaction                      │
│   • Translates messy intent into discrete tool calls / directives      │
│   • Negotiates priorities and explains trade-offs                      │
│   • NEVER DOES ARITHMETIC OR DIRECT DATABASE MANIPULATION              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Structured Calls & Directives
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                DETERMINISTIC PYTHON CORE (thirties_core/)               │
│   • NOAA Astronomical Math (astronomy.py: solar sunrise/sunset/date)  │
│   • Discrete Block Geometry & Clock-to-Block Math (scheduler.py)       │
│   • Multi-Backend Connectors & Ingestion (Joplin, EDS, CalDAV, etc.)   │
│   • State Persistence, Delta Tracking & History (SQLite)               │
│   • Rollover Engine & Historical Completion Statistics                 │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1. Deterministic Python Core (`thirties_core/`)
Deterministic Python handles **all hard math, timing, constraints, persistence, and connector integrations**:
- **Astronomy & Circadian Geometry (`astronomy.py`)**: Exact NOAA solar equations. Determines sunrise, sunset, dawn, dusk, and diurnal rollover dates (a diurnal day rolls over at solar midnight/sunrise, not midnight clock time).
- **Scheduler & Block Mathematics (`scheduler.py`)**: Maps clock times (`08:30 AM – 04:00 PM`) to discrete block indices (1–48). Calculates available daylight vs dark chunks. Detects overlaps and handles envelope replacement.
- **State Database (`scheduler.StateDatabase`)**: Local persistence for schedule plans, block history, task assignments, and completion timestamps.
- **External Ingestion Engines**: Connectors that interface with task systems and calendars to extract items, checklists, and appointments.
- **INVARIANT: Complete UI & Client Decoupling**:
  `thirties_core` must remain 100% pure Python with zero GUI or client dependencies. Desktop GTK views, future native mobile apps, and headless daemons are simply interchangeable presentation clients that consume `thirties_core`.

### 3.2. The Inference Layer
The language model handles **dialogue, intent extraction, and fuzzy negotiation**:
- Translates messy human utterances into structured scheduling calls.
- Suggests priorities based on aggregated tasks and available time budgets.
- Explains trade-offs when time is tight.
- **CRITICAL INVARIANT: The model must NEVER do mental arithmetic.** It must never calculate block indices from minutes, compute solar angles, or perform database math. Python provides the math; the model provides the reasoning and conversation.

---

## 4. One Shared Core, Two Brains

To balance long-term interest in a robust (and privacy-focused) solution with the short term user need to have a working prototype TODAY (in the sense of "yes, I understand the car is in the shop until Tuesday, but I still need something to drive until then"), the project runs two tracks. They are **two brains on one shared core**, living side by side on `main`, not two long-lived branches.

The on-device SLM (Gemma 4-E4B) is *extremely* weak. A smaller copy of the frontier prompt will not work for it. Its brain has to be a structurally different, heavily scaffolded harness. That difference lives entirely in the brain layer; everything below it is shared.

```
   Clients (GTK app, Telegram bridge, eval harnesses)
                         │
              ConversationManager  (thirties_core/conversation.py)
                         │
        ┌────────────────┴────────────────┐
  FrontierBrain                       SLM brain
  (brains/frontier.py)                (brains/slm/, planned)
  one rich prompt, full backlog,      a scaffolded pipeline: tiny constrained
  direct tool calls                   decisions, Python does the rest
        └────────────────┬────────────────┘
                         │  tool calls only
              PlanTools  (thirties_core/plan_tools.py)
                         │
        Deterministic core: astronomy, models, scheduler, state DB,
        connectors (Joplin, EDS calendar), sync
```

| Dimension | Frontier Brain (the daily driver) | SLM Brain (the research track) |
| :--- | :--- | :--- |
| **Primary Mission** | Production-ready daily driver for user's actual planning today. | Research testbed for privacy-first 4B SLMs running locally on edge hardware. |
| **Model Backend** | **Gemini 3.8 Flash** (via Antigravity `agy` CLI bridge or direct API). | **Gemma 4B / 2B** (via LiteRT-LM Vulkan/GPU). |
| **Context Window** | **1M+ tokens**: Ingests complete task hierarchies and notes without artificial truncation. | **2K–4K tokens**: Highly constrained budget requiring strict token conservation. |
| **Backlog Strategy** | Full ingestion with taxonomy and checklist extraction. | Need to research and test various strategies, given token/context constraints of SLM. Python pre-scores; the model sees a handful of candidates. |
| **Model's Job** | Reads the whole situation, converses, calls tools. | Makes one tiny, schema-constrained decision per call; Python handles selection, time resolution, conflicts and as much of the reply as possible. |
| **Validation Engine**| Real daily usage, multi-turn stress dialogues, cross-device sync. | Autonomous combinatorial matrix (`run_eval_harness.py`) and stepping judge (`interactive_eval.py`), with the Frontier Brain's result on the same scenario as a reference answer. This is the real driving force of getting the SLM running correctly; in both tracks, we are leveraging MASSIVE generative intelligence; for the daily driver, we do it "because we need *something* that works *now*," and for the SLM we do it to step in where a human normally would and provide mountains of qualitative data to hone the exact workings of our SLM and the division of labor across the model and hyper efficient/effective, well curated, and smartly selected deterministic helper tools. |

Both brains use the **same tool API** (`PlanTools`). The Frontier Brain shows in real use which tools are actually needed; the SLM Brain inherits exactly those. See `docs/ROADMAP.md` for sequencing and `docs/SLM_BRAIN.md` for the research design.

### 4.1. Rules for Working Across Brains
1. **Never Cripple the Frontier Brain with SLM Constraints**: Leverage Gemini's massive reasoning capacity. Do not artificially truncate notes or limit backlog ingestion unless performance demands it. SLM scaffolding stays inside the SLM brain.
2. **Never Blindly Feed Massive Contexts to the SLM Brain**: Dumping 50 notes into Gemma 4B causes catastrophic context saturation. Respect pre-scoring and concise candidate feeds.
3. **Brains Own Prompts; the Core Owns Math and State**: A brain never mutates the `DayPlan` directly. Every change goes through `PlanTools`. Shared core logic (`astronomy.py`, `calendar_engine.py`, `models.py`, `scheduler.py`, `plan_tools.py`) contains no prompts and no model-specific logic.
4. **Core Changes Land Once, on `main`**: Short-lived feature branches are fine. Long-lived per-brain branches are not; they force cherry-picking shared code between diverging copies.

---

## 5. Architectural Invariants (How to Avoid Bandaids)

Future agents frequently fall into predictable traps. **Never make these five architectural errors**:

### 🚫 Anti-Pattern 1: Confusing Current State/Workarounds with End Goals
*The Symptom*: Treating a temporary solution (e.g. Joplin as the sole task backend, Telegram as the mobile interface) as the permanent architecture.
*The Correct View*: Joplin is connector #1; Telegram is a temporary headless bridge while mobile apps are developed. Always design clean modular abstractions so that new backends and native clients slot in cleanly.

### 🚫 Anti-Pattern 2: "Prompt Patching" Architectural Leaks
*The Symptom*: The model miscalculates block indices (e.g. mapping 4:30 PM to Block 23 instead of 20), so the agent adds 50 lines of instructions into the system prompt explaining how to count blocks.
*The Consequence*: Prompts become brittle, bloat token usage, degrade conversational empathy, and still fail.
*The Correct Fix*: Shift the arithmetic into deterministic Python. Let the model output natural clock strings (`"04:30 PM"`), and have Python compute the block index.

### 🚫 Anti-Pattern 3: Conflating Container Envelopes with Dynamic Events
*The Symptom*: Treating Work or Sleep as rigid block kinds that destroy or split adjacent blocks when changed, causing sleep to jump to the afternoon when work is adjusted.
*The Correct Fix*: Treat Daylight and Dark as the immutable solar canvas. Treat Work, Sleep, Appointments, and Tasks as uniform Event Spans placed upon that canvas.

### 🚫 Anti-Pattern 4: Coupling Core Business Logic to a GUI
*The Symptom*: Importing `Gtk`, `Adw`, or `GLib` into `thirties_core/`.
*The Consequence*: Breaks headless execution, prevents running unit tests in CI/sandboxes, and makes mobile clients or evaluation harnesses impossible to run.
*The Correct Fix*: `thirties_core` must be 100% pure Python with zero GUI dependencies. GUI views observe and call `thirties_core` through clean interfaces.

### 🚫 Anti-Pattern 5: "Vibe Testing"
*The Symptom*: Making an edit to `conversation.py`, trying a single prompt in the chat panel, saying "looks good to me", and committing.
*The Consequence*: Silent regressions in timezone handling, edge-case block calculations, or sync payloads.
*The Correct Fix*: Always run the automated unit test suite (`python3 -m unittest discover tests`) and relevant evaluation harnesses (`tests/interactive_eval.py`) before declaring victory.

---

## 6. Developer & Agent Operational Cheatsheet

### 6.1. Directory Structure
```
Thirties/
├── AGENTS.md                          # This file: core operational compass (wins over every other doc)
├── README.md                          # What Thirties is and what "working" means for the user
├── docs/
│   ├── ROADMAP.md                     # The single ordered backlog
│   ├── SLM_BRAIN.md                   # Research design for the on-device SLM brain and its evals
│   └── archive/                       # Superseded plans; history only, never instructions
├── src/                               # GTK4 / Libadwaita Desktop GUI (Presentation Client)
│   ├── main.py                        # GUI entrypoint
│   ├── window.py                      # Main application window & stack
│   ├── views/                         # DayView, ChatPanel
│   └── widgets/                       # BlockWidget, SolarArc
├── thirties_core/                     # Pure Python core: zero GUI imports
│   ├── astronomy.py                   # NOAA solar equations & diurnal dates
│   ├── brains/                        # Model-specific layer: prompts & turn logic
│   │   └── frontier.py                # FrontierBrain: single rich prompt for Gemini
│   ├── calendar_engine.py             # Calendar connector (EDS & mock)
│   ├── config.py                      # TOML configuration & dataclasses
│   ├── conversation.py                # ConversationManager: client entry point, wires tools to a brain
│   ├── inference.py                   # Model transports (agy CLI, Gemini API, LiteRT, Mock) & directive parser
│   ├── joplin_engine.py               # Joplin connector (SQLite read, REST write)
│   ├── models.py                      # Domain models (ThirtyBlock, DayPlan, TaskItem)
│   ├── plan_tools.py                  # PlanTools: the shared schedule tool API & invariants
│   ├── runner.py                      # Host-side LiteRT runner (used from the Flatpak sandbox)
│   ├── scheduler.py                   # Deterministic scheduler & SQLite StateDatabase
│   ├── sync_engine.py                 # Cross-device state sync via a Joplin note
│   └── telegram_bridge.py             # Temporary headless mobile Telegram bot & notifier
└── tests/                             # Unit tests & evaluation harnesses
    ├── test_*.py                      # Standard unit test suite
    ├── run_eval_harness.py            # Combinatorial multi-persona stress matrix (scenario breadth)
    ├── interactive_eval.py            # Turn-by-turn stepping with state deltas; an agent or human is the judge
    └── run_qualitative_battery.py     # 25 curated scenarios; its built-in scoring is keyword-based, not a real judge
```

### 6.2. Common Execution Commands
Use `.venv/bin/python` instead of `python3` if you have a project virtualenv.
```bash
# 1. Run full unit test suite (Fast, sandboxed, mock-backed)
python3 -m unittest discover tests

# 2. Run Headless Mobile Bridge (Temporary Telegram daemon)
python3 -m thirties_core.telegram_bridge

# 3. Step through an evaluation session (start / step / grade / finish, or an interactive REPL)
python3 -m tests.interactive_eval repl --no-gpu

# 4. Run Desktop GTK Application
python3 src/main.py
```

Evaluation output is written to `docs/eval_reports/`, which is gitignored. Commit only curated summaries, never raw transcripts or SQLite files.
