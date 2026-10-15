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
from thirties_core.conversation import ConversationManager
from thirties_core.inference import InferenceEngine, get_inference_engine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.models import BlockKind, DayPlan, ThirtyBlock
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
        self.active_chats: Set[int] = set(self.allowed_chat_ids)
        self.current_date: date = self._get_active_diurnal_date()

        self.backlog_tasks = self.joplin_engine.fetch_tasks()
        self.active_notes = self.joplin_engine.fetch_active_notes()
        self.scheduler.attach_history_to_tasks(self.backlog_tasks)

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
            self.backlog_tasks = self.joplin_engine.fetch_tasks()
            self.active_notes = self.joplin_engine.fetch_active_notes()
            self.scheduler.attach_history_to_tasks(self.backlog_tasks)
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

        # Authorization check
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning("Unauthorized access attempt from chat_id %s", chat_id)
            self.client.send_message(
                chat_id,
                f"⛔ *Unauthorized*\n\nThis Thirties bot instance is private.\nYour Chat ID: `{chat_id}`",
                parse_mode="Markdown",
            )
            return

        # If no allowed_chat_ids were specified in config, allow and register the first chat
        if not self.allowed_chat_ids and chat_id not in self.active_chats:
            self.active_chats.add(chat_id)
            logger.info("Automatically registered active chat ID: %s", chat_id)

        self.active_chats.add(chat_id)
        self._check_and_refresh_date()

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
            elif cmd == "/clear":
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
            "Powered by Gemini 3.8 Flash & Joplin State Sync.\n\n"
            "📋 *Commands*:\n"
            "• `/now` – Current block, active task, and countdown\n"
            "• `/day` – Overview of today's 30-minute block schedule\n"
            "• `/sync` – Push/pull sync with Joplin cross-device state\n"
            "• `/clear` – Clear conversation context (start fresh)\n"
            "• `/status` – System, model, and sync status\n"
            "• `/help` – Show this guide\n\n"
            "💬 *Conversation*:\n"
            "Simply send natural messages to schedule and plan:\n"
            "• _\"Plan my evening after 7pm\"_\n"
            "• _\"Move my workout to 3:30pm\"_\n"
            "• _\"What do I have remaining in my backlog?\"_\n"
        )
        if not self.allowed_chat_ids:
            msg += (
                f"\n🔒 *Security Note*:\n"
                f"Your Chat ID is `{chat_id}`.\n"
                f"Add it to `~/.config/thirties/config.toml` to lock down this bot:\n"
                f"```toml\n"
                f"[telegram]\\n"
                f"allowed_chat_ids = [{chat_id}]\n"
                f"```"
            )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def _cmd_now(self, chat_id: int) -> None:
        now = self._get_current_time()
        blocks = self.conv_manager.day_plan.blocks
        curr = self._get_current_block()

        if not curr:
            self.client.send_message(
                chat_id,
                f"🕒 *Current Time*: {now.strftime('%I:%M %p')}\n\nOutside scheduled diurnal bounds for today.",
                parse_mode="Markdown",
            )
            return

        idx = self.conv_manager.day_plan.get_logical_index(curr)
        remaining_secs = max(0, int((curr.end_dt - now).total_seconds()))
        rem_mins = remaining_secs // 60

        first_sleep = next((b for b in blocks if b.kind == BlockKind.SLEEP), None)
        sleep_str = ""
        if first_sleep:
            sleep_idx = self.conv_manager.day_plan.get_logical_index(first_sleep)
            if idx is not None and sleep_idx is not None and sleep_idx >= idx:
                sleep_str = f"\n🌙 *Blocks until sleep*: {sleep_idx - idx} chunks (Sleep at {first_sleep.start_dt.strftime('%I:%M %p')})"

        kind_desc = curr.kind.name.replace("_", " ").title()
        label = curr.label or "(Unassigned / Discretionary)"

        msg = (
            f"🕒 *Current Block*: **Block {idx}**\n"
            f"⏱️ *Time*: {curr.start_dt.strftime('%I:%M %p')} – {curr.end_dt.strftime('%I:%M %p')}\n"
            f"🏷️ *Type*: {kind_desc}\n"
            f"📝 *Task*: {label}\n"
            f"⏳ *Time Remaining in Block*: {rem_mins} minutes"
            f"{sleep_str}"
        )
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

    def _cmd_day(self, chat_id: int) -> None:
        plan = self.conv_manager.day_plan
        lines = [f"📅 *Schedule for {self.current_date.strftime('%A, %b %d')}*\n"]

        curr_b = self._get_current_block()
        curr_idx = plan.get_logical_index(curr_b) if curr_b else None

        groups: List[tuple[int, int, ThirtyBlock]] = []
        for b in plan.blocks:
            idx = plan.get_logical_index(b)
            if not idx:
                continue
            if groups and groups[-1][2].kind == b.kind and groups[-1][2].label == b.label:
                start_i, _, first_b = groups[-1]
                groups[-1] = (start_i, idx, first_b)
            else:
                groups.append((idx, idx, b))

        for start_i, end_i, b in groups:
            s_time = b.start_dt.strftime("%I:%M %p")
            end_b = plan.get_logical_block(end_i) or b
            e_time = end_b.end_dt.strftime("%I:%M %p")
            range_str = f"Block {start_i}" if start_i == end_i else f"Blocks {start_i}–{end_i}"

            is_active = (curr_idx is not None and start_i <= curr_idx <= end_i)
            marker = "👉 " if is_active else "• "

            lbl = f" – {b.label}" if b.label else ""
            lines.append(f"{marker}*{range_str}* ({s_time}–{e_time}): {b.kind.name.replace('_', ' ').title()}{lbl}")

        msg = "\n".join(lines)
        self.client.send_message(chat_id, msg, parse_mode="Markdown")

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
        plan, ambiguous = self.scheduler.build_day_plan(self.current_date)
        self.conv_manager = ConversationManager(
            day_plan=plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
            active_notes=self.active_notes,
        )
        self.client.send_message(chat_id, "🧹 Conversation context cleared and re-initialized with fresh schedule.", parse_mode="Markdown")

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
                curr = self._get_current_block()
                if not curr:
                    continue

                curr_idx = self.conv_manager.day_plan.get_logical_index(curr)
                if curr_idx is None:
                    continue

                if self.last_notified_block_index is None:
                    # Initial startup anchor
                    self.last_notified_block_index = curr_idx
                    continue

                if curr_idx != self.last_notified_block_index:
                    logger.info("Block transition detected: Block %s -> Block %s", self.last_notified_block_index, curr_idx)
                    self.last_notified_block_index = curr_idx

                    if self.config.telegram.notify_block_transitions and self.active_chats:
                        s_time = curr.start_dt.strftime("%I:%M %p")
                        e_time = curr.end_dt.strftime("%I:%M %p")
                        kind_str = curr.kind.name.replace("_", " ").title()
                        task_str = curr.label or "Discretionary / Unassigned"

                        if curr.kind == BlockKind.SLEEP:
                            buzz_msg = (
                                f"🌙 *Bedtime: Block {curr_idx}* ({s_time} – {e_time})\n\n"
                                f"Your scheduled sleep block has begun. Wind down and rest well!"
                            )
                        else:
                            buzz_msg = (
                                f"🔔 *Block {curr_idx}* ({s_time} – {e_time})\n"
                                f"🏷️ *Type*: {kind_str}\n"
                                f"📝 *Task*: {task_str}"
                            )

                        for chat_id in list(self.active_chats):
                            self.client.send_message(chat_id, buzz_msg, parse_mode="Markdown")

            except Exception as e:
                logger.error("Error in block transition notifier loop: %s", e)

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
            print("⚠️  No allowed_chat_ids set: will auto-bind first user.")
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Thirties Headless Telegram Bridge")
    parser.add_argument("--token", type=str, default="", help="Telegram Bot Token")
    parser.add_argument("--config", type=str, default="", help="Path to config.toml")
    parser.add_argument("--no-notify", action="store_true", help="Disable proactive block buzzes")
    args = parser.parse_args()

    cfg = load_config(args.config if args.config else None)
    if args.token:
        cfg.telegram.bot_token = args.token
    if args.no_notify:
        cfg.telegram.notify_block_transitions = False

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
