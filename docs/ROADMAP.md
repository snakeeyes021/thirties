# Thirties Roadmap

> The single, ordered backlog for the whole project. `AGENTS.md` defines the mission and the invariants; this file defines the order of work. If the two ever disagree, `AGENTS.md` wins and this file gets fixed.
>
> Last revised: 2026-10-14.

---

## Where We Are

- **Frontier Brain works and has been used for real.** The GTK app runs on Gemini 3.8 Flash (via `agy`, or the Gemini API directly), with the full Joplin backlog in context. In its first full day of real use (before 2026-10-14) it was about **75% usable**: morning planning, back-and-forth task picking, and moving things around later in the day all worked.
- **The Telegram bridge and the Joplin sync note are built but not yet tried in real use.**
- **The core still uses container block kinds.** `BlockKind.WORK` and `BlockKind.SLEEP` are block types rather than events placed on the solar canvas (Anti-Pattern 3), and the model reads and writes block numbers.
- **The Frontier prompt still contains things AGENTS.md rules out**: block arithmetic lessons, hard-coded backward-scheduling rules, and fixed "creative work goes in daylight, admin goes in dark" mappings (contradicting §2.3).
- **Nothing records what actually happened.** The app helps make a plan but never learns whether it was followed. `StateDatabase.record_task_deferred` and `record_task_completed` exist but nothing calls them.
- **There is no SLM brain yet.** The LiteRT transport works mechanically. Earlier SLM experiments ran Gemma through essentially the frontier prompt, which does not work.

---

## Phase 0: Restructure (done)

- Merged the practical branch into the trunk. `feat/longterm-slm-architecture` contained only docs, now archived.
- Split `conversation.py` into the shared tool API (`plan_tools.py`) and the `FrontierBrain` (`brains/frontier.py`). Behaviour unchanged, verified by replaying a scripted conversation through the old and new code.
- Consolidated docs: `AGENTS.md`, this roadmap, `docs/SLM_BRAIN.md`, and an archive.

---

## Phase 1: Event Spans on a Solar Canvas (shared core)

**Why:** Anti-Pattern 3. Treating Work and Sleep as block kinds is what makes "oh shoot, it went til 4:30" dangerous: changing work can rewrite other blocks, and sleep can jump. The fix is a data-model fix, and both brains need it.

**Scope:**
- Blocks carry only physical facts: index, clock span, daylight vs dark (twilight flag optional). They never change kind because of user activity.
- Everything the user does is an `EventSpan`: title, start, end, category (work, sleep, appointment, task, routine, ...), source (calendar, Joplin, user), optional task id, firmness (hard calendar commitment vs movable), and status (see Phase 3).
- Block occupancy, daylight and dark budgets, and the GUI's collapsed banners are *derived* from spans, never stored as block state.
- Overlaps are detected in Python. Tools report conflicts back to the brain instead of silently overwriting or skipping blocks.
- Persistence: a spans table in `StateDatabase`, a migration from `day_snapshots`, and an updated sync payload.
- The GTK views render spans; Work and Sleep become collapsible banners as before.

**Done when:** extending work to 4:30 changes exactly one span and nothing else; sleep cannot move unless explicitly changed; no core code branches on `BlockKind.WORK` or `BlockKind.SLEEP`; and the unit tests cover overlap, cross-midnight spans and envelope edits.

---

## Phase 2: Clock-Time Tools and a Clean Frontier Prompt

**Why:** Anti-Pattern 2. The model should never see or produce block numbers or do duration math.

**Scope:**
- Replace the block primitives in `PlanTools` with span tools in clock time, for example: `list_day`, `add_event(title, start, end | duration)`, `update_event(id, start?, end?, shift_minutes?)`, `remove_event(id)`, `find_open_time(duration, after?, before?, daylight?)`, `resolve_calendar_event`, `finalize_day`. Relative edits ("push it back an hour", "make lunch 15 min earlier") are computed in Python from `shift_minutes`.
- Tool results describe outcomes in clock times and durations, and include any conflicts.
- Strip from the Frontier prompt: block numbering, arithmetic lessons, fixed backward-scheduling recipes, and the daylight/dark activity mappings. Keep the user's actual communication preferences (clock times first; check with the user whether chunk counts should stay).
- Prefer native function calling where the transport supports it (Gemini API). Keep the text-directive grammar only where it is needed (`agy` CLI), and make it clock-time only.
- Retire `_detect_discrepancy`'s block-number regexes once replies no longer quote block numbers.

**Done when:** the prompt contains no arithmetic or block-index instructions; a replay of real multi-turn days (Phase 2 eval set below) passes; and the unit tests are green.

---

## Phase 3: Close the Loop: What Actually Happened

**Why:** AGENTS.md §2.2 is the heart of the mission. An assistant that never learns whether the plan happened is just a planner.

**Scope:**
- Every scheduled span gets a status: planned, done, partly done, skipped, or moved.
- Capturing outcomes, with as little effort from the user as possible:
  - in conversation ("finished the taxes around 4"), via a `log_outcome` tool;
  - a checkbox in the GTK app, and `/done` in Telegram;
  - an end-of-day (or next-morning) review the brain starts itself: "Here's what was planned; what happened?"
- Record the reported actual start and end when given. When only a finish time is known, store what was reported and leave inferring the start to Phase 4. Do not invent precision.
- Write completions back to the task source (`JoplinEngine.complete_task` exists).
- **Rollover engine** (deterministic): for each unfinished item, Python proposes a destination (tomorrow, later this week, or back to the backlog, or drop it) from deadlines, deferral count and recurrence. The brain presents the whole batch for one-shot confirmation. The user never reschedules items one by one.
- Increment deferral counts for real, so the "chronically deferred" handling the prompt already has is driven by true data.

**Done when:** yesterday's unfinished items appear the next morning as a single batch proposal, and completion and deferral history is persisted and synced.

---

## Phase 4: Learn from History

**Scope:**
- Statistics computed in Python: actual vs planned duration by task and category, completion rate by time of day, deferral patterns, realistic daily capacity.
- An estimate of actual start time when only a completion time was reported, using that history.
- Statistics are handed to brains as precomputed facts ("composing sessions usually run about 1.5x what you plan"). The model never computes them.
- Proposals use them: realistic durations, and a warning when a plan exceeds demonstrated capacity.

---

## Phase 5: SLM Brain (research track)

See `docs/SLM_BRAIN.md`. The brain itself depends on Phases 1–2 (it needs the clock-time span tools). Eval harness work can start in parallel at any time.

---

## Continuous and Parallel Work

- **Real-use feedback is the top input.** After each day of real use, record what broke or was annoying, and turn it into eval scenarios and roadmap items. *Open question for the user: what was the missing 25% on the first test day?*
- **Frontier eval set:** a small set of replayable multi-turn "real day" scenarios (drawn from real use) that run against the Frontier Brain before prompt or tool changes are merged.
- **Telegram:** connect it and use it for a day.
- **Connector abstraction (Anti-Pattern 1):** a `TaskSource` interface (Joplin is the first implementation) and a `CalendarSource` interface (EDS first; CalDAV/Google later). Put the Joplin sync note behind a `SyncTransport` interface so state sync is not permanently tied to Joplin.
- **Housekeeping:**
  - `pyproject.toml` dependencies are stale: `astral` is unused, and `litert-lm-api` should be optional.
  - `inference.py` hard-codes `~/dev/Thirties` paths when finding the host runner.
  - `models.py` defines `ThirtyBlock.is_open` twice.
  - There are unused imports across `thirties_core`.
