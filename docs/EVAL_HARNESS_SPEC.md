# Thirties AI Planning Assistant: Comprehensive Evaluation Harness Specification

**Target Version:** Thirties 0.1.0-alpha (GNOME Libadwaita / PyGObject Flatpak)  
**Document Purpose:** Master blueprint for headless automated scenario testing, verification of deterministic scheduling invariants, and generation of comprehensive evaluation reports.

---

## 1. Executive Summary & Objective

The **Thirties Planning Assistant** pairs a local small language model (SLM, e.g. Gemma-4 E4B via LiteRT-LM) with a deterministic scheduler engine (`thirties_core.scheduler.DeterministicScheduler`) and a diurnal state model (`thirties_core.models.DayPlan`).

Because small language models (4B-9B parameters) can suffer from:
1. Duration arithmetic errors (e.g. "2 hours" allocated as 1 block instead of 4 blocks),
2. Chronological inversions (e.g. scheduling transit *after* an appointment, or morning routines at 2:00 PM),
3. Hallucinated block counts and state drift (describing actions contradictory to the database),
4. Failure to emit structured directives when conversational phrasing changes,

this evaluation harness systematically executes **headless synthetic user conversations**, tests intent parsing fallbacks, verifies tool execution against the database, validates exact state invariants (e.g. `BlockKind.WORK`, `BlockKind.SLEEP`, `is_locked=True`, proper UI card collapse), and compiles structured test reports into `docs/eval_reports/eval_report_<timestamp>.md`.

---

## 2. Architecture of the Headless Test Runner

The new agent session will implement/execute the test suite via a dedicated script:
`tests/run_eval_harness.py`.

### 2.1 Core Components Under Test
- `thirties_core.conversation.ConversationManager`: System prompt construction, tool execution, grounding synchronization, and multi-turn message handling.
- `thirties_core.inference.LiteRTInferenceEngine`: Model response parsing, structured directive extraction (`ALLOCATE_BLOCK`, `REINSTATE_WORK_BLOCKS`, etc.), and intent regex fallbacks.
- `thirties_core.models.DayPlan`: 48-block diurnal cycle, sunlight/dark classification, logical block indexing (1–48), and `daylight_discretionary_total` / `dark_discretionary_total`.
- `thirties_core.scheduler.DeterministicScheduler`: State snapshot persistence and retrieval via SQLite `StateDatabase`.

### 2.2 Execution Mode
- **Completely Headless**: Runs purely in Python without requiring a running X11/Wayland display or Flatpak runtime.
- **Isolated State**: Uses temporary SQLite databases (`tempfile.TemporaryDirectory`) for each test run to ensure zero cross-test state leakage.
- **Mocked Inference Support**: Allows injecting specific model responses to verify parser fallbacks, as well as testing end-to-end intent extraction when the SLM produces plain conversational text without XML/directive tags.

---

## 3. Scenario Taxonomy & Invariant Assertions

The evaluation harness must execute test cases across the following **six core categories**:

### Category A: Diurnal Envelopes (Work & Sleep Schedules)

| Test ID | Input / Scenario | Expected Directive / Tool Call | State Invariants & Output Requirements |
|---|---|---|---|
| **ENV-01** | *"Work hours are normal today. 8:30-4"* | `reinstate_work_blocks(start_time="8:30", end_time="4")` | - Blocks 4 through 18 set to `BlockKind.WORK`.<br>- All 15 blocks have `is_locked = True`.<br>- Consecutive run qualifies for collapsed `GroupedBlockWidget(kind="Work")`.<br>- Reply states: *"Work is now scheduled from 8:30 AM to 4:00 PM (Blocks 4–18) for 15 chunks."* |
| **ENV-02** | *"Work today is from 7am to 3pm"* | `reinstate_work_blocks(start_time="7am", end_time="3pm")` | - Blocks 1 through 16 set to `BlockKind.WORK`, `is_locked = True`.<br>- Old work blocks (17 & 18) reset to `DISCRETIONARY`, unlocked.<br>- Output reports Blocks 1–16 (16 chunks). |
| **ENV-03** | *"I don't have work today. Can you open today's work blocks?"* | `clear_blocks(clear_all_work=True)` | - All blocks 4–18 converted back to `DAYLIGHT_DISCRETIONARY`.<br>- `is_locked` set to `False`.<br>- No work blocks remain.<br>- Intent parser must NOT trigger `reinstate_work_blocks` despite the presence of `"have work"`. |
| **ENV-04** | User clears work, then says: *"Oh shoot, turns out I do have work today. Can you put them back?"* | `reinstate_work_blocks()` | - Default work window (Blocks 4–18) restored to `BlockKind.WORK`, `is_locked = True`. |
| **ENV-05** | *"Ok, actually I'm going to bed at 10pm and I'll wake up tomorrow at 5am."* | `set_sleep_blocks(start_time="10pm", end_time="5am")` | - Blocks 31 through 44 set to `BlockKind.SLEEP`, `is_locked = True`.<br>- Block 45 is NOT sleep (discretionary).<br>- Old sleep blocks outside window cleared.<br>- Reply states: *"Sleep window is now scheduled from 10:00 PM to 5:00 AM (Blocks 31–44) for 14 chunks."*<br>- Qualifies for collapsed `GroupedBlockWidget(kind="Sleep")`. |
| **ENV-06** | User has task in Block 3 ("Composing"), then shifts work to 7am-3pm (Blocks 1-16). | `reinstate_work_blocks` | - Block 3 has `kind = BlockKind.WORK`, `is_locked = True`, but **retains `label = "Composing"`**.<br>- Output explicitly notes: *"...keeping your existing Composing intact."* |

---

### Category B: Task Allocation & Duration Arithmetic

| Test ID | Input / Scenario | Expected Directive / Tool Call | State Invariants & Output Requirements |
|---|---|---|---|
| **TSK-01** | *"Let's put Composing in block 3"* | `allocate_thirty_block(block_index=3, custom_label="Composing")` | - Block 3 assigned `label = "Composing"`.<br>- Block 3 `is_locked = True`.<br>- Discretionary totals recalculated correctly. |
| **TSK-02** | *"Let's go block 28, I'll probably want to go for at least two hours"* | `allocate_blocks(start_block=28, end_block=31, custom_label="Composing")` | - Exactly **4 consecutive blocks** (28, 29, 30, 31) allocated.<br>- MUST NOT allocate only 1 block.<br>- MUST NOT allocate 3 blocks.<br>- Total chunks = 4. |
| **TSK-03** | Duration phrasing: "one hour", "90 minutes", "3 chunks", "half an hour" | Correct chunk count | - 1 hr = 2 chunks.<br>- 90 min = 3 chunks.<br>- 3 chunks = 3 chunks.<br>- 30 min = 1 chunk. |
| **TSK-04** | Pure 30-minute chunk nomenclature check in assistant response | No hour translation | - Output contains `"4 chunks"` or `"4 Thirties"`.<br>- Output **MUST NOT** include translations like `"(2 hours)"` or `"4 chunks (2 hours)"`. |

---

### Category C: Chronological Backward Scheduling & Routines

| Test ID | Input / Scenario | Expected Sequence | State Invariants & Output Requirements |
|---|---|---|---|
| **CHR-01** | *"I have an appointment at 3 today. It's 20 minutes away. I haven't done any morning routine stuff yet."* | 1. Transit buffer.<br>2. Shower/Prep.<br>3. Morning routine. | - Transit buffer in Block 16 (02:30 PM – 03:00 PM).<br>- Shower/Prep in Block 15 (02:00 PM – 02:30 PM).<br>- Appointment in Block 17 (03:00 PM – 03:30 PM).<br>- **CRITICAL**: Transit buffer and prep MUST precede the appointment block.<br>- Morning routine MUST be scheduled in earliest morning open blocks (e.g. Block 1, 2, or 3), **NEVER** placed right before the 3:00 PM appointment! |
| **CHR-02** | Chronological non-inversion rule | Strictly $T_{\text{prep}} < T_{\text{transit}} < T_{\text{appointment}}$ | Verification script checks that timestamp of prep < transit < appointment. Fail immediately if prep/transit occurs in or after appointment block. |

---

### Category D: Diurnal Grounding, Tallies & Solar Anchors

| Test ID | Scenario | Expected Behavior | Verification Check |
|---|---|---|---|
| **SOL-01** | Solar Midday display in UI and Prompt | Solar Midday with exact clock time | - Must show `Solar Midday <HH:MM AM/PM>` (e.g. "Solar Midday 12:47 PM").<br>- Must not label it as plain "Midday" without time. |
| **SOL-02** | Solar Midnight display | Solar Midnight with exact clock time | - Must show `Solar Midnight <HH:MM AM/PM>` (e.g. "Solar Midnight 12:48 AM"). |
| **SOL-03** | Available Thirties baseline tally | "X / Y Available" format | - Ratio must be `available / total_discretionary_baseline`.<br>- Baseline is `total_blocks - default_sleep - default_work`.<br>- Example: 8 total daylight discretionary, 3 occupied $\to$ `"5/8 Available"`, **NEVER** `"5/34 Available"` or `"5/48 Available"`. |
| **SOL-04** | Elapsed block exclusion | No past block recommendations | - When simulated current time is 03:15 PM (Block 17), blocks 1–17 must be partitioned into "Elapsed", and suggestions must only pick from Blocks 18+. |

---

### Category E: Multi-Turn Consistency & Anti-Hallucination Grounding

| Test ID | Scenario | Verification Invariant |
|---|---|---|
| **GRD-01** | Model hallucinates wrong block numbers in raw prose | Grounding override in `ConversationManager.send_user_message` replaces hallucinated text with deterministic tool output for mutations (`reinstate_work_blocks`, `clear_blocks`, `set_sleep_blocks`). |
| **GRD-02** | Multi-turn sequence: Set sleep $\to$ Set work $\to$ Allocate task $\to$ Ask status | Plan state after turn 4 reflects all previous 3 operations without resetting or corrupting earlier blocks. |

---

### Category F: UI & Interaction Verification

| Test ID | Feature | Verification Standard |
|---|---|---|
| **UI-01** | **"Copy Whole Chat" Button** | - Header contains button with icon `edit-copy-symbolic`.<br>- Clicking extracts all turns formatted as `User:
...

Assistant:
...
`.<br>- Sets clipboard via `Gdk.Clipboard`.<br>- Temporarily updates button icon to `object-select-symbolic` ("Transcript copied!") for 1.5s before reverting. |
| **UI-02** | **Chat Panel Autoscroll** | - Uses `Gtk.Viewport.scroll_to(descendant)` on message insertion.<br>- Passes single descendant argument (compatible with PyGObject GTK 4).<br>- Does not trap user scroll adjustment or freeze scroll position. |
| **UI-03** | **Card Stacking / Grouping** | - Consecutive `BlockKind.WORK` blocks stack into a single collapsible card.<br>- Consecutive `BlockKind.SLEEP` blocks stack into a single collapsible card.<br>- Expanding the card displays any preserved tasks or sub-blocks. |

---

## 4. Test Runner Implementation Guide (`tests/run_eval_harness.py`)

The new agent session should implement `tests/run_eval_harness.py` with the following structure:

```python
"""Headless Evaluation Harness for Thirties Planning Assistant."""

import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import List, Dict, Any

from thirties_core.config import ThirtiesConfig
from thirties_core.calendar_engine import MockCalendarEngine
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine
from thirties_core.scheduler import DeterministicScheduler, StateDatabase
from thirties_core.conversation import ConversationManager
from thirties_core.models import BlockKind, DayPlan

class EvalReportGenerator:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.results: List[Dict[str, Any]] = []

    def log_result(self, test_id: str, name: str, category: str, passed: bool, details: str):
        self.results.append({
            "id": test_id,
            "name": name,
            "category": category,
            "passed": passed,
            "details": details,
        })

    def write_markdown_report(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"eval_report_{timestamp}.md"
        # Generate detailed Markdown with pass/fail breakdown, summary metrics, and failed assertion diffs
        ...
        return report_path
```

---

## 5. Output Report Requirements (`docs/eval_reports/`)

The resulting evaluation report markdown file **must** adhere to this format:

1. **Header & Metadata**: Execution date, commit SHA, model runner configuration, total scenarios executed.
2. **Executive Scorecard Table**:
   - Total Scenarios
   - Passed / Failed / Pass Rate (%)
   - Breakdown by category (Envelopes, Tasks, Arithmetic, Chronology, Grounding, UI).
3. **Failure Analysis Section**:
   - For every failed scenario: Test ID, Exact Input, Model Raw Output, Tool Execution, Expected State vs. Actual State diff.
4. **Actionable Recommendations**: Clear, prioritized technical fixes for the primary agent session.

---

## 6. Instructions for the New Agent Session

When starting the new agent session, provide the following prompt:

> *"Please read `docs/EVAL_HARNESS_SPEC.md`. Build and execute the headless evaluation harness in `tests/run_eval_harness.py` across all defined scenario categories (ENV, TSK, CHR, SOL, GRD, UI). Generate the comprehensive evaluation report into `docs/eval_reports/eval_report_<timestamp>.md`. Provide a summary of the report results and highlight any failing invariants."*
