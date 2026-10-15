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

Steps 1–2 work today (about 75% usable in the first real day). Steps 3–4 are roadmap phases 3–4.

## Running

```bash
python3 -m unittest discover tests        # unit tests
python3 -m tests.frontier_eval            # live-model eval (needs GEMINI_API_KEY)
python3 -m thirties_core.telegram_bridge  # headless Telegram bridge (temporary mobile client)
```

The GTK desktop app is built with meson (GNOME Builder, or `flatpak-builder` with `tech.redfoxlabs.Thirties.json`).

Configuration lives in `~/.config/thirties/config.toml`.
