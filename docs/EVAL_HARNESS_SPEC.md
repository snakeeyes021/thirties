# Thirties AI Planning Assistant: Agentic Dynamic Stress-Testing Specification

**Document:** `docs/EVAL_HARNESS_SPEC.md`  
**Purpose:** Instructions for a dedicated evaluation agent session to build and execute a large-scale, dynamic, multi-turn user simulation test harness against the Thirties Planning Assistant.

---

## 1. Vision & Core Philosophy

The Thirties Planning Assistant pairs a local on-device SLM with a deterministic scheduling engine (`DayPlan`, `DeterministicScheduler`). 

Traditional software testing relies on rigid unit test assertions. However, a personal scheduling assistant must handle **messy, chaotic, contradictory human beings**. Real users do not speak in clean tool-call syntax; they:
- Ramble and change their minds mid-sentence.
- Misread calendars and backtrack: *"Wait, shoot, I looked at the wrong day, I actually don't have that meeting."*
- Give contradictory instructions: *"I need 2 hours of composing between 2:00 PM and 3:00 PM."* (An impossible request that requires clarifying feedback rather than blind compliance).
- Use vague, idiomatic time: *"crack of dawn"*, *"after the kids go to bed"*, *"sometime before my 3pm call"*.
- Chain multiple unrelated diurnal adjustments into a single breath: *"Going to sleep late tonight at 11, working normal 8:30-4, and can you find me a chunk for a walk in the daylight?"*

### The Anti-Pattern: Regex Whack-a-Mole
We do **not** want an ever-expanding spiderweb of bespoke regexes matching specific English phrases (e.g., regex for "bedtime is X", regex for "going to sleep at X", regex for "wake up at Y"). 

Instead, the Assistant must rely on a **small, powerful, orthogonal set of generic primitives**:
1. `inspect_schedule`: Check current commitments, envelopes, available discretionary blocks, and solar anchors.
2. `set_schedule_window`: Define or shift diurnal envelopes (`WORK`, `SLEEP`) with start/end time or block spans, automatically managing locked states and preserving contained tasks.
3. `allocate_blocks`: Place tasks with specified labels into block ranges or search for best-fit open slots.
4. `clear_blocks`: Deallocate tasks or clear entire envelopes back to open discretionary time.

The evaluation harness evaluates how effectively the assistant understands natural messy human intent and maps it to these generic primitives.

---

## 2. Architecture of the Dynamic Evaluation Harness (`tests/run_eval_harness.py`)

The new agent session will implement `tests/run_eval_harness.py` to run **hundreds of multi-turn simulated conversations**.

### 2.1 The Two-Agent Simulator Pattern
```
                     +---------------------------------------+
                     |         Synthetic User Agent          |
                     |  (Personas: Busy, Scatterbrained,    |
                     |   Contradictory, Vague, Demanding)   |
                     +---------------------------------------+
                                        |  (Messy Human Text)
                                        v
                     +---------------------------------------+
                     |      Thirties Planning Assistant      |
                     |  (ConversationManager + State DB)     |
                     +---------------------------------------+
                                        |  (Reply + DB Mutations)
                                        v
                     +---------------------------------------+
                     |         Independent Evaluator         |
                     |  (Scores Comprehension, Correctness,  |
                     |   Truthfulness, Human Legibility)     |
                     +---------------------------------------+
```

1. **Synthetic User Generator**: Generates varied conversational turns across diverse personas (detailed in Section 3). It can simulate conversations ranging from a quick 2-turn clarification to a winding 15-turn scheduling marathon.
2. **System Under Test**: `thirties_core.conversation.ConversationManager` running against an isolated `DeterministicScheduler` with a fresh temporary SQLite database for each scenario.
3. **Turn-by-Turn & End-of-Dialogue Evaluator**:
   Inspects both the conversational dialogue and the ground-truth database state (`day_plan.blocks`) after every turn.

---

## 3. Persona & Scenario Taxonomy

The evaluation harness must sample across the following conversational archetypes:

### Persona 1: The Scatterbrained Backtracker
- **Behavior:** Starts with one plan, interrupts themselves, changes details, realizes errors.
- **Example Flow:**
  - Turn 1: *"Let's put 2 hours of Dorico writing at 1pm."*
  - Turn 2: *"Oh wait, no, 1pm is right when my sister calls. Make it 3pm instead."*
  - Turn 3: *"Shoot, actually work goes until 4 today. Can we just do it after dinner?"*
- **Evaluation Criteria:** Did the assistant clean up previous allocations without leaving phantom orphan blocks in the database? Does the final schedule match the last agreed state?

### Persona 2: The Contradictory / Impossible Requestor
- **Behavior:** Requests durations longer than available windows, or schedules conflicts over locked commitments.
- **Example Flow:**
  - Turn 1: *"I need 2 hours of gym time between 1:00 PM and 2:00 PM."*
- **Evaluation Criteria:** Did the assistant politely explain the physical impossibility (2 hours = 4 chunks; the 1:00–2:00 PM window is only 2 chunks) and offer viable alternatives, rather than silently truncating or overflowing into 3:00 PM?

### Persona 3: The Holistic Diurnal Shifter
- **Behavior:** Simultaneously adjusts sleep, work, and personal commitments in conversational flow.
- **Example Flow:**
  - Turn 1: *"I'm feeling under the weather. Going to sleep early at 9pm tonight, waking up at 7am, working a half day from 9 to 1, and I just want the afternoon wide open to rest."*
- **Evaluation Criteria:**
  - Sleep blocks correctly set from 9:00 PM to 7:00 AM (`BlockKind.SLEEP`, locked, grouped).
  - Work blocks set from 9:00 AM to 1:00 PM (`BlockKind.WORK`, locked, grouped).
  - Afternoon blocks (1:00 PM to 9:00 PM) restored to open discretionary daylight/dark blocks.
  - Available tally accurately reported based on the new baseline.

### Persona 4: The Vague / Natural Language Planner
- **Behavior:** Uses approximate colloquialisms rather than block numbers or clock times.
- **Example Flow:**
  - Turn 1: *"Where can I squeeze in a quick walk while the sun is still up?"*
  - Turn 2: *"Let's do it right before sunset."*
- **Evaluation Criteria:** Does the assistant inspect solar daylight blocks, identify open daylight blocks before sunset, and allocate accurately?

### Persona 5: The Appointment & Backward Buffer Scheduler
- **Behavior:** Mentions an external appointment and travel time, plus routine tasks.
- **Example Flow:**
  - Turn 1: *"Dentist appointment at 2:30 PM. It takes half an hour to drive there. I haven't showered yet today."*
- **Evaluation Criteria:**
  - Transit buffer placed in the block immediately preceding 2:30 PM (02:00 PM – 02:30 PM).
  - Shower/prep placed before transit (01:30 PM – 02:00 PM).
  - Transit/prep **never** placed during or after the appointment.

---

## 4. Evaluator Scoring Dimensions

For every conversation, the evaluator grades 4 core pillars on a 1–5 scale (and binary Pass/Fail on safety invariants):

### 1. Intent Comprehension (Did the assistant get what the human meant?)
- **5:** Flawlessly extracted core intents, respected nuance, handled corrections smoothly.
- **3:** Understood the main task but missed a secondary constraint (e.g. forgot travel buffer).
- **1:** Completely misunderstood or ignored the user's instruction.

### 2. State & Tool Correctness (Did the database state mutate accurately?)
- **Invariants Checked in SQLite/DayPlan:**
  - Are work blocks set to `BlockKind.WORK` and `is_locked = True`?
  - Are sleep blocks set to `BlockKind.SLEEP` and `is_locked = True`?
  - Did consecutive work/sleep blocks qualify for UI card stacking?
  - Was duration arithmetic exact (e.g. 2 hours = exactly 4 blocks, not 1, not 3)?
  - Were orphan allocations cleaned up when tasks were moved or canceled?

### 3. Truthfulness & Anti-Hallucination Grounding
- **Check:** Does what the assistant *claims* in its prose match the *actual state* in `day_plan.blocks`?
- **Failure:** Assistant says *"I have scheduled your walk from 4:00 PM to 4:30 PM"*, but Block 17 remains empty or was placed at 5:00 PM.

### 4. Human Communication & Legibility
- **Conciseness:** Avoids repeating the entire day plan unsolicited if the user asked a focused question.
- **Natural Phrasing:** Speaks in natural half-hour blocks/chunks. Does **not** awkwardly append `(2 hours)` or verbose robotic math.
- **Tone:** Constructive, proactive, streamlined for quick human decision-making.

---

## 5. Execution & Reporting Requirements

The harness should output reports into `docs/eval_reports/eval_report_<timestamp>.md`.

### Report Sections:
1. **Summary Scorecard**:
   - Total Conversations & Turns Executed.
   - Overall Pass Rate (%) & Mean Scores across Comprehension, State Correctness, Truthfulness, and Legibility.
2. **Breakdown by Persona & Category**:
   - Scatterbrained / Backtracking Pass Rate.
   - Contradictory Constraints Handling Rate.
   - Diurnal Envelope Adjustments (Work/Sleep).
   - Backward Scheduling & Buffer Accuracy.
3. **Detailed Failure Logs**:
   - For every conversation with a score $< 4$ or an invariant violation:
     - Transcript of the conversation.
     - Internal tool calls and execution logs.
     - Database state diff (Expected vs. Actual blocks).
     - Specific failure reason (e.g., "Hallucinated confirmation without DB mutation", "Duration arithmetic off by 2 blocks", "Overwrote locked commitment").
4. **Actionable Recommendations**:
   - Insights on prompt vulnerabilities, missing generic tool primitives, or engine bugs discovered during the run.

---

## 6. How the New Agent Session Should Proceed

When starting the new agent session, the prompt to give it is:

> *"Please review `docs/EVAL_HARNESS_SPEC.md`. Your task is to build and run the dynamic evaluation harness in `tests/run_eval_harness.py`. Generate dozens to hundreds of varied, multi-turn, messy human conversations across all personas described in the spec, evaluate the assistant's responses and database mutations, and write the comprehensive evaluation report to `docs/eval_reports/eval_report_<timestamp>.md`. Finally, summarize the findings and key failure modes."*
