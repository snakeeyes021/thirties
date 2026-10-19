# Thirties Roadmap

> The single, ordered backlog for the whole project. `AGENTS.md` defines the mission and the invariants; this file defines the order of work. If the two ever disagree, `AGENTS.md` wins and this file gets fixed.
>
> Last revised: 2026-10-18.

---

## Where We Are

Both brains run on one shared core, and both are measured with simulated users: Gemini, through `agy`, role-plays the user with hidden goals across 13 multi-turn scenarios built from real use, then judges the outcome. Raw reports are in `docs/eval_reports/sim/` (gitignored).

| Brain | Goals met | Deterministic checks | Judge: accuracy / understanding / clarity / efficiency / tone (of 5) | Time per reply |
|---|---|---|---|---|
| Frontier (gemini-3.8-flash-low via agy) | **0.98** | 12/13 | 4.5 / 4.3 / 4.9 / 4.5 / 4.8 | ~9 s |
| SLM (Gemma 4 E4B on the laptop's iGPU), round 5 | **0.97** | 12/13 | 4.3 / 4.0 / 4.6 / 4.1 / 4.4 | ~13 s |

**Scaled up on 2026-10-15** (see `docs/SLM_BRAIN.md` §4.2): generated scenarios (`tests/sim_generator.py`) and a 546-case parse benchmark. On 90 generated conversations the SLM met 0.88 of goals (63/90 passed every check, and the simulated user gave up 12 times); on 139 the Frontier met 0.98. The hand-written 13 had overstated the SLM.

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

## MVP: released as 0.0.1, an alpha (agreed 2026-10-16; done 2026-10-17)

Done means a generic user can install Thirties fresh and use it daily, with no config files, dev tools or hand-run setup. Work happens on `restructure/one-core-two-brains`, in roughly this order:

1. **Fix Gemma in the Flatpak, the right way:** the LiteRT runtime is bundled in the Flatpak (the wheel is pure-py3 manylinux) and runs inside the sandbox. The model (not gated) is downloaded on first run, with progress. This replaces the host-process workaround.
2. **First-run setup and real Preferences.** No hand-edited TOML: brain; sync storage (a folder the user picks, or a Joplin note); Joplin connection and permission to check items off; Telegram; location (auto-detected, or a city); work days and hours; sleep; media wishlist usernames (profiles must be public). Everything is editable later in Preferences.
3. **Brains:** Gemma is the default. Gemini through agy is a developer option: selectable only when `agy` is installed and signed in, with a one-line note on how to set it up and no walkthrough. The user chooses which Gemini sources it uses and in what order: subscription through agy, API key, or both.
4. **Joplin write-back:** connect through Joplin's local API (Web Clipper service), queue anything that can't be delivered, and retry. With the user's permission, Thirties starts Joplin minimized when it needs it and Joplin isn't running.
5. **Telegram:** setup in the app (BotFather steps, token check, pairing your phone's chat, saved), and the bridge running as the app's background mode through the Background portal (autostart at login, switchable in GNOME Settings).
6. **Messages:** keep the check-in 10 minutes after an item ends; add reminders the user asks for at any time, on any day, and suppression on request.
7. **Chat panel as its own full-height pane** with its own header bar (the libadwaita split-view pattern), holding new conversation, the conversation picker and the brain.
8. **Sync to a user-chosen folder** (Drive via Insync, for example), with the Joplin note as the fallback.
9. **AM/PM only where it could be mistaken:** the first time when it isn't obvious from now, wherever a time crosses noon or midnight relative to the time before it, and on ambiguous bare hours.
10. **Rollover as a batch:** yesterday's unfinished items proposed together, confirmed in one go.
11. **Statistics in planning:** real durations in proposals; a warning when a plan exceeds the capacity you've actually shown.
12. **Conversation summaries** for history search.
13. **The release build, last:** a Flatpak manifest and GitHub Actions workflow like Pianola's (`.github/workflows/flatpak.yml`: tests, a `.flatpak` bundle on every push, an OCI image on GHCR from `main` and tags, and a GitHub Release from a `v*` tag, marked pre-release while the version is 0.x). Then a human PR and merge to `main`, then a fresh eval baseline (both brains, sim/judge on flash-low, within the 66% budget).

**Not yet tried by hand** (anything broken here is tech debt for the branch current when it's found): background permission and autostart, Detect location, pairing a phone by code, starting Joplin when needed, folder sync, and notifications from the installed app.

**Deferred until after the MVP:** contextual check-ins ("have you started?"), a headless Joplin CLI sync peer, calendar refinements (no calendar write-back), per-weekday routines, the connector abstraction beyond the storage transport, further SLM research (E2B only if E4B doesn't fit a device), Flathub, translations, a native mobile app, and housekeeping.

---

## 0.1: The day list (branch `feature/0.1`, agreed 2026-10-17)

Real use of the MVP showed the root problem: the schedule does two jobs, the plan and the record. So every drift became a schedule edit, which brought shuffles, confirmations, nagging, and a day view full of things that never happened. 0.1 separates them.

### Principles
- **The task backend is the source of truth** (Joplin today). Every list lives there, including the day's list, and the user can edit any of it by hand. Thirties is a wrapper around the user's own notes. It stores only what has no home in them:
  - the plan (when things are scheduled)
  - the record (what actually happened, and when)
  - facts about items ("after shower", "not before 3", "needs daylight")
  - each list's role and each item's urgency
  - the link between a Today item and its home list
  - settings and conversations
- **Notes stay plain checklists.** Thirties never writes times, ids or markers into them.
- **The plan changes only when the user asks** (or agrees to a proposal). Nothing is assumed done.

### 1. Lists: a role per note, an urgency per item
Notes are organized by area of life (Home, Errands/Admin, Medical…), and urgency varies within each one, so there are two axes:
- **Role (per note, not per notebook):**

  | Role | Example | What Thirties does with it |
  |---|---|---|
  | routine | Morning Routine | feeds Today each morning, in order |
  | today | Today To-Do | the day's actual list |
  | menu | Self | offers items when there's free time, and expands "pick one item from Home…" into a suggestion |
  | list | Home, Errands/Admin, Medical, Shopping | items to work through |
  | project | Wedding To-Do | a one-off with an end |
  | reference | wishlists, "Do I want?" | never pushed; mentioned only when asked |
  | ignore | | not read |

- **Urgency (per item):** now, soon, whenever, someday, or a date.
  - The guess reads the note's own structure: headings (Active / Probation), blank-line groups, nesting (the leaf "Need to call" is the action; its parents are context), and wording ("for THIS winter").
  - The note's role and area give the default (Medical leans soon, Home leans whenever).
- **Who guesses:**
  - The chosen brain's model guesses, with Gemma as the fallback since it's always installed. Python doesn't attempt this judgment.
  - Roles: one small, constrained choice per note.
  - Urgency: guessed one note at a time, for new or changed items only.
  - Guesses are starting values; the user confirms or corrects them.
  - Each guessing task is also an SLM eval, with Gemini's answer on the same notes as the reference.
- **Setup:**
  - Thirties finds every notebook and note, preselects the relevant ones, and proposes a role for each note.
  - New notes that appear later are asked about once.
- **Profile → Your lists:** every item grouped by notebook and note, with an urgency choice (or a date) to bulk-edit. Urgency can also be corrected in conversation ("that's not urgent").
- **Restructuring is offered, never required.** A user with an empty backend gets a starter set (Today, a routine, a couple of lists). A one-time triage conversation for a monolithic list waits until after 0.1.

### 2. The Today note
- **Morning:** Thirties proposes the day. It includes the routine, asks which of yesterday's unfinished items to keep, and picks by urgency and fit. Once agreed, it writes the list to the Today note.
- **Linked to home lists:** a Today item copied from Errands stays linked to it, so ticking either one ticks both.
- **Edits by hand:**
  - Thirties re-reads the note, and a reworded item counts as a new item.
  - If a scheduled item disappears from the list, the user is told once.
- **Next morning:** declined items leave Today. Items with a home list are still on it. Items without one get a proposed home, as one list: "Clip cat nails → Errands/Admin".
- **Routine notes are unticked each morning** (with permission), so routines start fresh.

### 3. Plan and record
- **Done items:**
  - They grey out at the times they actually happened, like traffic behind you on a map.
  - "Done" with no time given means finished when reported, with the start estimated and marked as a guess.
- **Unreported items:**
  - They stay where they were, marked not reported, and the day view shows a quiet "behind: Shower, PT".
  - Thirties never moves them on its own.
- **Redraw** (a button, or "replan") is one deterministic action, with undo. Python reflows what's left from now, around fixed items and item facts. The model steps in only when things don't fit, with one proposal ("drop the walk, or move cables to tomorrow?").
- **Fixed commitments at risk:** the assistant speaks up once. It doesn't silently change the plan.
- **Facts about items, enforced by Python:**
  - how fixed an item is: fixed (calendar), anchored (meals, routines), or flexible
  - what must come first ("outside things after shower")
  - when it can't start ("not before she leaves")
  - daylight
- **Direct manipulation:** drag items, tap them done. This goes through `PlanTools`, like the assistant.

### 4. Messages
- **Light:** a heads-up before fixed commitments, plus reminders the user asked for.
- **Medium (default):** one message at the start of each stretch of the day ("This afternoon: X, Y, Z") and one check-in at its end ("What did you get to?").
  - Python cuts stretches at fixed commitments, work, sunset and sleep, up to about three hours each.
- **Heavy:** per item, grouped by thirty (the 0.0.1 behaviour).
- **Always:**
  - nothing during sleep or calendar events
  - nothing about items already reported
  - no check-in shortly after the user has talked to the assistant

### 5. The day view
- **Default page:**
  - "Now and next"
  - the rest of today in a compact list
  - earlier items folded into "Done today", a summary of what actually happened and when
- **Second page:** the full 48-thirty timeline.
- Try it, and change it if it doesn't feel right.

### Order of work (first versions of 0–5 landed 2026-10-18)
0. **A day-long sim** (`tests/day_sim.py`). The short sim froze the clock with two to four crisp goals, about 3/10 for realism against a real day. The day sim lives a whole day from a script: the clock runs, the real background service's messages reach the simulated user, hidden constraints come out when the plan breaks them, and a ground truth of what really happened scores the record. Scripts come from real conversations (`tests/day_scripts.py`, kept out of git) and variations of them. Baseline it on 0.0.1 (branch `eval/0.0.1`: v0.0.1 plus the agy fix and the harness), then on 0.1.
1. **List roles and urgency.** `lists.py` stores them (synced); `brains/list_sorter.py` has the chosen model guess (Gemini a note per call, Gemma one choice per call); the background service guesses what's new every half hour; Profile → Lists and a Your Lists setup step for the user's choices; both brains use them; `set_urgency` in conversation.
2. **The Today note** (`today.py`). `add_to_today`, links to home lists (ticking either ticks both), scheduling a list item for today adds it. Mornings: routines unticked (a switch), ticked items cleared from Today, and the rollover covers what's still on Today plus pressing items. Hand edits are caught every two minutes. Routines stay in their own notes rather than being copied into Today each day.
3. **Plan and record.** `redraw` and `undo_redraw` (Python lays out the rest of today, around fixed things and `set_item_facts` facts; what doesn't fit comes off with a question); "not reported" items are never assumed done. Gemini's rules: record drift, don't reshuffle.
4. **Message levels** (off, light, medium by default, heavy), stretches of the day, quiet during sleep and calendar events and while the user is talking.
5. **The day view.** A Day page (Now, Next, Not reported with Done/Skipped, Done today at real times, Replan with Undo) and the Timeline page with drag to move.

Still to do in 0.1:
- Measure the day sim's noise (the same version twice on the same days), then the full 106-day run on the latest commit after the weekly quota resets (Fri Oct 23); check whether automatic sliding hurts plan respect (docs/SLM_BRAIN.md).
- The SLM brain can redraw ("replan", "just figure it out") and carries the morning flow, but it has no actions yet for `add_to_today`, `set_urgency` or `set_item_facts`; add them once the day sim shows how often they're needed.
- Try the day view and the lists pages for real, and adjust.

---

## After 0.1

1. **Use it for real days and log friction.** That's still the best input. Turn each friction point into a sim scenario.
2. **SLM understanding and efficiency** (judge: understanding 3.4, efficiency 3.4 on generated scenarios):
   - Decision fatigue: when the user says "you pick" or is low on energy, propose one low-effort plan and apply it on "sure". Tasks need a rough effort estimate for this.
   - Multi-part messages drop a part (50% on the parse benchmark: "work ran until 4:30, and move budget review to 4:30" loses the work change). Add a "did I cover everything in the message?" check in Python: look for nouns from the message that matched no action.
   - "Dinner, cooking and eating, listening to X" becomes three items; it should be one item with X alongside.
   - Speed: try E2B for parsing, shorter keys in the output, and a lighter context.
3. **Spike: which on-device model.** Compare Gemma 4 E4B with other small models that fit the same devices: Phi-4 Mini (3.8B) and Qwen (3–4B, and 7–9B where memory allows), on the same tasks: the parse benchmark (`tests/slm_parse_eval.py`), list sorting against Gemini's answers, and the day sim. Measure accuracy, latency and memory, and check the licence. First, put the brain behind a model-neutral runtime: `json_call`, `text_call` and the parser session are all it uses. Then check which models LiteRT-LM can run; llama.cpp (GGUF, Vulkan, JSON-schema grammars) is the fallback. Keep Gemma's own strengths in view: it was built for on-device use, it takes audio (for voice later), and it already runs on the GPU in the Flatpak.
4. **Contextual messages** ("have you started X?", "are you on your way?") only for items that matter, chosen by the app. Don't build this until the rule is clear.
5. **Calendar refinements:**
   - remember "not mine" decisions per recurring series, not per occurrence
   - choose per calendar: mine, show only, or ignore
   - (not planned: writing events back to Google Calendar)
6. **Connector abstraction (Anti-Pattern 1):** `TaskSource` and `CalendarSource` interfaces, beside the existing `SyncTransport`.
7. **Weekly patterns:** different routines per weekday (work hours, sleep).
8. **Triage for monolithic lists:** a one-time conversation that proposes splitting a list sorted by time horizon into lists by area of life.
9. **Housekeeping:**
   - `pyproject.toml` dependencies are stale (`astral` is unused; `litert-lm-api` should be optional)
   - `inference.py` hard-codes `~/dev/Thirties` runner paths
   - unused imports
   - the DST fall-back hour edge case: same-zone comparisons use wall time
   - the desktop app's periodic refresh runs on the UI thread
   - GUI tests in CI (they skip there: no display)
10. **Later:** a headless Joplin CLI sync peer, Flathub, translations, a native mobile app.
