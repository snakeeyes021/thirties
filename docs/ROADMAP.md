# Thirties Roadmap

> The single, ordered backlog for the whole project. `AGENTS.md` defines the mission and the invariants; this file defines the order of work. If the two ever disagree, `AGENTS.md` wins and this file gets fixed.
>
> Last revised: 2026-10-15.

---

## Where We Are

Both brains run on one shared core, and both are measured with simulated users: Gemini, through `agy`, role-plays the user with hidden goals across 13 multi-turn scenarios built from real use, then judges the outcome. Raw reports are in `docs/eval_reports/sim/` (gitignored).

| Brain | Goals met | Deterministic checks | Judge: accuracy / understanding / clarity / efficiency / tone (of 5) | Time per reply |
|---|---|---|---|---|
| Frontier (gemini-3.8-flash-low via agy) | **0.98** | 12/13 | 4.5 / 4.3 / 4.9 / 4.5 / 4.8 | ~9 s |
| SLM (Gemma 4 E4B on the laptop's iGPU), round 5 | **0.97** | 12/13 | 4.3 / 4.0 / 4.6 / 4.1 / 4.4 | ~13 s |

The SLM went from 0.39 to 0.89–0.94 on the hardest scenario (the user's real Sunday conversation), and from 0.33 to 1.0 on "work ran long", over five rounds. Each fix came from reading transcripts and the judge's notes. Its one remaining check failure came from a check that was too strict, since relaxed.

The first real day of use (before 2026-10-14) was about 75% usable, on the old core. Everything below has changed since.

---

## Done

### Phase 0: Restructure
- **One trunk.** The practical branch was merged in. The SLM branch held only docs, which are now archived.
- **The core split.** The shared tool API (`plan_tools.py`) is separated from the brains (`brains/`).

### Phase 1: Event spans on a solar canvas
- **The canvas** is 48 thirties from the sunrise thirty into the next morning, DST-safe. Blocks hold only physical facts.
- **Spans.** Work, sleep, calendar events, tasks and activities are all `EventSpan`s, kept in a global store (so spans can cross midnight) with tombstones. Old block-based plans migrate once on startup.
- **Routines** (default work and sleep) apply on configured **work days only**, with deterministic ids, so a day off always wins.
- **Sync** is last-write-wins per item, and covers spans, days, decisions, conversations, profile, memory and energy.

### Phase 2: Clock-time tools and a clean Frontier prompt
- **Tools:**
  - `add_event` (with `alongside`, `push_later`)
  - `update_event` (start, end, duration, shift, `push_later`, `to_day`)
  - `remove_event`, `swap_events`, `clear_time`, `find_open_time`, `list_schedule`, `resolve_calendar_event`
  - Every one takes a `day` argument.
- **One text grammar** for all models: `CALL tool {json}`.
- **The Frontier prompt** has no block numbers, no arithmetic lessons, no fixed daylight/dark rules and no "lock in" step. It is rebuilt every turn.
- **Reply style, for both brains:** answer in the user's terms, with only what the reply needs. If the user counts in chunks, answer in chunks ("three open chunks at 4:00 PM"). A plan is listed by start time ("- 4:00 PM – Feed cat"), with an end time only where it isn't implied (`clock.agenda_lines`). The sim's `fill_afternoon` and `fits_in_chunks` scenarios check this.

### Phase 3: Closing the loop (first version)
- **`log_outcome`** records done, partial or skipped:
  - Reported or estimated times are shown on the schedule; the original plan is kept for statistics.
  - Finished tasks are checked off in Joplin (this needs `[joplin] api_token`).
  - Skips count as deferrals.
  - Moving a done or skipped item to another day keeps the record and plans a fresh copy.
- **`review_day`** lists items with no outcome yet and suggests rollovers. Both brains are told about anything waiting for an outcome.
- **Telegram check-ins:** "did you get to X, or are you still on it?" after a planned item ends.
- **`log_energy`.**

### Phase 4: Learning (first version)
- **`stats.py`** computes:
  - completion rates
  - real versus planned duration per activity (reliable times only)
  - completion by part of the day and by energy level
  - chronically deferred tasks
- **The facts reach both brains** through `get_statistics`, the Frontier prompt, and the SLM's suggestion scoring.
- **Assistant memory** (`remember`/`forget`) and **a user profile** (habits, near- and long-term goals, preferences, observations):
  - Both are shown to the Frontier Brain every turn.
  - You can view and edit both in the desktop app (menu → Profile & Memory).
- **`week_overview`**, plus any-day tools, for planning beyond one day.

### Conversations
- **Stored per diurnal day.** A day reopens its latest conversation, and the desktop app and Telegram share it.
- **Starting fresh:** "New conversation" in the app, `/new` in Telegram. The app also has a picker for the day's conversations.
- **History tools** for both brains: `search_history`, `list_conversations`, `read_conversation`.

### Backlog and connectors
- **Joplin parsing:**
  - keeps headings and nesting
  - **"someday" sections** (Probation and similar) are visible but never pushed
  - stable task ids
  - completion keeps the item's indentation
- **Media wishlists** from Goodreads (RSS), Letterboxd (public pages) and Serializd (its web app's API), with no logins.
- **Google calendars through GNOME Online Accounts:**
  - recurring events expanded
  - real time zones
  - all-day events and birthdays skipped
  - your RSVP decides invites
  - other people's calendars need a decision

### SLM brain (see `docs/SLM_BRAIN.md`)
- **Gemma parses; Python does the rest.** One parse per message, in a long-lived session that reuses the KV cache: about 12 s for the first message and about 3.5 s after that in the benchmark. The context window is 8k tokens, with a safe budget and a fresh session if a call fails.
- **Python handles:**
  - references ("it", "the first X", "the 9 PM one")
  - loose times ("7", "later this morning", "after work")
  - relative placement and sliding, swaps, moves to other days
  - conflict options
  - a scored backlog with Gemma picking the best few
  - dialog acts (yes/no/option N)
  - repairing misfiled fields, retrying actions that depend on later ones
  - guards against invented changes or unasked sleep/work changes
  - plain-word errors
- **A verified voice:** Gemma rewrites short replies; Python keeps every time and title.

### Clients
- **Desktop app:**
  - chat beside the schedule (hideable)
  - multi-thirty items collapsed into cards, with concurrent items shown together
  - the "now" highlight and the sun position update live
  - availability counts only thirties still ahead
  - other days can be viewed and edited, each with its own conversation
- **Telegram bridge:**
  - locked to your chat, with `--check`
  - continues the desktop conversation, with `/new`
  - check-ins and "starts now" notifications

---

## Next

1. **Use it for real days and log friction.** That's still the best input. Turn each friction point into a sim scenario.
2. **SLM understanding and efficiency** (judge scores ~4.0):
   - Decision fatigue: when the user says "you pick" or is low on energy, propose one low-effort plan and apply it on "sure". Tasks need a rough effort estimate for this.
   - Multi-part messages still drop a part now and then. Measure per action with `tests/slm_parse_eval.py`, and add a "did I cover everything in the message?" check in Python (look for nouns from the message that matched no action).
   - Speed: try E2B for parsing, shorter keys in the output, and a lighter context.
3. **Rollover as a batch:** a morning "yesterday's unfinished items" proposal you confirm in one go (the tools exist; this is the flow).
4. **Statistics in planning:** use real durations from history when proposing how long things take, and warn when a plan exceeds the capacity you've actually shown.
5. **Conversation summaries** when a conversation ends, so history search works on summaries instead of raw messages.
6. **Calendar refinements:**
   - remember "not mine" decisions per recurring series, not per occurrence
   - creating events back in Google Calendar (write access through EDS)
7. **Connector abstraction (Anti-Pattern 1):** `TaskSource`, `CalendarSource` and `SyncTransport` interfaces. The user mentioned "a cloud location with much more storage" as another configurable option, probably a sync or storage transport; waiting on the user to say what it is.
8. **Weekly patterns:** different routines per weekday (work hours, sleep).
9. **Housekeeping:**
   - `pyproject.toml` dependencies are stale (`astral` is unused; `litert-lm-api` should be optional)
   - `inference.py` hard-codes `~/dev/Thirties` runner paths
   - unused imports
   - the DST fall-back hour edge case: same-zone comparisons use wall time
   - the desktop app's periodic refresh runs on the UI thread
