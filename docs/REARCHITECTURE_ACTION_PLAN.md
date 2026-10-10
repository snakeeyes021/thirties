# Thirties Core: Universal Primitives Rearchitecture Action Plan

**Date:** October 9, 2026  
**Status:** Approved & Ready for Execution  
**Target Branch:** `feat/initial-mvp`  
**Prerequisite Commit:** `93a855d` (Stripped prompt-scraping regexes and response-hijack overrides)

---

## 1. Executive Context & Architectural Objectives

The codebase recently completed Phase 1 of its cleanup (commit `93a855d`), stripping out the brittle shadow regex engines that scraped user prompts behind the model's back, along with deterministic response-hijack overrides that overwrote the assistant's voice with canned strings.

However, an unvarnished audit revealed that the codebase is in a **half-migrated state**:
1. **Directive Parsing Drops Operations**: `inference.py` uses `re.search` instead of `re.finditer`, causing any assistant response with multiple directives (e.g. allocating two distinct blocks or clearing + allocating) to silently drop all operations after the first.
2. **Greedy Regex Corrupts Data**: Key-value extraction in `inference.py` greedily matches labels, turning `label=Composing locked=true` into a label named `'Composing locked=true'`.
3. **`resolve_event` Destroys Unrelated Events**: A logic flaw in `resolve_event` (`e.id != event_id and (event_id != 'unconfirmed')`) evaluates to `False` for all events, wiping out the user's entire list of ambiguous events whenever an unconfirmed event is resolved.
4. **`inspect_blocks` is a Dead End**: `ConversationManager.send_user_message` does not implement an agentic ReAct loop. When the model emits `INSPECT_BLOCKS`, the tool runs, but execution terminates without feeding the result back into the model to complete its turn.
5. **Mutation Invariants are Violated**: `modify_blocks` and `clear_blocks` blindly overwrite locked calendar appointments (`BUSY_CALENDAR`). Furthermore, modifying `WORK` or `SLEEP` sweeps the rest of the day and deletes all other work/sleep blocks, preventing split schedules.
6. **The Test Suite Masks Breakages**: `tests/test_conversation.py` contains zero calls to `modify_blocks` or `resolve_event` (it still exercises legacy shims like `allocate_thirty_block`), and three tests are orphaned after `if __name__ == '__main__':`—one of which fails immediately when executed.
7. **Code Duplication in Eval Harness**: `tests/run_eval_harness.py` duplicates the directive extraction logic of `inference.py` instead of importing it.

This action plan provides a step-by-step backlog with exact specifications so an agent or swarm can execute the complete, holistic rearchitecture without regressions.

---

## 2. Target Architecture Specification

### 2.1 The 5 Universal Schedule Primitives

All schedule modifications must flow through these 5 orthogonal primitives in `ConversationManager`:

| Primitive | Signature | Purpose & Invariants |
| :--- | :--- | :--- |
| `modify_blocks` | `(start_block: int, end_block: Optional[int], kind: Optional[str], label: Optional[str], task_id: Optional[str], is_locked: Optional[bool], force_calendar: bool = False)` | Mutates state across `[start_block, end_block]`. **Invariant:** Must NOT overwrite `BUSY_CALENDAR` or locked blocks unless `force_calendar=True`. Must NOT clear blocks outside `[start_block, end_block]` unless an explicit parameter like `replace_envelope=True` is provided. Preserves diurnal sunlight/dark kinds when setting tasks. |
| `clear_blocks` | `(start_block: Optional[int], end_block: Optional[int], clear_kind: Optional[str] = None)` | Resets targeted blocks back to open discretionary time (`label=''`, `assigned_task_id=None`, `is_locked=False`, `kind=DAYLIGHT_DISCRETIONARY` or `DARK_DISCRETIONARY`). If `clear_kind='WORK'`, clears all work blocks. If `clear_kind='SLEEP'`, clears all sleep blocks. **Invariant:** Never clears `BUSY_CALENDAR` unless explicitly targeted by index with confirmation. |
| `resolve_event` | `(event_id: str, action: str = 'attend')` | Transitions an ambiguous calendar event to `BUSY_CALENDAR` (`action='attend'`) or discretionary (`action='decline'`). **Invariant:** Only updates/removes the target event ID. Never wipes the entire ambiguous list. |
| `inspect_blocks` | `(start_block: int, end_block: Optional[int])` | Pure read primitive. Returns formatted text representing block kinds, times, labels, and lock states. Used by the agentic loop before making planning decisions. |
| `finalize_day_plan` | `(notes: Optional[str])` | Sets `day_plan.is_finalized = True`, attaches notes, and saves snapshot to SQLite database. |

### 2.2 Text Directive Protocol (Model -> Inference Engine)

Because on-device SLMs (Gemma 4-E4B / 4-E2B via LiteRT-LM) use plain-text generation, the model communicates directives on standalone lines:

```text
MODIFY_BLOCKS: <start>[-<end>] | [kind=<WORK|SLEEP|DISCRETIONARY>] [label=<LABEL>] [locked=<true|false>]
CLEAR_BLOCKS: <start>[-<end>] | [kind=<WORK|SLEEP|ALL>]
RESOLVE_EVENT: <event_id> | <attend|decline>
INSPECT_BLOCKS: <start>[-<end>]
FINALIZE_PLAN [| notes=<NOTES>]
```

**Parsing Rules:**
1. Multiple directives on separate lines MUST all be extracted in appearance order.
2. Directives MUST be stripped from the user-visible natural language text.
3. Attribute parsing after `|` must support space-separated or pipe-separated key-values (`key=value`), handling multi-word labels safely without eating trailing attributes.
4. Directives must be case-insensitive on keywords, but preserve label casing.

---

## 3. Work Package Breakdown (Action Items)

### Work Package 1: Inference Engine Directive Parser Rework
**Primary File:** `thirties_core/inference.py`  
**Secondary File:** `tests/run_eval_harness.py`

- [ ] **Task 1.1: Replace Single-Match `re.search` with Multi-Directive Scanner**
  - Implement a structured parser function `parse_model_directives(raw_reply: str) -> tuple[str, list[dict[str, Any]]]`.
  - Scan line-by-line or with `re.finditer` to capture **all** valid directive lines in order.
  - Test case: verify that multiple `MODIFY_BLOCKS` lines (e.g. `MODIFY_BLOCKS: 19-21 | label=Dorico\nMODIFY_BLOCKS: 22 | label=Lunch`) yield 2 distinct tool calls.
- [ ] **Task 1.2: Robust Attribute Lexer for `MODIFY_BLOCKS`**
  - Tokenize the attribute segment after `|`.
  - Recognize keys: `kind=(WORK|SLEEP|DISCRETIONARY)`, `locked=(true|false)`, `task_id=\S+`.
  - Everything else assigned to `label=...` (or unquoted trailing text) must not accidentally ingest subsequent `locked=true` tokens.
- [ ] **Task 1.3: Support `CLEAR_BLOCKS` Syntax Variants**
  - Handle both `CLEAR_BLOCKS: 24-28`, `CLEAR_BLOCKS: WORK`, and `CLEAR_BLOCKS: | kind=WORK`.
  - Map `clear_kind` cleanly to `clear_blocks` arguments without needing legacy `clear_all_work` booleans.
- [ ] **Task 1.4: Update `MockInferenceEngine`**
  - Stop returning legacy `allocate_thirty_block` and `resolve_calendar_event` tool calls in `MockInferenceEngine.chat`.
  - Change default mock responses to return `modify_blocks` and `resolve_event`.
- [ ] **Task 1.5: Harmonize `run_eval_harness.py`**
  - Remove duplicated regex extraction logic inside `FastHybridInferenceEngine`.
  - Import and use `parse_model_directives` directly from `thirties_core.inference`.

---

### Work Package 2: Primitive Semantics & Invariants in `ConversationManager`
**Primary File:** `thirties_core/conversation.py`  
**Secondary File:** `thirties_core/models.py`

- [ ] **Task 2.1: Enforce Orthogonality & Calendar Protection in `modify_blocks`**
  - Before modifying a block, check if `b.kind == BlockKind.BUSY_CALENDAR` or `b.is_locked`. If locked by calendar and `force_calendar` is False, skip or reject with a descriptive warning message.
  - Remove the global loop that auto-deallocates all other `WORK` and `SLEEP` blocks when a range is set to `WORK` or `SLEEP`. If the intent is explicitly to shift the work envelope (e.g. from prompt / command), introduce a parameter `clear_existing_envelope: bool = False`, defaulting to `False`.
- [ ] **Task 2.2: Fix `resolve_event` Ambiguous Event Scoping**
  - Fix line 534:
    ```python
    if event_id in ('unconfirmed', '*'):
        # Target the first ambiguous event or match by block reference
        target_id = self.ambiguous_events[0].id if self.ambiguous_events else None
    else:
        target_id = event_id
    self.ambiguous_events = [e for e in self.ambiguous_events if e.id != target_id]
    ```
  - Ensure resolving one event does not delete unrelated ambiguous events.
- [ ] **Task 2.3: Implement Agentic ReAct Turn for Read-Only Directives (`inspect_blocks`)**
  - In `ConversationManager.send_user_message`:
    If `executed_tools` contains read-only tools like `inspect_blocks`, do NOT terminate the turn immediately.
    Append the tool output to `self.messages`, and perform a follow-up call: `self.inference_engine.chat(self.messages, tools=PLANNING_TOOLS)` (up to a max depth of 2 turns) so the model can inspect and then output its response or mutation directives.
- [ ] **Task 2.4: Clean Up Legacy Tool Shims in `execute_tool`**
  - Keep legacy names (`allocate_thirty_block`, `reinstate_work_blocks`) solely with `@deprecated` log warnings if needed for backwards compatibility, or migrate all internal callers and tests to only dispatch `modify_blocks`, `clear_blocks`, `resolve_event`, `inspect_blocks`, `finalize_day_plan`.
- [ ] **Task 2.5: Synchronize System Prompt & `PLANNING_TOOLS`**
  - Ensure the tools in `PLANNING_TOOLS` match the 5 primitives exactly.
  - Ensure the directive grammar in `_build_system_prompt` matches the parser implemented in Work Package 1.

---

### Work Package 3: Test Suite Modernization & Orphan Reclamation
**Primary Files:** `tests/test_conversation.py`, `tests/test_inference.py`

- [ ] **Task 3.1: Reclaim Orphaned Tests in `test_conversation.py`**
  - Move lines 254–289 back into the `TestConversation(unittest.TestCase)` class body before `if __name__ == '__main__':`.
  - Fix `test_user_message_sets_custom_work_hours`: configure `MockInferenceEngine` or mock chat response to emit `MODIFY_BLOCKS: 1-16 | kind=WORK` so the test exercises directive execution cleanly without prompt-scraping fallbacks.
  - Verify that `test_discretionary_totals` and `test_work_window_moves_clearing_old_work_blocks` pass.
- [ ] **Task 3.2: Modernize `test_conversation.py` Tool Calls**
  - Replace calls to `self.manager.execute_tool('allocate_thirty_block', ...)` with `self.manager.execute_tool('modify_blocks', ...)`.
  - Replace calls to `self.manager.execute_tool('resolve_calendar_event', ...)` with `self.manager.execute_tool('resolve_event', ...)`.
  - Add explicit unit tests for:
    - Direct invocation of `modify_blocks` with single block, multi-block, labels, and work kinds.
    - Direct invocation of `inspect_blocks`.
    - Protection against overwriting `BUSY_CALENDAR` blocks.
- [ ] **Task 3.3: Expand `test_inference.py` Edge Cases**
  - Add tests for:
    - Multiple `MODIFY_BLOCKS` directives in one reply.
    - Combined `MODIFY_BLOCKS` + `CLEAR_BLOCKS` in one reply.
    - Space-separated attributes: `MODIFY_BLOCKS: 19-22 | label=Composing locked=true`.
    - Lowercase directive: `modify_blocks: 10 | label=Break`.
    - Pure conversational reply with 0 directives.
    - Malformed directive handling (graceful fallback without crashing).

---

### Work Package 4: Verification & Evaluation Harness Run
**Primary Files:** `tests/run_eval_harness.py`, test suite

- [ ] **Task 4.1: Run Full Unit Test Suite**
  - Execute `python3 -m unittest discover tests`.
  - Ensure all 52+ tests pass with zero warnings or failures.
- [ ] **Task 4.2: Execute Evaluation Harness**
  - Run `python3 tests/run_eval_harness.py --scenarios 50` (or local dry-run).
  - Verify that tool extraction and qualitative evaluation metrics remain at or above current benchmarks without relying on prompt regex fallbacks.

---

## 4. Acceptance Criteria for Completion

1. **Zero Shadow Regexes**: `conversation.py` and `inference.py` contain no regex inspections of user prompt text to trigger mutations.
2. **Zero Response Hijack Overrides**: Model natural language is preserved; tool output is only used as fallback when the model generates no text.
3. **Multi-Directive Support**: Model can output multiple directives per turn, and all are parsed and executed in sequence.
4. **Calendar Invariant Preserved**: Soft allocations never silently erase `BUSY_CALENDAR` blocks.
5. **No Dead Test Code**: All test methods in `tests/test_conversation.py` reside within `TestConversation` and run under `unittest`.
6. **All Tests Pass**: 100% green test run across the test suite.
