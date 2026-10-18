# Telegram Setup

The Telegram bridge is a temporary mobile client (AGENTS.md, Anti-Pattern 1): a small program on your computer that relays messages between a private Telegram bot and Thirties. Your phone only runs Telegram; the computer does the work, so it must be on and online whenever you want to use the bot.

## 1. Create the bot (phone)

1. In Telegram, open a chat with **@BotFather** (blue checkmark).
2. Send `/newbot`. Give it a display name (e.g. "Thirties"), then a username ending in `bot` (e.g. `matt_thirties_bot`).
3. BotFather replies with a **token** like `123456789:AA...`. Treat it like a password: anyone with it controls the bot.

## 2. Configure (computer)

Edit `~/.config/thirties/config.toml` (template: `config.example.toml`; keep the file `chmod 600`):

- `[telegram] bot_token`: the token from BotFather.
- The model: by default the bridge uses the `agy` CLI, signed in to your Antigravity / Google AI subscription, so there's nothing to set if `agy -p "hi"` works. It runs `agy` sandboxed in an empty directory (`~/.cache/thirties/agy-workspace`). An `[inference] gemini_api_key` is only a fallback; a free key allows about 20 requests per model per day (roughly 10 messages).
- `[general] latitude`, `longitude`, `timezone`: your location, so sunrise and sunset are right.
- Leave `[sync] enabled = false` unless you run Thirties on more than one computer.

Then check everything:

```bash
cd ~/dev/thirties
python3 -m thirties_core.telegram_bridge --check
```

The check makes one real model call to confirm the key works.

## 3. First run and lock-down

```bash
python3 -m thirties_core.telegram_bridge
```

1. On your phone, search for the bot's username and send `/start`.
2. The bot binds itself to your chat and shows your **chat ID**. Any other chat is refused, but only until the bridge restarts.
3. Make that permanent: stop the bridge (Ctrl+C), set `allowed_chat_ids = [<your chat id>]` under `[telegram]`, and start it again.

## 4. Try it

- `/day`: today's schedule. `/now`: what's happening now and time until sleep.
- "work went til 4:30", "put 2 hours of composing at 5pm", "push composing back an hour", "what should I do this evening?"
- The bot continues the same conversation as the desktop app for that day. `/new` starts a fresh one (the desktop app follows it). `/status` shows the backend.
- Tell it what happened ("finished the taxes around 4", "didn't get to the email"): outcomes are recorded, and finished Joplin tasks are checked off if `[joplin] api_token` is set.
- After a planned item ends with no outcome, the bot asks once: "did you get to X, or are you still on it?" (`[assistant] checkins = false` turns that off).
- Every 30 minutes the bot sends a notification when something starts (set `notify_block_transitions = false` to turn that off).

## 5. Keep it running (optional)

```bash
cp contrib/thirties-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now thirties-telegram
journalctl --user -u thirties-telegram -f      # logs
```

User services stop when you log out, unless you run `loginctl enable-linger $USER`.

## Notes

- The GTK app and the bridge share the same local state, so changes made from your phone show up in the desktop app after you reload the day.
- The bot sends your Joplin backlog and schedule to Gemini, the same as the desktop app.
