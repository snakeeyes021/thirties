# Thirties Roadmap

> The single, ordered backlog for the whole project. `AGENTS.md` defines the mission and the invariants; this file defines the order of work. If the two ever disagree, `AGENTS.md` wins and this file gets fixed.
>
> Last revised: 2026-10-14 (after Phases 1–2).

---

## Where We Are

- **Frontier Brain works and has been used for real.** The GTK app runs on Gemini 3.8 Flash (via `agy`, or the Gemini API directly), with the full Joplin backlog in context. In its first full day of real use (before 2026-10-14) it was about **75% usable**: morning planning, back-and-forth task picking, and moving things around later in the day all worked. That day ran on the pre-span core; Phases 1–2 below have changed the core and the prompt since.
- **Phases 1–2 are implemented; first live check passed.** Unit tests are green, and the Frontier eval's checks are verified with scripted ideal behaviour. On 2026-10-14 a single live pass, with the free quota split across gemini-3.7-flash and gemini-3.6-flash, scored 10/10 on the 10 scenarios that ran, after relaxing one over-strict check. The other 3 hit API errors (overload, timeout, quota). Still to do: a full run with repeats on gemini-3.8-flash, the model actually used day to day.
- **The Telegram bridge and the Joplin sync note are built but not yet tried in real use.**
- **Nothing records what actually happened.** The app helps make a plan but never learns whether it was followed. `StateDatabase.record_task_deferred` and `record_task_completed` exist but nothing calls them.
- **There is no SLM brain yet.** The LiteRT transport works mechanically. Earlier SLM experiments ran Gemma through essentially the frontier prompt, which does not work.

---

## Phase 0: Restructure (done)

- Merged the practical branch into the trunk. `feat/longterm-slm-architecture` contained only docs, now archived.
- Split `conversation.py` into the shared tool API (`plan_tools.py`) and the `FrontierBrain` (`brains/frontier.py`). Behaviour unchanged, verified by replaying a scripted conversation through the old and new code.
- Consolidated docs: `AGENTS.md`, this roadmap, `docs/SLM_BRAIN.md`, and an archive.

---

## Phase 1: Event Spans on a Solar Canvas (implemented)

- **Canvas:** 48 `ThirtyBlock`s starting at the thirty that contains sunrise and running 24 hours into the next morning, spaced in absolute time (DST-safe). Blocks carry only physical facts. This also fixed a latent bug: the old midnight-to-midnight blocks, shown sunrise-first, gave the "after midnight" thirties timestamps from the *previous* night.
- **Spans:** work, sleep, calendar events, tasks and activities are all `EventSpan`s in real clock time. Occupancy, budgets and the GUI's collapsed cards are derived from them.
- **Store:** spans are stored globally (not per day), so a sleep span crossing midnight belongs to both days. Day metadata and calendar attend/decline decisions have their own tables. Old block-kind snapshots are migrated once on startup.
- **Routines:** default work and sleep from config are created once per day, with deterministic ids and the oldest possible timestamp, so a day off or an edit on any device always wins over a default.
- **Calendar:** events are fetched fresh for both calendar dates the diurnal day touches. Accepted events are firm, and unanswered invites are tentative until the user decides. The old builder dropped pending invites and stopped refreshing the calendar as soon as a day had been saved.
- **Sync:** per-item last-write-wins with deletions synced as tombstones. The old pull overwrote local days with the remote ones regardless of age. Old v1 payloads are still imported, but only for days this device never touched.

## Phase 2: Clock-Time Tools and a Clean Frontier Prompt (implemented)

- **Tools** (`plan_tools.py`): `add_event`, `update_event` (start, end, duration, or a `shift` like "+1 hour"), `remove_event`, `clear_time`, `find_open_time`, `list_schedule`, `resolve_calendar_event`, `finalize_day`. Times are clock strings and durations are phrases; `clock.py` does all parsing and math. Overlaps are refused with a clear explanation, firm calendar items are never overwritten, and activities may sit on top of work or sleep.
- **Grammar:** a single text format for all transports: `CALL tool_name {json arguments}`.
- **Prompt:** no block numbers, no arithmetic lessons, no fixed daylight/dark activity rules. It is rebuilt every turn from the live schedule.
- **Turn loop:** up to 3 rounds. The model sees tool results after lookups, refusals or an empty reply, and a change claimed without any calls triggers one correction.
- **Transports:** the Gemini API retries 429 and 5xx responses, both transports now pass tool results to the model, and empty message parts are dropped (they caused HTTP 400s).

### Open items from Phases 1–2
- **Run the full live Frontier eval on gemini-3.8-flash** (`python3 -m tests.frontier_eval --repeat 2`). It needs about 35 requests per run. The free API tier allows only 20 requests per day per model, and the `agy` installed on the dev machine is too old ("no longer supported"; it needs `agy update` and a fresh login). The first live check also saw the model claim a change without making a call once; the correction step caught it. There is no live baseline from the old core: that attempt hit the quota.
- **Check the `agy` transport flags.** `AntigravityInferenceEngine` passes `--output-format json --model ... --disable-slash-commands`, which the agy 1.x on the dev machine rejects. Confirm what the daily-driver machine's agy accepts.
- **Fix the direct-API default model name.** The config default `gemini-3.8-flash-low` is agy naming. The API engine now splits off the thinking level, but the factory still prefers agy whenever the binary exists, even if it is broken.
- Consider native function calling on the Gemini API path.
- Ask the user whether replies should count time in thirties ("4 chunks"), as the old prompt insisted, or in hours. The new prompt allows either.
- GUI: a work span interrupted by an appointment renders as two cards. Consider one card with the appointment inside.

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
- **Telegram:** connect it and use it for a day (`docs/TELEGRAM_SETUP.md`). Note: on a free Gemini key, ~20 requests/day is only about 10 messages.
- **Connector abstraction (Anti-Pattern 1):** a `TaskSource` interface (Joplin is the first implementation) and a `CalendarSource` interface (EDS first; CalDAV/Google later). Put the Joplin sync note behind a `SyncTransport` interface so state sync is not permanently tied to Joplin.
- **Housekeeping:**
  - `pyproject.toml` dependencies are stale: `astral` is unused, and `litert-lm-api` should be optional.
  - `inference.py` hard-codes `~/dev/Thirties` paths when finding the host runner.
  - There are unused imports across `thirties_core`.
  - DST fall-back hour: Python compares same-zone datetimes by wall time, so overlap checks can misjudge spans inside the repeated 1–2 AM hour. Constructors are safe; a full fix is to compare in UTC everywhere.
