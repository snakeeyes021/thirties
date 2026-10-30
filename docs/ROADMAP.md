# Thirties Roadmap

> The single, ordered backlog for the whole project. `AGENTS.md` defines the mission and the invariants; this file defines the order of work. If the two ever disagree, `AGENTS.md` wins and this file gets fixed.
>
> Last revised: 2026-10-30. Releases: 0.0.1 (2026-10-18), 0.1.0 (2026-10-30). In progress: 0.2 on `feature/0.2`.

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

## 0.1: The day list (agreed 2026-10-17; released as 0.1.0 on 2026-10-30)

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
- ~~Measure the day sim's noise, then the full 106-day run~~ (done 2026-10-23, docs/SLM_BRAIN.md).
- The fixes from the first week of use (A below): done 2026-10-24, checked on 8 paired Gemini days.
- The user uses the 2026-10-24 build for a few days and says it's good; then 0.1.0 is released (AGENTS.md §6.3).
- **Soak, Oct 24–27** (AGENTS.md §6.3). The first build was `0d6ecca` (clock from Sat Oct 24, 8:20 PM). Blocker 4 reset it: the build is now `eed92ae` (the fix is `747de89`), the clock restarted Sat Oct 24 at 9:44 PM, and the evaluation is Tue Oct 27 from 9:45 PM. Frictions as reported, with the proposed tag:
  1. **Polish (M):** calendar events aren't plan items you can tick off. An event is something you'd mark done, but Thirties treats it like a reminder: `log_outcome` refuses calendar events, and the Day page gives them no ✓. Make them plan items with an outcome, without changing the calendar.
  2. **Polish (S):** the chat in the app shows no times on messages. Every message now has a time stored, so show it. Related: a user message is stamped when the reply is saved, not when it's sent, so it should be stamped on arrival.
  3. **Polish (S):** ticking items done before their time leaves a confusing card in the schedule (10-24: two 9:00 PM items ticked at 8:06). The record is right: both are done, with an estimated 7:36 – 8:06 and the 9:00 plan kept. The display isn't. (a) Every expanded row shows "Planned", because the badge ignores status; this was already in 0.0.1. (b) Each row shows the joined title, "A + B". (c) The guessed 7:36 – 8:06 straddles two thirties. Thirties picked that time itself, so by §7.1 it should round to a thirty, and then it would show as one card. Also, both items got the same half hour; they could go back to back instead.
  4. **Blocker (agreed; fixed 10-24 in `747de89`):** an item that is added to a list and scheduled in the same turn is never linked, so ticking it done leaves the Joplin item open (heavy principle 2). This happened twice on 10-24: "Tell Claude re: Torquio machine ID" and "…missed events visibility". In each case the model called `add_task` (to Assorted Quickies) and `add_event` in parallel in one reply, so `add_event` couldn't carry the task_id that `add_task` returns. Neither item went onto Today either. Prompt rule 9 ("add it first, then schedule it with the task_id") can't be followed with parallel calls, which makes this a core gap rather than a prompt gap. Fix: `add_task` takes an optional time (and `schedule_as`, a short name for the plan) and schedules the item itself, linked and put on Today; rule 9 now says to do both in that one call. (A title guard on `add_event` wouldn't have caught either case: the plan titles differed from the list wording.) Already the case in 0.0.1; it's a blocker because it breaks 0.1's headline, keeping Joplin current.
  - **Watch (check at the review):** unlinked add-and-schedule pairs, meaning `add_task` without a time plus `add_event` without a task_id in one reply. Look in the gate run's Joplin write scores and in real use. If it still happens, Python runs the add first and holds the `add_event` back with the new task_id, asking the model whether the two are the same thing.
  5. **Next (agreed with the user 10-26):** a **drop box** list role ("grab bag"), its own type, for a place to drop things quickly without deciding where they go (e.g. Assorted Quickies in Dev). It can cover one notebook (it feeds that notebook's lists) or all of them. Thirties offers now and then to empty it, proposing a home for each item (one small decision each, which the user confirms), and adds new items straight to a proper list when it's obvious, using the drop box only when it isn't.
  6. **0.2, first item (proposed as a blocker; the user overruled 10-27: they're the only user, and they'd rather have 0.2's fixes sooner than reset the clock): the Today note doesn't match the plan, and a routine is written wrong.** Not investigated yet. (a) A new item (not in Joplin) was scheduled. The assistant asked where it should go, got no answer, and said it would put it on Today To-Do, which was the right call. It isn't there: the reply claimed a write that didn't happen. (b) Today To-Do doesn't hold everything on the plan. (c) The morning routine is scheduled as its separate steps (one thirty, then the shower thirty), but Today got one item, "Morning Routine steps", with no sub-items. (d) Later that item was written back into the Morning Routine note itself, a wrong Joplin write. Wanted: a routine comes onto Today as ONE item titled with the note's name, with each of its items nested underneath. At the least, every routine item goes on Today. Never write Today items back into a routine note. Why it was proposed as a blocker: (a) and (d) are wrong or missing Joplin writes, and (b) breaks 0.1's headline.
- **Release check (2026-10-30):** unit tests green; the gate run is no worse than the milestone on the same 106 days, with no internal text (docs/SLM_BRAIN.md). The "no new blocker in 3 days" check first counted only the frictions reported directly. The full transcripts (Oct 14–30, read 10-30) show blocker-type problems during the soak too, and most of them already occurred on 0.0.1: `CALL redraw()` reached Telegram (10-27); a chat "done" and the app's ticks stored the planned times (10-25); ghost copies of a moved item, with outcomes on the wrong copy (10-27/28). The user chose to release 0.1 and make these, with friction 6, the first fixes of 0.2.
- The SLM brain can redraw ("replan", "just figure it out") and carries the morning flow, but it has no actions yet for `add_to_today`, `set_urgency` or `set_item_facts`; add them once the day sim shows how often they're needed.
- Try the day view and the lists pages for real, and adjust.

---

## From the first week on 0.1 (2026-10-24): fixes in 0.1, then 0.2

The first week of real use on 0.1 (10-20 to 10-24) showed that most daily friction comes from a few gaps in the core, not from the model:
- Joplin and Thirties disagree.
- There is no idea of what kind of item something is.
- Stored times get rewritten.

The model spends its effort on bookkeeping and apologies, where it should be using judgement. The guiding principles behind this section are in `AGENTS.md` §7. Each item below is tagged with its size: **S** (hours), **M** (a day or so), **L** (several days, needs design).

### A. In 0.1: bugs with an obvious right answer (all done 2026-10-24 on `feature/0.1`)
1. **Every Joplin write is exactly linked** (M). An item scheduled from a list carries that item's `task_id`, and so does a Today line. A write happens only through the id; nothing is ever fuzzy-matched. "Dinner" once added *My Dinner with Andre* to Today, and "Kitty litter boxes" ticked "Near vanity boxes". When there's no exact link, the assistant asks.
2. **What's done stays done** (S). Before offering an item (morning, rollover, pressing), check Thirties' own done record as well as Joplin. If Joplin couldn't be written, the item still isn't offered again.
3. **Fresh notes** (M). Re-read Joplin when the day changes and after every write. The window, the background service and the Telegram bridge share one copy. The window never refreshed on a day change, which is how "Zein's text" came back on 10-24.
4. **A failed Joplin change can't block the queue** (S). A permanent error parks that change, and the rest go through. One change failed about 1,500 times and held up everything queued after it while Joplin was closed. Media items are never ticked in Joplin.
5. **New lines never land under a Done heading** (S).
6. **The reply filter deletes real content** (S). Any paragraph containing "call" (for example "Call ENT") was removed as "tool talk", which is how the 10-21 pressing list vanished.
7. **Telegram delivery** (S). Retry failed sends, keep a record of any reply that never reached Telegram and send it later, and log to a file rather than only to the Builder console. Example: on 10-24 at 9:18 a reply reached the app but not Telegram.
8. **Each stored message carries its time, channel and build** (S), so issues can be placed by version and by channel.
9. **Times you report are recorded as you said them** (S). "Done" with no start gives the start as an estimate, marked as a guess, never the planned start. The reported end is kept.
10. **A failed tool can't be reported as done** (S). This is a deterministic check on the reply, not a prompt rule.
11. **Things at the same time don't knock each other out** (M). Adding something to a busy block never deletes the item there, and never deletes Work or Sleep. Sliding things later stops at bedtime (partly fixed 10-23).
12. **Start-of-block notices** (S). They go into the conversation history, under a lock against the Telegram thread. Repeats are blocked by item and time, not by internal id, which changes when the plan is re-made.
13. **The time model** (M), as agreed 2026-10-24:
    - Events keep their real times, and blocks are only how they're shown.
    - Times are rounded only when Thirties picks a time itself.
    - A schedule is shown in blocks. Asking about one item gives its real time plus its block ("12:15, in the 12:00 block").
    - The reply filter turns a stray time into block wording ("in the 12:00 block"), never into a different clock time. `says_minutes` goes.
    - **Fixed or flexible:**
      - Fixed by default: calendar events, appointments, flights, rides, concerts, shows, anything set by someone else's clock, and any daily item at a set time (the cats' dinner at 5).
      - Flexible by default: everything else.
      - The model judges the unclear cases, and the item stores which it is.
    - A flexible item that starts mid-block and crosses into the next block moves to the next block, to discourage overstuffing. A fixed item never moves.

### B. 0.2: design settled enough to build (the details; the order of work is in "0.2" below)
1. **Joplin stays clean, and Thirties does the upkeep** (M). The user never has to manage two places.
   - Thirties adds items to the right list, in the right section.
   - Thirties records done items and moves them out of their lists into its Done log (decided below).
   - **Routine notes are templates**, never moved. Ongoing items stay on their lists and get a "worked on" line in the log.
   - **Every Joplin write is recorded** in Thirties' database: before and after, plus the conversation turn that caused it.
   - **Evals score those writes** as part of the grade. The day sim gets a Joplin fake whose writes are compared with the script's truth.
   - **The write log gets audited** against the conversations in real use until it's trustworthy.
2. **What kind of item** (M):
   - **once:** done means finished.
   - **ongoing:** done means "worked on it", and the item stays open until "finished".
   - **daily:** done means done today.

   Python reads the kind from the list's role, with a per-item override. The model only says "done" or "worked on it". Rollover, statistics and the Done log all follow from the kind. A skipped daily item is not "put off".
3. **Routines and daily anchors** (L):
   - **A routine is a template with a trigger:** daily, on some days (work days or days off), or on request. A packing list, for example, happens only when asked and is placed where asked.
   - **Each item in a routine has a placement:**
     - **fixed time:** the cats' dinner at 5, every day;
     - **a usual spot with a fallback:** the morning routine belongs in the morning; if it's missed, after work on work days. Picking the fallback is the model's judgement, from the open slots Python lists;
     - **wherever asked.**
   - **Daily anchors that aren't on any list** (meals) are offered once as defaults and kept in the profile.
   - Every morning Python puts the day's routines and anchors on the plan. They never roll over, so the morning message never asks about them.
   - These facts live in Thirties' item facts, not in the notes.
4. **Things go moot** (S, after B3). Each item gets a "moot after" point:
   - a meal is moot at the next meal;
   - a daily item is moot at the end of the day;
   - a fixed item is moot once its time has passed.

   Moot items are never offered, rolled over or asked about. They're stored as "unknown", not "skipped". "You didn't mark lunch done; roll it over?" can't happen.
5. **"Remind me" schedules an item** (S). It goes on the list and the plan, and may carry a notification. A notification on its own is only for an explicit "ping me at 3", and it's called a ping or an alarm, not a reminder.
6. **Actual wake, sleep and work times** (M). Thirties asks with the morning routine ("when did you start work?") and at the end of work ("done with work?"). Sleep and wake are asked for now; a fitness tracker can supply them later. The rollover and statistics use the actual times.
7. **Context with each outcome** (S). When an outcome is logged, store:
   - how full the day was;
   - the gaps before and after;
   - the nearby events and their kinds;
   - the time since waking.

   Capture this at the moment it's logged, because the plan changes later. The analysis comes later.
8. **Placement with judgement** (L). For a new item, Python lists the candidate slots with their context: right after a stretch, in the middle of a big gap, at its end, or in a later gap. The model picks with a reason, using the item's kind and what's nearby. The current behaviour, "the next free block", piles things on.

### C. Later: design conversations (big; not before B1–B2 are designed)
1. **The views, under natural units.** Today the Day view and the Timeline overlap. Decide what each view is for across the five domains (tasks, schedule, calendar, tracking, planning), and how each shows sizes and positions you can feel rather than clock times.
2. **Planning across horizons.** Week, month and longer goals, broken down into days, with "how on track are we" at each level. Thirties currently plans no further than today.
3. **Helpers for Gemma.** Split each message into small steps with helpers (for example, count and separate the requests first), so the small model never one-shots a whole turn. This goes together with the model spike (After 0.1, item 3).
4. **The bar for the Frontier brain.** A frontier model handed the raw Joplin backlog can plan a day better than our harness does today. The harness should beat that baseline, so measure it as a reference.

### D. Housekeeping tied to the above
- **Memory notes that paper over bugs get deleted once their bug is fixed.** Six of eleven qualify (the list is in the 10-24 review). One was never right: shower things go after work only when the morning was missed. Keep the five real preferences.
- **The profile's "Near-term goals"** are out of date (the Redfox merge is done), which feeds stale offers. A future fix: goals are dated, and they're checked during weekly planning (C2).
- **Contradictions found:**
  - Frontier rule 9 ("don't offer meals or routines") against B3.
  - The memory note "items don't cross block boundaries" against A13.
  - Routine items were copied into Today on 10-21, against §2 of 0.1 ("routines stay in their own notes").

  Each is resolved by the item that replaces it.

### Decided
- **Where done items live in Joplin** (2026-10-24): a single Thirties notebook with one Done note per month. Each done item is one dated line naming the list it came from. Items move out of their lists into it, and undo moves them back. There are no per-list or per-notebook Done sections, because more places means more ways to fall out of step. Routine items stay in place (they're templates). Ongoing items stay too, with a "worked on" line in the log.

---

## 0.2: Trustworthy data, and a day that sets itself up (agreed 2026-10-30; branch `feature/0.2`)

The scope is everything from the 0.1 soak and the Oct 14–30 transcripts, plus section B above. The user chose to have all of it in 0.2 rather than split it with 0.3. The full friction list, with quotes, is in `docs/eval_reports/frictions/2026-10-30_oct14-30.md` (gitignored, because it's private chat). Codes like J1 and S3 refer to it. Each block lands on the branch as it's done, so the user's builds get the fixes as they come.

**Block 0: the data is right** (blockers, first; all six done 2026-10-30)
1. **Today matches the plan, and routines are written right** (soak 6, J1, J2, S9). Everything on today's plan is on Today. A routine goes on as one item, named after its note, with its steps nested under it. Nothing is written back into a routine note.
2. **Completion times** (J8, J9). A "done" in chat with no time is stored at the time it was said, as an estimate (start = that time minus the planned length). A tick in the app is stored at the time of the tick. Notices and replies see ticks.
3. **Ghost copies and outcomes on the wrong item** (S3, S8). Moving or redrawing an item moves it; it never leaves a copy behind. An outcome is written only to the item the user named, on the day they meant.
4. **Internal text never reaches the user** (A1). Python strips `CALL …` lines and any tool syntax before a reply is sent. Retry prompts ("[System check…]") are kept out of the visible history.
5. **Check the possible recurrences** in the database: Work moved on 10-27 (S1), and done items offered again on 10-25 (J3). Fix whichever is real.
6. **Every Joplin write is recorded and scored** (B1). Every write is logged (`joplin_writes`: the operation, the lines it took out and put in, and the message that caused it), and the day sim's Joplin facts check what must hold: nothing written into a routine, everything on the plan is on Today, what's done is ticked, and no line appears twice.

**Built 2026-10-30 (items 7–29, horizons C2, Gemma C3 and After 0.1 items 2, 5–9), in parallel streams merged on `feature/0.2`:** A1 kinds, lasting facts, moot, rollover; A2 routines, anchors, the day proposal, weekly patterns; B lists, drop box, Done note, remind me, pencilled in, later days, note edits, triage; C the overlap model, placement, push back, times; D missed items, schedule replies, bursts, retries, Telegram, mirroring, chat times, contextual questions; E actual times, outcome context, calendar choices, goals, memory review, connectors, housekeeping, the Frontier bar; F Gemma parity, step-by-step parsing, model-neutral runtime; G planning horizons. Still to do: stream I (loose ends), the timeline views (design agreed in principle 10-30), the local model spike (stream H), and the full evals.

**Block 1: the day sets itself up**
7. **What kind of item** (B2): once, ongoing or daily.
8. **Routines and daily anchors** (B3, R1, R2, R6). The user's two morning thirties go on every day without being asked: kitties, caffeinate, water and breakfast in one; throne, shower, PT and brush teeth in the other. Fixed times are kept (cats' dinner at 5), and known facts are applied (dinner takes an hour; outside things wait until after the shower). **Routines repeat on their own schedule** (added by the user 10-30): every day, every other day, every 12 days, some weekdays, or on request. The schedule is stored with the routine, and Python counts from the last time it was done, not from a fixed date. **Skipping one** asks, like a subscription would: skip for today (it comes back tomorrow), skip this time (the next one is a full cycle later), or skip until a day you pick. The same three choices appear as buttons in the app and as a short question in chat.
9. **Things go moot** (B4, R5).
10. **The morning rollover offers only what should roll over** (R4). No routine items, no generic "dev" blocks, nothing done or moot, and a plain question.
11. **The assistant proposes the day** (A6). It covers the daily items plus what keeps goals on track, so the user doesn't have to micromanage.

**Block 2: lists and items**
12. **New items go on the right list and in the right section, without asking** (B1, J5). The **drop box** list role (soak 5) is a fallback for when it isn't obvious. Done items move to the monthly Done note.
13. **"Remind me" schedules an item** (B5, C4).
14. **"Pencilled in" items** (O3): on Today, but not given a time.
15. **Planning tomorrow and later days** (O2).
16. **Editing a note's structure** (J7): sub-items, and splitting a line.

**Block 3: the schedule model**
17. **Overlapping, nested and coincident items** (S2, T4): one coherent model. Items placed alongside never move, split or hide each other; Work shows under what's placed on top of it; a group reads as one entry.
18. **Placement with judgement** (B8, S5): no cramming; "later" means a real gap.
19. **"Push everything back" includes what's past and unmarked** (S4).
20. **Times** (S6, S7). "Now" is the real time. Thirties never picks minute times or a time that crosses blocks. Calendar events can be ticked (soak 1). Ticking items before their time leaves a clean card (soak 3).

**Block 4: messages**
21. **Missed items** (C2): shown with the "you're in this block" line, one per line, without duplicates, and not for the current block. **No "did you get to the rest?" after every done** (C5). **Late notices are dropped**, not sent stale (C3).
22. **Schedule replies** (A5): from now on, with no "(until…)" when the next item follows. Grouped items read as one entry.
23. **Act, don't ask** (A3). Don't make up explanations or capabilities (A4). Reply once to a burst of quick messages (A8).
24. **Telegram** (T1, T2, T3): timeouts retried automatically; formatting escaped; the desktop chat mirrored to the phone.
25. **Times on chat messages in the app** (soak 2).

**Block 5: records and the rest**
26. **Actual wake, sleep and work times** (B6). **Context with each outcome** (B7).
27. **Calendar** (O1): "not mine" per recurring series; mine / show only / ignore, per calendar.
28. **Stale goals** (A7), and **memory notes that paper over fixed bugs get deleted** (D).
29. **The eval counts only minute times Thirties picked itself** (gate write-up, docs/SLM_BRAIN.md).

## Soon, not today (from the user, 2026-10-30)
- **Benchmark local models on other devices:** the user's 8 GB GPU desktop (a likely home-base machine, not set up yet) and their phone (needs the mobile app). The probe is `tests/model_spike.py` (0.2 stream H). Models that don't fit in VRAM or RAM are still candidates: some users will take spilling to RAM or disk, and the wait, in exchange for a smarter model.

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
