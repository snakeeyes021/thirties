"""Headless Telegram Bot Bridge for Thirties.

Enables two-way conversational planning with Gemini 3.8 Flash via Telegram
and proactive lock-screen push notifications ("buzzes") at 30-minute block transitions.

Uses zero external dependencies (pure Python standard library urllib.request).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from thirties_core.astronomy import get_diurnal_date
from thirties_core.config import ThirtiesConfig, load_config
from thirties_core.astronomy import get_current_time
from thirties_core.backlog import load_backlog
from thirties_core.conversation import ConversationManager
from thirties_core.inference import InferenceEngine, get_inference_engine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.clock import format_clock, format_duration
from thirties_core.models import SpanCategory, ThirtyBlock
from thirties_core.scheduler import DeterministicScheduler
from thirties_core.sync_engine import SyncEngine

logger = logging.getLogger(__name__)


class TelegramClient:
    """Minimal, dependency-free Telegram Bot API client using urllib.request."""

    def __init__(self, bot_token: str, base_url: str = "https://api.telegram.org") -> None:
        self.bot_token = bot_token.strip()
        self.api_url = f"{base_url.rstrip('/')}/bot{self.bot_token}"

    def _request(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        url = f"{self.api_url}/{method}"
        headers = {"User-Agent": "Thirties-Telegram-Bridge/1.0"}
        data = None

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    def get_me(self) -> Dict[str, Any]:
        """Verify bot token and return bot information."""
        res = self._request("getMe", timeout=10.0)
        if not res.get("ok"):
            raise ValueError(f"Telegram getMe failed: {res}")
        return res.get("result", {})

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> List[Dict[str, Any]]:
        """Fetch new updates via long-polling."""
        payload: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        try:
            res = self._request("getUpdates", payload, timeout=timeout + 10.0)
            if res.get("ok"):
                return res.get("result", [])
            logger.warning("Telegram getUpdates returned not ok: %s", res)
            return []
        except (urllib.error.URLError, TimeoutError) as e:
            logger.debug("getUpdates network or timeout exception: %s", e)
            return []

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> bool:
        """Send chat action (e.g. typing) to indicate work in progress."""
        try:
            res = self._request("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10.0)
            return bool(res.get("ok"))
        except Exception as e:
            logger.debug("Failed to send chat action: %s", e)
            return False

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: Optional[str] = "Markdown",
    ) -> bool:
        """Send a text message, splitting into <= 4000 char chunks and retrying plain text on Markdown syntax errors."""
        chunks = self._chunk_text(text, max_len=4000)
        success = True

        for chunk in chunks:
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode

            try:
                res = self._request("sendMessage", payload, timeout=15.0)
                if not res.get("ok"):
                    raise urllib.error.HTTPError(
                        url="", code=400, msg="Telegram API error", hdrs={}, fp=None  # type: ignore
                    )
            except urllib.error.HTTPError as e:
                if parse_mode:
                    logger.debug("Failed sending markdown message (%s), retrying plain text", e)
                    payload.pop("parse_mode", None)
                    try:
                        res = self._request("sendMessage", payload, timeout=15.0)
                        if not res.get("ok"):
                            success = False
                    except Exception as ex:
                        logger.error("Failed sending fallback plain message: %s", ex)
                        success = False
                else:
                    logger.error("Failed sending message: %s", e)
                    success = False
            except Exception as e:
                logger.error("Unexpected error sending message to chat %s: %s", chat_id, e)
                success = False

        return success

    @staticmethod
    def _chunk_text(text: str, max_len: int = 4000) -> List[str]:
        if len(text) <= max_len:
            return [text]

        chunks: List[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > max_len:
                if current:
                    chunks.append(current)
                    current = ""
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                current = line
            else:
                current += line
        if current:
            chunks.append(current)
        return chunks


class ThirtiesTelegramBridge:
    """Headless daemon bridging Telegram to Thirties ConversationManager and Proactive Transitions."""

    def __init__(
        self,
        config: Optional[ThirtiesConfig] = None,
        scheduler: Optional[DeterministicScheduler] = None,
        joplin_engine: Optional[JoplinEngine] = None,
        inference_engine: Optional[InferenceEngine] = None,
        sync_engine: Optional[SyncEngine] = None,
    ) -> None:
        self.config = config or load_config()
        self.bot_token = self.config.telegram.bot_token.strip()
        if not self.bot_token:
            raise ValueError(
                "Telegram bot token is not configured. Set telegram.bot_token in config.toml or specify --token."
            )

        self.client = TelegramClient(self.bot_token)
        self.scheduler = scheduler or DeterministicScheduler(self.config)
        self.joplin_engine = joplin_engine or JoplinEngine(self.config)
        self.inference_engine = inference_engine or get_inference_engine(self.config)
        self.sync_engine = sync_engine or SyncEngine(
            config=self.config,
            state_db=self.scheduler.state_db,
            joplin_engine=self.joplin_engine,
        )

        self.allowed_chat_ids: Set[int] = set(self.config.telegram.allowed_chat_ids)
        self.auto_bound_chat_id: Optional[int] = None
        self.active_chats: Set[int] = set(self.allowed_chat_ids)
        self.current_date: date = self._get_active_diurnal_date()

        self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)

        # Startup sync pull
        if self.config.sync.enabled and self.config.sync.auto_pull_on_startup:
            try:
                ok, msg = self.sync_engine.pull()
                if ok:
                    logger.info("Startup Joplin sync pull: %s", msg)
            except Exception as e:
                logger.warning("Startup Joplin sync pull failed: %s", e)

        day_plan, ambiguous = self.scheduler.build_day_plan(self.current_date)
        self.conv_manager = ConversationManager(
            day_plan=day_plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
            active_notes=self.active_notes,
        )

        self.last_notified_block_index: Optional[int] = None
        self.checked_in_span_ids: Set[str] = set()
        self.running = False
        self._notifier_thread: Optional[threading.Thread] = None

    def _get_active_diurnal_date(self) -> date:
        gen = self.config.general
        return get_diurnal_date(
            lat=gen.latitude,
            lon=gen.longitude,
            tz_name=gen.timezone,
        )

    def _get_current_time(self) -> datetime:
        tz = None
        if self.conv_manager and self.conv_manager.day_plan and self.conv_manager.day_plan.sunrise:
            tz = self.conv_manager.day_plan.sunrise.tzinfo
        return get_current_time(tz)

    def _get_current_block(self) -> Optional[ThirtyBlock]:
        blocks = self.conv_manager.day_plan.blocks
        if not blocks:
            return None
        now = self._get_current_time()
        first_tz = blocks[0].start_dt.tzinfo
        if first_tz is not None and now.tzinfo is None:
            now = now.replace(tzinfo=first_tz)
        elif first_tz is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return next((b for b in blocks if b.start_dt <= now < b.end_dt), None)

    def _check_and_refresh_date(self) -> None:
        today = self._get_active_diurnal_date()
        if today != self.current_date:
            logger.info("Diurnal date rolled over from %s to %s. Reloading day plan.", self.current_date, today)
            self.current_date = today
            self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
            plan, ambiguous = self.scheduler.build_day_plan(today)
            self.conv_manager = ConversationManager(
                day_plan=plan,
                tasks=self.backlog_tasks,
                ambiguous_events=ambiguous,
                scheduler=self.scheduler,
                joplin_engine=self.joplin_engine,
                inference_engine=self.inference_engine,
                active_notes=self.active_notes,
            )
            self.last_notified_block_index = None

    def handle_message(self, chat_id: int, text: str) -> None:
        """Process incoming message from a chat."""
        text = text.strip()

        # With no allowed_chat_ids configured, the first chat to message the bot claims it
        # for the lifetime of this process; everyone else is refused.
        if not self.allowed_chat_ids:
            self.allowed_chat_ids = {chat_id}
            self.auto_bound_chat_id = chat_id
            logger.warning(
                "No telegram.allowed_chat_ids configured: bound this bot to chat %s until restart. "
                "Add allowed_chat_ids = [%s] to config.toml to make that permanent.", chat_id, chat_id,
            )

        # Authorization check
        if chat_id not in self.allowed_chat_ids:
            logger.warning("Unauthorized access attempt from chat_id %s", chat_id)
            self.client.send_message(
                chat_id,
                f"⛔ *Unauthorized*\n\nThis Thirties bot instance is private.\nYour Chat ID: `{chat_id}`",
                parse_mode="Markdown",
            )
            return

        self.active_chats.add(chat_id)
        self._check_and_refresh_date()
        self.conv_manager.refresh()

        # Command Dispatch
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower().split("@")[0]
            if cmd in ("/start", "/help"):
                self._cmd_help(chat_id)
            elif cmd == "/now":
                self._cmd_now(chat_id)
            elif cmd == "/day":
                self._cmd_day(chat_id)
            elif cmd == "/sync":
                self._cmd_sync(chat_id)
            elif cmd in ("/new", "/clear"):
                self._cmd_clear(chat_id)
            elif cmd == "/status":
                self._cmd_status(chat_id)
            else:
                self.client.send_message(
                    chat_id,
                    f"Unknown command `{cmd}`. Send /help to view available commands.",
                    parse_mode="Markdown",
                )
            return

        # Natural Conversation with Gemini
        self.client.send_chat_action(chat_id, "typing")
        try:
            reply = self.conv_manager.send_user_message(text)

            # Auto-push sync if plan changed
            if self.config.sync.enabled and self.config.sync.auto_push_on_change:
                try:
                    self.sync_engine.push()
                except Exception as e:
                    logger.debug("Post-message sync push failed: %s", e)

            self.client.send_message(chat_id, reply, parse_mode="Markdown")
        except Exception as e:
            logger.error("Error generating assistant reply: %s", e, exc_info=True)
            self.client.send_message(
                chat_id,
                f"⚠️ *Assistant Error*:\n`{e}`",
                parse_mode="Markdown",
            )

    def _cmd_help(self, chat_id: int) -> None:
        msg = (
            "🤖 *Thirties Mobile Planning Assistant*\n\n"
            "Powered by Gemini & your Joplin backlog.\n\n"
            "📋 *Commands*:\n"
            "• `/now` – What's happening now, and time until sleep\n"
            "• `/day` – Today's schedule\n"
            "• `/sync` – Push/pull sync with Joplin cross-device state\n"
            "• `/new` – Start a new conversation (the desktop app continues the same one)\n"
            "• `/status` – System, model, and sync status\n"
            "• `/help` – Show this guide\n\n"
            "💬 *Conversation*:\n"
            "Simply send natural messages to schedule and plan:\n"
            "• _\"Plan my evening after 7pm\"_\n"
            "• _\"Move my workout to 3:30pm\"_\n"
            "• _\"What do I have remaining in my backlog?\"_\n"
        )
        if self.auto_bound_chat_id == chat_id:
            msg += (
                f"\n🔒 *Security Note*:\n"
                f"This bot is now bound to your chat (ID `{chat_id}`) until the bridge restarts.\n"
                f"Add it to `~/.config/thirties/config.toml` to make that permanent:\n"
                f"```toml\n"
                f"[telegram]\n"
                f"allowed_chat_ids = [{chat_id}]\n"
                f"```"
            )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def _cmd_now(self, chat_id: int) -> None:
        now = self._get_current_time()
        plan = self.conv_manager.day_plan
        curr = self._get_current_block()

        if not curr:
            self.client.send_message(
                chat_id,
                f"🕒 *Current Time*: {format_clock(now)}\n\nOutside today's diurnal day.",
                parse_mode="Markdown",
            )
            return

        span = plan.display_span(curr)
        rem_mins = max(0, int((curr.end_dt - now).total_seconds())) // 60
        doing = f"{span.title} (until {format_clock(span.end_dt)})" if span else "Open time"

        sleep = next((s for s in plan.active_spans()
                      if s.category == SpanCategory.SLEEP and s.start_dt > now), None)
        sleep_str = ""
        if sleep:
            until = int((sleep.start_dt - now).total_seconds() // 60)
            sleep_str = f"\n🌙 *Until sleep*: {format_duration(until)} (sleep at {format_clock(sleep.start_dt)})"

        msg = (
            f"🕒 *Now*: {format_clock(now)} (thirty #{curr.number:02d}, {format_clock(curr.start_dt)} – {format_clock(curr.end_dt)})\n"
            f"📝 *Doing*: {doing}\n"
            f"⏳ *Left in this thirty*: {rem_mins} minutes"
            f"{sleep_str}"
        )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def _cmd_day(self, chat_id: int) -> None:
        plan = self.conv_manager.day_plan
        now = self._get_current_time()
        lines = [f"📅 *Schedule for {self.current_date.strftime('%A, %b %d')}*\n"]
        spans = plan.spans_in_view()
        if not spans:
            lines.append("Nothing scheduled.")
        for span in spans:
            marker = "👉 " if span.covers(now) else "• "
            note = " (unconfirmed)" if span.tentative else ""
            lines.append(f"{marker}{format_clock(span.start_dt)} – {format_clock(span.end_dt)}: {span.title}{note}")
        self.client.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_sync(self, chat_id: int) -> None:
        self.client.send_chat_action(chat_id, "typing")
        try:
            pull_ok, pull_msg = self.sync_engine.pull()
            push_ok, push_msg = self.sync_engine.push()
            status = "✅ Sync Completed" if (pull_ok and push_ok) else "⚠️ Sync Partially Completed"
            msg = f"*{status}*\n\n📥 *Pull*: {pull_msg}\n📤 *Push*: {push_msg}"
            self.client.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            self.client.send_message(chat_id, f"❌ *Sync Error*: `{e}`", parse_mode="Markdown")

    def _cmd_clear(self, chat_id: int) -> None:
        self.conv_manager.new_conversation()
        self.client.send_message(chat_id, "🆕 Started a new conversation. What's next?")

    def _cmd_status(self, chat_id: int) -> None:
        inf_name = type(self.inference_engine).__name__
        model_name = getattr(self.inference_engine, "model_name", "N/A")
        tasks_count = len(self.backlog_tasks)
        notes_count = len(self.active_notes)
        sync_enabled = "Enabled" if self.config.sync.enabled else "Disabled"
        notify_enabled = "Enabled" if self.config.telegram.notify_block_transitions else "Disabled"

        msg = (
            "⚙️ *Thirties System Status*\n\n"
            f"🧠 *Inference Backend*: `{inf_name}`\n"
            f"🤖 *Model*: `{model_name}`\n"
            f"🔄 *Joplin Sync*: {sync_enabled}\n"
            f"🔔 *Block Buzzes*: {notify_enabled}\n"
            f"📋 *Backlog Tasks*: {tasks_count}\n"
            f"📒 *Active Notes*: {notes_count}\n"
            f"📱 *Connected Chats*: {len(self.active_chats)}"
        )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def _notifier_loop(self) -> None:
        """Background loop that sends lock-screen push notifications at 30-min block transitions."""
        logger.info("Proactive block transition notifier started.")
        while self.running:
            try:
                time.sleep(15.0)
                if not self.running:
                    break

                self._check_and_refresh_date()
                self._maybe_check_in()
                curr = self._get_current_block()
                if not curr:
                    continue

                curr_idx = curr.index

                if self.last_notified_block_index is None:
                    # Initial startup anchor
                    self.last_notified_block_index = curr_idx
                    continue

                if curr_idx != self.last_notified_block_index:
                    logger.info("Thirty transition detected: #%s -> #%s", self.last_notified_block_index, curr.number)
                    self.last_notified_block_index = curr_idx

                    if self.config.telegram.notify_block_transitions and self.active_chats:
                        span = self.conv_manager.day_plan.display_span(curr)
                        s_time, e_time = format_clock(curr.start_dt), format_clock(curr.end_dt)
                        if span and span.category == SpanCategory.SLEEP and span.start_dt >= curr.start_dt:
                            buzz_msg = f"🌙 *Bedtime* ({s_time})\n\nYour sleep starts now. Wind down and rest well!"
                        elif span and span.start_dt >= curr.start_dt:
                            buzz_msg = f"🔔 *{span.title}* starts now ({s_time} – {format_clock(span.end_dt)})"
                        elif span:
                            continue  # still inside something already announced
                        else:
                            buzz_msg = f"🔔 *Open time* ({s_time} – {e_time})"

                        for chat_id in list(self.active_chats):
                            self.client.send_message(chat_id, buzz_msg, parse_mode="Markdown")

            except Exception as e:
                logger.error("Error in block transition notifier loop: %s", e)

    def _maybe_check_in(self) -> None:
        """After a planned activity ends with no outcome recorded, ask about it once."""
        cfg = self.config.assistant
        if not (cfg.checkins and self.active_chats):
            return
        now = self._get_current_time()
        grace = timedelta(minutes=cfg.checkin_after_minutes)
        plan = self.conv_manager.day_plan
        due = [s for s in plan.active_spans()
               if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and s.status.value == "planned"
               and s.id not in self.checked_in_span_ids
               and s.end_dt + grace <= now < s.end_dt + timedelta(hours=2)]
        if not due:
            return
        span = due[-1]
        self.checked_in_span_ids.update(s.id for s in due)
        following = next((s for s in plan.active_spans()
                          if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and s.covers(now)), None)
        question = f"👋 Just checking in: did you get to *{span.title}*"
        question += f", or are you still on it? (Next up: {following.title}.)" if following else "?"
        self.conv_manager.refresh()
        self.conv_manager.add_assistant_message(question.replace("*", ""))
        for chat_id in list(self.active_chats):
            self.client.send_message(chat_id, question, parse_mode="Markdown")

    def start(self) -> None:
        """Start the polling listener and transition notifier."""
        bot_info = self.client.get_me()
        bot_name = bot_info.get("first_name", "Thirties Bot")
        bot_user = bot_info.get("username", "")
        logger.info("Connected to Telegram Bot: %s (@%s)", bot_name, bot_user)
        print("\n=======================================================")
        print(f"🚀 Thirties Telegram Bridge Running as @{bot_user}!")
        print(f"📱 Open Telegram on your phone and send /start to @{bot_user}")
        if not self.allowed_chat_ids:
            print("⚠️  No allowed_chat_ids set: the first chat to message the bot will be bound to it.")
        print("=======================================================\\n")

        self.running = True
        self._notifier_thread = threading.Thread(target=self._notifier_loop, daemon=True, name="BlockNotifier")
        self._notifier_thread.start()

        offset: Optional[int] = None
        while self.running:
            try:
                updates = self.client.get_updates(offset=offset, timeout=25)
                for u in updates:
                    update_id = u.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1

                    msg = u.get("message") or u.get("edited_message")
                    if not msg:
                        continue

                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    text = msg.get("text", "")
                    if chat_id is not None and text:
                        self.handle_message(chat_id, text)

            except Exception as e:
                if self.running:
                    logger.error("Polling error: %s (sleeping 5s)", e)
                    time.sleep(5.0)

    def stop(self) -> None:
        """Stop the bridge gracefully."""
        self.running = False
        logger.info("Thirties Telegram Bridge stopped.")


def check_setup(cfg: ThirtiesConfig) -> bool:
    """Print a checklist of everything the bridge needs. Returns True if it can run."""
    from thirties_core.config import get_default_config_path
    from thirties_core.inference import AntigravityInferenceEngine, GeminiInferenceEngine

    ok = True

    def line(passed: Optional[bool], label: str, detail: str = "") -> None:
        mark = {True: "✅", False: "❌", None: "⚠️ "}[passed]
        print(f"{mark} {label}{': ' + detail if detail else ''}")

    path = get_default_config_path()
    line(path.is_file() or None, "Config file", str(path) if path.is_file() else f"{path} not found (using defaults)")

    token = cfg.telegram.bot_token.strip()
    if not token:
        ok = False
        line(False, "Bot token", "set [telegram] bot_token (get one from @BotFather)")
    else:
        try:
            me = TelegramClient(token).get_me()
            line(True, "Bot token", f"connected as @{me.get('username', '?')}")
        except Exception as e:
            ok = False
            line(False, "Bot token", f"Telegram rejected it: {e}")

    if cfg.telegram.allowed_chat_ids:
        line(True, "Allowed chats", ", ".join(str(c) for c in cfg.telegram.allowed_chat_ids))
    else:
        line(None, "Allowed chats", "none yet: the first chat to message the bot gets bound until restart")

    engine = get_inference_engine(cfg)
    name = type(engine).__name__
    if isinstance(engine, (GeminiInferenceEngine, AntigravityInferenceEngine)):
        try:
            reply = engine.chat([{"role": "user", "content": "Reply with the single word: ready"}])
            passed = "ready" in reply.get("content", "").lower()
            ok = ok and passed
            line(passed, "Model", f"{name} ({engine.model}) replied: {reply.get('content', '')[:80]!r}")
        except Exception as e:
            ok = False
            line(False, "Model", f"{name}: {e}")
    else:
        ok = False
        line(False, "Model", f"{name}: no working model backend. Install and sign in to agy, or set "
             "[inference] backend = \"gemini_api\" and gemini_api_key")

    joplin_db = cfg.resolve_joplin_db_path()
    line(bool(joplin_db) or None, "Joplin backlog", str(joplin_db) if joplin_db else "database not found; the assistant will have no backlog")
    if cfg.sync.enabled:
        has_token = bool(cfg.joplin.api_token)
        line(has_token or None, "Joplin sync", "enabled" if has_token else
             "enabled but [joplin] api_token is empty; set it, or set [sync] enabled = false")

    gen = cfg.general
    line(None, "Location", f"lat {gen.latitude}, lon {gen.longitude}, {gen.timezone} (sunrise/sunset come from this)")
    print("\nReady to run." if ok else "\nFix the ❌ items above, then run --check again.")
    return ok


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Thirties Headless Telegram Bridge")
    parser.add_argument("--token", type=str, default="", help="Telegram Bot Token")
    parser.add_argument("--config", type=str, default="", help="Path to config.toml")
    parser.add_argument("--no-notify", action="store_true", help="Disable proactive block buzzes")
    parser.add_argument("--check", action="store_true", help="Check the setup (token, model, Joplin, location) and exit")
    args = parser.parse_args()

    cfg = load_config(args.config if args.config else None)
    if args.token:
        cfg.telegram.bot_token = args.token
    if args.no_notify:
        cfg.telegram.notify_block_transitions = False
    if args.check:
        sys.exit(0 if check_setup(cfg) else 1)

    bridge = ThirtiesTelegramBridge(config=cfg)

    def sig_handler(sig, frame):
        print("\nShutting down Telegram Bridge...")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        bridge.start()
    except KeyboardInterrupt:
        bridge.stop()


if __name__ == "__main__":
    main()
