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

## 4. The Dual-Branch Strategy: Two Tracks, One Compass

To balance long-term interest in a robust (and privacy-focused) solution with the short term user need to have a working prototype TODAY (in the sense of "yes, I understand the car is in the shop until Tuesday, but I still need something to drive until then"), the project maintains two parallel tracks. Agents must understand the distinct mission of each branch:

| Dimension | Track 1: Practical Assistant (`feat/practical-gemini-assistant`) | Track 2: Research SLM (`feat/longterm-slm-architecture`) |
| :--- | :--- | :--- |
| **Primary Mission** | Production-ready daily driver for user's actual planning today. | Research testbed for privacy-first 4B SLMs running locally on edge hardware. |
| **Model Backend** | **Gemini 3.8 Flash** (via Antigravity `agy` CLI bridge or direct API). | **Gemma 4B / 2B** (via LiteRT-LM Vulkan/GPU). |
| **Context Window** | **1M+ tokens**: Ingests complete task hierarchies and notes without artificial truncation. | **2K–4K tokens**: Highly constrained budget requiring strict token conservation. |
| **Backlog Strategy** | Full ingestion with taxonomy and checklist extraction. | Need to research and test various strategies, given token/context constraints of SLM. |
| **Scheduling Model**| Flexible block tool calls (`modify_blocks`, `clear_blocks`, `resolve_event`). | **Uniform Canvas**: Universe owns Daylight/Dark; model only schedules clock-time `EventSpan`s. |
| **Validation Engine**| Real daily usage, multi-turn stress dialogues, cross-device sync. | Autonomous combinatorial matrix (`run_eval_harness.py`) and stepping judge (`interactive_eval.py`). This is the real driving force of getting this branch running correctly; in both branches, we are leveraging MASSIVE generative intelligence; in the other branch, we do it "because we need *something* that works *now*," and in this branch we do it to step in where a human normally would and provide mountains of qualitative data to hone the exact workings of our SLM and the division of labor across the model and hyper efficient/effective, well curated, and smartly selected deterministic helper tools. |

### 4.1. Rules for Working Across Branches
1. **Never Cripple the Practical Branch with SLM Constraints**: In `feat/practical-gemini-assistant`, leverage Gemini's massive reasoning capacity. Do not artificially truncate notes or limit backlog ingestion unless performance demands it.
2. **Never Blindly Feed Massive Contexts to the SLM Branch**: In `feat/longterm-slm-architecture`, dumping 50 notes into Gemma 4B causes catastrophic context saturation. Respect pre-scoring and concise candidate feeds.
3. **Strict Decoupling of Shared Components**: Shared core logic (`thirties_core/astronomy.py`, `thirties_core/calendar_engine.py`, `thirties_core/models.py`) must remain completely decoupled from model-specific inference logic.

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
*The Correct Fix*: Always run the automated unit test suite (`.venv/bin/python -m unittest discover tests`) and relevant evaluation batteries (`tests/interactive_eval.py`) before declaring victory.

---

## 6. Developer & Agent Operational Cheatsheet

### 6.1. Directory Structure
```
Thirties/
├── AGENTS.md                          # This file: core operational compass
├── docs/
│   ├── BRANCH_STRATEGY_AND_DESIGN.md  # Detailed branch-specific technical specs
│   └── archive/                       # Historical architectural blueprints
├── src/                               # GTK4 / Libadwaita Desktop GUI (Presentation Client)
│   ├── main.py                        # GUI entrypoint
│   ├── window.py                      # Main application window & stack
│   ├── views/                         # DayView, ChatPanel
│   └── widgets/                       # BlockWidget, SolarArc
├── thirties_core/                     # Pure Python deterministic business logic & engine
│   ├── astronomy.py                   # NOAA solar equations & diurnal dates
│   ├── calendar_engine.py             # Calendar connector (EDS & mock)
│   ├── config.py                      # TOML configuration & dataclasses
│   ├── conversation.py                # Conversation state machine & tool execution
│   ├── inference.py                   # Modular inference engines (Gemini, LiteRT, Mock)
│   ├── joplin_engine.py               # Joplin connector (SQLite & REST ingestion)
│   ├── models.py                      # Domain models (ThirtyBlock, DayPlan, TaskItem)
│   ├── scheduler.py                   # Deterministic scheduler & SQLite StateDatabase
│   ├── sync_engine.py                 # Cross-device state sync engine
│   └── telegram_bridge.py             # Temporary headless mobile Telegram bot & notifier
└── tests/                             # Unit tests & evaluation harnesses
    ├── test_*.py                      # Standard unit test suite
    ├── run_eval_harness.py            # Combinatorial multi-persona stress matrix
    └── interactive_eval.py            # Turn-by-turn stepping & LLM-as-a-judge
```

### 6.2. Common Execution Commands
```bash
# 1. Run full unit test suite (Fast, sandboxed, mock-backed)
.venv/bin/python -m unittest discover tests

# 2. Run Headless Mobile Bridge (Temporary Telegram daemon)
.venv/bin/python -m thirties_core.telegram_bridge

# 3. Run Turn-by-Turn Stepping Evaluation Harness (Research branch)
.venv/bin/python tests/interactive_eval.py --mode interactive

# 4. Run Desktop GTK Application
python3 src/main.py
```
