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

from thirties_core.brains import BRAINS
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
        except urllib.error.HTTPError:
            raise
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


def pair_by_code(client: TelegramClient, code: str, timeout: float = 300.0,
                 cancel: Optional[threading.Event] = None) -> Optional[Dict[str, Any]]:
    """Wait for someone to send the bot this one-time code; returns their chat ({"id", "name"}) or None.

    The code proves the chat belongs to the person at the computer, so the pairing can be
    saved for good (unlike binding whichever chat writes first).
    """
    deadline = time.time() + timeout
    offset: Optional[int] = None
    code = code.strip()
    while time.time() < deadline and not (cancel is not None and cancel.is_set()):
        for u in client.get_updates(offset=offset, timeout=5):
            offset = u.get("update_id", 0) + 1
            msg = u.get("message") or {}
            words = (msg.get("text") or "").replace("/start", " ").split()
            if code in words:
                chat = msg.get("chat", {})
                client.get_updates(offset=offset, timeout=0)  # mark it read
                name = chat.get("first_name") or chat.get("username") or str(chat.get("id"))
                client.send_message(chat["id"], f"✅ Linked to Thirties. Hi {name}! You can plan here now; "
                                                f"send /help to see what I can do.", parse_mode=None)
                return {"id": chat["id"], "name": name}
    return None


class ThirtiesTelegramBridge:
    """Headless daemon bridging Telegram to Thirties ConversationManager and Proactive Transitions."""

    def __init__(
        self,
        config: Optional[ThirtiesConfig] = None,
        scheduler: Optional[DeterministicScheduler] = None,
        joplin_engine: Optional[JoplinEngine] = None,
        inference_engine: Optional[InferenceEngine] = None,
        sync_engine: Optional[SyncEngine] = None,
        service: Any = None,
    ) -> None:
        """With ``service`` (the app's BackgroundService), the bridge shares its day and conversation."""
        self.service = service
        if service is not None:
            config, scheduler, joplin_engine = service.config, service.scheduler, service.joplin_engine
            inference_engine, sync_engine = service.inference_engine, service.sync_engine
        self.config = config or load_config()
        self.bot_token = self.config.telegram.bot_token.strip()
        if not self.bot_token:
            raise ValueError("No Telegram bot token: set one up in Preferences → Phone.")

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
        self.active_chats: Set[int] = set(self.allowed_chat_ids)
        self.current_date: date = self._get_active_diurnal_date()
        self._own_manager: Optional[ConversationManager] = None
        self.running = False

        if service is None:
            self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
            day_plan, ambiguous = self.scheduler.build_day_plan(self.current_date)
            self._own_manager = ConversationManager(
                day_plan=day_plan,
                tasks=self.backlog_tasks,
                ambiguous_events=ambiguous,
                scheduler=self.scheduler,
                joplin_engine=self.joplin_engine,
                inference_engine=self.inference_engine,
                active_notes=self.active_notes,
            )

    @property
    def conv_manager(self) -> ConversationManager:
        return self.service.manager() if self.service is not None else self._own_manager

    @conv_manager.setter
    def conv_manager(self, value: ConversationManager) -> None:
        self._own_manager = value

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
        if self.service is not None:
            return  # the service follows the day
        today = self._get_active_diurnal_date()
        if today != self.current_date:
            logger.info("Diurnal date rolled over from %s to %s. Reloading day plan.", self.current_date, today)
            self.current_date = today
            self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
            plan, ambiguous = self.scheduler.build_day_plan(today)
            self._own_manager = ConversationManager(
                day_plan=plan,
                tasks=self.backlog_tasks,
                ambiguous_events=ambiguous,
                scheduler=self.scheduler,
                joplin_engine=self.joplin_engine,
                inference_engine=self.inference_engine,
                active_notes=self.active_notes,
            )

    def handle_message(self, chat_id: int, text: str) -> None:
        """Process incoming message from a chat."""
        text = text.strip()

        if not self.allowed_chat_ids:
            self.client.send_message(chat_id, "This bot isn't linked to Thirties yet. In the Thirties app, open "
                                     "Preferences → Phone and follow the steps there.", parse_mode=None)
            return

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
            elif cmd == "/brain":
                self.client.send_message(chat_id, self.conv_manager.send_user_message(text))
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
            "• `/brain` – Which brain answers; `/brain gemma` or `/brain gemini` to switch\n"
            "• `/help` – Show this guide\n\n"
            "💬 *Conversation*:\n"
            "Simply send natural messages to schedule and plan:\n"
            "• _\"Plan my evening after 7pm\"_\n"
            "• _\"Move my workout to 3:30pm\"_\n"
            "• _\"What do I have remaining in my backlog?\"_\n"
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
        services = self.conv_manager.tools.services
        tasks_count = len(services.tasks) if services else 0
        notes_count = len(getattr(self, "active_notes", []) or [])
        sync_enabled = "Enabled" if self.config.sync.enabled else "Disabled"
        notify_enabled = getattr(self.config.assistant, "message_level", "medium").capitalize()

        msg = (
            "⚙️ *Thirties System Status*\n\n"
            f"🧠 *Inference Backend*: `{inf_name}`\n"
            f"🧠 *Brain*: {BRAINS.get(self.conv_manager.brain_name, self.conv_manager.brain_name)}\n"
            f"🤖 *Model*: `{model_name}`\n"
            f"🔄 *Joplin Sync*: {sync_enabled}\n"
            f"🔔 *Block Buzzes*: {notify_enabled}\n"
            f"📋 *Backlog Tasks*: {tasks_count}\n"
            f"📒 *Active Notes*: {notes_count}\n"
            f"📱 *Connected Chats*: {len(self.active_chats)}"
        )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def broadcast(self, text: str) -> None:
        """Send a message to the paired phone(s)."""
        for chat_id in list(self.allowed_chat_ids):
            self.client.send_message(chat_id, text, parse_mode="Markdown")

    def poll_forever(self) -> None:
        """Answer messages until stopped. Another computer polling the same bot makes Telegram refuse
        one of the two (HTTP 409); then this one backs off, since only one computer should run the bridge."""
        self.running = True
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
                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")
                    if chat_id is not None and text:
                        self.handle_message(chat_id, text)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    logger.warning("Another computer is running the Telegram bridge for this bot; backing off.")
                    time.sleep(60)
                else:
                    logger.error("Telegram polling error: %s", e)
                    time.sleep(5)
            except Exception as e:
                if self.running:
                    logger.error("Polling error: %s (sleeping 5s)", e)
                    time.sleep(5.0)

    def start(self) -> None:
        """Run standalone (a developer tool; the app runs the bridge inside its background service)."""
        bot_user = self.client.get_me().get("username", "")
        logger.info("Thirties Telegram bridge running as @%s.", bot_user)
        self.conv_manager.warm_in_background()
        self.poll_forever()

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
        line(False, "Paired phone", "none yet: pair it in the app (Preferences → Phone)")
        ok = False

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
        cfg.assistant.message_level = "off"
    if args.check:
        sys.exit(0 if check_setup(cfg) else 1)

    from thirties_core.background import BackgroundService
    service = BackgroundService(cfg)

    def sig_handler(sig, frame):
        print("\nShutting down...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    service.start()
    while service.running:
        time.sleep(1)


if __name__ == "__main__":
    main()
