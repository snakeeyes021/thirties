# Thirties

A personal assistant for your time. Thirties gathers your tasks and calendar into one place, lays the day out as 48 half-hour "thirties" anchored to sunrise and sunset, and lets you plan, re-plan and review the day in plain conversation. The aim is to take the work of *organizing the organizing* off you, the way a good executive assistant would.

- A language model handles the conversation. Deterministic Python handles all time math, schedule state, invariants and integrations.
- Today it runs on Gemini (the **Frontier Brain**). An on-device small model (the **SLM Brain**) is the long-term research goal.
- Connected so far: Joplin (tasks and notes) and GNOME Evolution Data Server (calendar). More connectors are planned.

Read [`AGENTS.md`](./AGENTS.md) for the mission and architecture, and [`docs/ROADMAP.md`](./docs/ROADMAP.md) for what's next.

## What "working" means

Thirties works when a real day runs like this without friction:

1. **Morning:** open the app (or message it). It already knows today's sunrise, sunset, calendar and backlog. Plan the day together through back-and-forth: pick tasks, place events, adjust.
2. **During the day:** when reality changes ("oh shoot, it went til 4:30"), say so. Only the affected things move, and nothing you didn't mention breaks.
3. **Evening, or next morning:** the assistant asks what actually happened, records it, and proposes where unfinished items should go in one batch.
4. **Over weeks:** its suggestions get more realistic because it has learned how long things actually take you.

All four steps are in place as of the MVP (2026-10-16); see `docs/ROADMAP.md`.

## Running

Open Thirties and the setup assistant asks for your location, usual week, assistant
(Gemma runs on your computer; it's downloaded on first use), Joplin, sync, and
optionally your phone (Telegram). Everything can be changed later in Preferences.
With your permission it keeps running in the background after you close it, for
reminders, check-ins and your phone; Ctrl+Q quits it completely.

The desktop app is a Flatpak built with meson (GNOME Builder, or `flatpak-builder`
with `tech.redfoxlabs.Thirties.json`).

For development:

```bash
python3 -m unittest discover tests                         # unit tests
~/.local/share/thirties/venv/bin/python -m tests.sim_eval  # simulated users (see docs/SLM_BRAIN.md §4)
python3 -m thirties_core.telegram_bridge                   # the background service headless, without the app
```

An optional `~/.config/thirties/config.toml` sets developer defaults; settings chosen in the app win over it.
