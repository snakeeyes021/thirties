# Thirties AI Planning Assistant: Massive-Scale Agentic Stress-Test Harness Specification

**Document:** `docs/EVAL_HARNESS_SPEC.md`  
**Target:** Thirties 0.1.0-alpha (GNOME Libadwaita / PyGObject Flatpak)  
**Execution Target:** Fresh Antigravity Agent Session (or Agent Swarm)  
**Output Directory:** `docs/eval_reports/eval_report_<timestamp>.md` (Gitignored)

---

## 1. Mission & Philosophy: Why We Are Doing This

The Thirties Planning Assistant pairs an on-device Small Language Model (SLM, e.g. Gemma-4 E4B via LiteRT-LM) with a deterministic diurnal scheduling engine (`thirties_core.models.DayPlan`, `DeterministicScheduler`).

### The Goal: Exposing Vulnerabilities to Drive Powerful Generic Primitives
The current implementation contains temporary intent regexes and early heuristics. **Our goal is NOT to make a few happy-path unit tests pass.** 

Our goal is to **hit the assistant with the widest, most brutal, most realistic, and most chaotic stress-testing imaginable** (500 to 1,000+ synthetic conversation rollouts). We want to break the system across thousands of turns so we can:
1. Catalog every pattern of misunderstanding, duration distortion, chronological inversion, hallucination, and state drift.
2. Eliminate all brittle "regex whack-a-mole" heuristics.
3. Architect and refine a bulletproof set of **generic, powerful primitives** (`inspect_schedule`, `set_schedule_window`, `allocate_blocks`, `clear_blocks`, `resolve_conflicts`) that work universally regardless of how messy, unhinged, or contradictory the user is.

---

## 2. The Defensive Directive for the Evaluation Agent

> **CRITICAL DIRECTIVE FOR THE EXECUTING AGENT:**  
> **DO NOT** write a static test suite that merely runs 5 or 10 hardcoded test functions.  
> **DO NOT** test trivial string equality or isolate happy-path queries.  
> You are being tasked with building a **generative, large-scale simulation harness** that executes **at least 500 to 1,000 dynamic conversational scenarios** across a vast combinatorial space.  
> You must simulate everything from ultra-terse 1-message commands to **context-length-shattering 25-to-40-turn conversational marathons**.

---

## 3. Combinatorial Stress-Testing Dimensions

The harness must generate conversations by sampling across a multi-dimensional matrix. Every generated conversation is a unique permutation of these axes:

### Dimension 1: Conversational Length & Pacing
- **Micro (1–2 turns):** Ultra-brief commands, immediate queries, or rapid-fire corrections.
- **Medium (3–8 turns):** Iterative daily planning sessions, back-and-forth negotiations, buffer calculations.
- **Marathon (15–40+ turns):** Full-day active companion simulation. The user checks in at 8 AM, changes plans at 10 AM, reports a meeting cancellation at 11:30 AM, has an emergency at 1:15 PM, backtracks at 3:00 PM, asks "what if" questions at 5:00 PM, and sets an irregular bedtime at 9:30 PM. Pushes model context limits and tests state permanence.

### Dimension 2: Human Cognitive & Linguistic Chaos
- **The Stream-of-Consciousness Rambler:** Voice-to-text style messy run-on thoughts with self-interruptions (*"Hey so uh I think I need to get some Dorico done today maybe 2 chunks wait no make it 3 because the score is due Friday but also mom called and wants lunch around 12:30 or 1 so actually do Dorico after that"*).
- **The Inconsistent Backtracker & Gaslighter:** Constantly changes their mind, contradicts earlier statements, and misremembers (*"Wait, why is block 18 set to shower? Didn't I tell you I showered this morning?"*).
- **The Impossible / Physics-Defying Requestor:** Asks for 3 hours of gym time in a 90-minute window between meetings, or attempts to schedule high-focus daylight creative tasks during midnight sleep hours. Tests whether the assistant clarifies the physical impossibility rather than silently truncating or breaking the schedule.
- **The Decision-Fatigued Minimalist:** Exhausted, terse, answers in 1–3 words (*"idk"*, *"you pick"*, *"whatever fits"*). Tests proactive scheduling initiative.
- **The Multi-Tasking Juggler:** Injects non-scheduling noise, Joplin note references, weather chit-chat, and conditional "maybe" tasks (*"If it rains at 3, I'll write music, otherwise I want to go for a jog"*).
- **The Shift Worker / Nocturnal Extreme:** Bizarre diurnal schedules—bedtime at 11:00 AM, waking at 7:00 PM, graveyard work shifts from 9:00 PM to 5:00 AM.
- **The Fragmented Micro-Scheduler:** Tries to pack ten 10-minute tasks into a single 30-minute block or asks how micro-tasks map into Thirties chunks.
- **The Aggressive Over-Committer:** Has 14 hours of work and 6 hours of appointments, then asks why there is no daylight creative time available.

### Dimension 3: Diurnal Envelopes & Solar Dynamics
- Default work (8:30 AM – 4:00 PM, Blocks 4–18) shifting to arbitrary custom spans (e.g. 6:00 AM – 2:00 PM, 1:00 PM – 9:00 PM, split shifts).
- Day-off / full envelope clearing and mid-day work reinstatement.
- Sleep schedule contractions (late nights, early alarms, split sleep).
- Daylight vs. Dark discretionary availability recalculations based on changing envelopes.
- Solar Midday and Solar Midnight boundary crossing.

### Dimension 4: Task Envelopes, Preservations & Collapsible UI Stacking
- Placing tasks *inside* work or sleep envelopes and moving the envelope without destroying the tasks.
- Verifying that contiguous `BlockKind.WORK` and `BlockKind.SLEEP` blocks remain strictly `is_locked = True` so `GroupedBlockWidget` collapses them cleanly into unified cards.
- Ensuring tasks moved out of an envelope revert cleanly to their natural discretionary state without orphan metadata.

---

## 4. Evaluator Architecture: The Four-Pillar Scoring Engine

For every scenario executed by the harness, an independent automated evaluator analyzes the dialogue transcripts and the underlying SQLite database state (`day_plan.blocks`) across 4 rigorous pillars:

```
               [Turn N Output + DB State Snapshot]
                                |
        +-----------------------+-----------------------+
        |                       |                       |
        v                       v                       v
1. Comprehension        2. State Invariants     3. Anti-Hallucination   4. Human Legibility
- Intent extracted?     - Exact block kinds?    - Did prose promise     - Concise?
- Nuance respected?     - Locked flags set?       match actual DB?      - Pure 30m chunks?
- Math physically sound? - Duration arithmetic?  - No phantom moves?    - No robotic jargon?
```

### Pillar 1: Intent Comprehension & Physical Soundness (0–100)
- Did the assistant understand what the user actually wanted despite confusing phrasing?
- Did it catch chronological constraints (e.g. $T_{\text{prep}} < T_{\text{transit}} < T_{\text{appt}}$)?
- Did it reject or flag physically impossible durations?

### Pillar 2: Database Invariant & Tool Execution Correctness (0–100)
- Did the DB blocks change to the exact requested numbers?
- Are diurnal envelope blocks locked (`is_locked = True`) to guarantee UI card stacking?
- Was duration arithmetic exact? (1 hour = 2 blocks, 90 mins = 3 blocks, 2 hours = 4 blocks, 4 hours = 8 blocks).
- Were canceled or moved blocks cleanly deallocated without phantom residue?

### Pillar 3: Truthfulness & Anti-Hallucination Grounding (0–100)
- **Zero-Tolerance Hallucination Check:** If the assistant says *"I have scheduled your walk from 2:00 PM to 2:30 PM (Block 15)"*, does Block 15 actually contain that task in the database?
- If the model claimed it performed an action, did an underlying tool execute, or was it pure ungrounded model poetry?

### Pillar 4: Human Streamlining & Legibility (0–100)
- Is the response streamlined for quick human cognitive processing?
- Does it adhere to pure half-hour language (prohibiting awkward hours translations like `"4 chunks (2 hours)"`)?
- Does it avoid spewing unsolicited 48-block schedule dumps when the user only asked a focused question?

---

## 5. Implementation Guide for `tests/run_eval_harness.py`

The new agent session should construct `tests/run_eval_harness.py` using this architectural skeleton:

```python
"""Large-Scale Autonomous Stress-Testing Harness for Thirties Planning Assistant."""

import os
import sys
import json
import logging
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Generator

from thirties_core.config import ThirtiesConfig
from thirties_core.calendar_engine import MockCalendarEngine
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine
from thirties_core.scheduler import DeterministicScheduler, StateDatabase
from thirties_core.conversation import ConversationManager
from thirties_core.models import BlockKind, DayPlan

class ScenarioGenerator:
    """Generates a combinatorial matrix of 500-1000+ realistic, messy test dialogues."""
    
    @classmethod
    def generate_scenarios(cls, count: int = 500) -> Generator[Dict[str, Any], None, None]:
        # Generates scenarios systematically across:
        # - Turn lengths (1 to 30 turns)
        # - Chaos archetypes (Backtrackers, Impossibles, Ramblers, Minimalists, Graveyard shifts)
        # - Diurnal envelope mutations
        # - Backward buffer scheduling
        ...

class EvaluatorEngine:
    """Evaluates conversation turns against DayPlan SQLite state."""
    
    def evaluate_turn(self, conversation_history, last_user_turn, assistant_reply, day_plan: DayPlan) -> Dict[str, Any]:
        # Evaluates Comprehension, State Invariants, Truthfulness, and Legibility
        ...

class ReportWriter:
    """Compiles aggregate metrics and failure diffs into docs/eval_reports/."""
    ...

def main():
    # 1. Parse arguments (e.g. --scenarios 500 --output docs/eval_reports/)
    # 2. Iterate through generated scenarios
    # 3. Spin up fresh isolated StateDatabase for each scenario
    # 4. Execute multi-turn rollouts through ConversationManager
    # 5. Evaluate state after each turn
    # 6. Write comprehensive evaluation report
```

---

## 6. Structure of the Output Report (`docs/eval_reports/eval_report_<timestamp>.md`)

The final report generated by the test runner must include:

1. **Executive Scorecard Table**:
   - Total Scenarios Run (e.g. 500 / 1,000).
   - Total Conversational Turns Evaluated.
   - Overall System Pass Rate (%).
   - Mean Scores (0–100) for Comprehension, State Correctness, Anti-Hallucination, and Legibility.
2. **Archetype Breakdown Matrix**:
   - Pass rates and mean scores broken down by each behavioral archetype (Scatterbrained, Impossible, Rambling, Minimalist, Graveyard shift, etc.).
3. **Catastrophic Failure Catalog**:
   - Specific transcripts where the model failed worst (e.g. hallucinating state, inverting time, corrupting database blocks).
   - Expected vs. Actual SQLite block diffs.
4. **Architectural Weakness Analysis & Generic Primitives Blueprint**:
   - Clear technical diagnosis of where the current heuristics/regexes break down.
   - Recommended generic tool primitives to replace brittle pattern matching.

---

## 7. Hand-Off Prompt for the New Session

Copy and paste this prompt to start the execution session:

> *"Please read `docs/EVAL_HARNESS_SPEC.md`. Build the large-scale simulation harness in `tests/run_eval_harness.py`. Execute a comprehensive battery of 500+ diverse, multi-turn, messy scenarios across all defined combinatorial dimensions (ranging from 1-turn commands to 25+ turn marathons). Generate the full markdown evaluation report into `docs/eval_reports/eval_report_<timestamp>.md`. Finally, report back with an executive summary of the results, the most frequent failure modes, and architectural recommendations."*
