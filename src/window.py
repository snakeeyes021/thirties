"""Main application window for Thirties."""

import logging
import threading
import time
from datetime import date, timedelta
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from thirties_core.astronomy import get_diurnal_date
from thirties_core.backlog import load_backlog
from thirties_core.brains import BRAINS, brain_choice
from thirties_core.conversation import ConversationManager
from thirties.views.chat_panel import ChatPanel
from thirties.views.day_view import DayView
from thirties.views.profile_dialog import ProfileMemoryDialog

logger = logging.getLogger(__name__)

# How often the live "now" (gold highlight, sun/moon, remaining tallies) advances.
TICK_SECONDS = 30
# How often to pick up changes other clients (the Telegram bot) made to the day and its conversation.
STORE_REFRESH_SECONDS = 60
# Refreshing on focus is skipped if the last refresh was this recent.
FOCUS_REFRESH_MIN_SECONDS = 5


@Gtk.Template(resource_path='/tech/redfoxlabs/Thirties/window.ui')
class ThirtiesWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ThirtiesWindow'

    prev_day_btn = Gtk.Template.Child()
    next_day_btn = Gtk.Template.Child()
    today_btn = Gtk.Template.Child()
    calendar_menu_btn = Gtk.Template.Child()
    calendar_popover = Gtk.Template.Child()
    date_calendar = Gtk.Template.Child()
    window_title = Gtk.Template.Child()
    chat_toggle_btn = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    toast_overlay = Gtk.Template.Child()
    content_toolbar = Gtk.Template.Child()
    chat_toolbar = Gtk.Template.Child()
    chat_header = Gtk.Template.Child()

    def __init__(self, core=None, **kwargs):
        super().__init__(**kwargs)
        if core is None:
            from thirties_core.app_core import AppCore
            core = AppCore(use_keyring=False)
        self.core = core
        self.config = core.config
        self.scheduler = core.scheduler
        self.joplin_engine = core.joplin_engine
        self.inference_engine = core.inference_engine
        self.sync_engine = core.sync_engine
        self.active_date = self._get_active_diurnal_date()
        self.current_date = self.active_date

        # Ingest backlog tasks & notes from Joplin
        self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
        self.scheduler.attach_history_to_tasks(self.backlog_tasks)

        if self.config.sync.enabled and self.config.sync.auto_pull_on_startup:
            try:
                ok, msg = self.sync_engine.pull()
                if ok:
                    logger.info("Startup sync pull: %s", msg)
            except Exception as e:
                logger.warning("Startup sync pull failed (continuing normally): %s", e)
        core.listeners.append(self._on_settings_changed)

        # Schedule on the left, chat on the right (see window.ui)
        self.day_view = DayView()
        self.day_view.set_actions(self._run_day_tool, self._on_replanned, on_notice=self._toast,
                                  routines=self._day_routines, ongoing=self._ongoing)
        self.day_view.agenda.fixed = lambda span: (self.conv_manager.tools._is_fixed(span) if self.conv_manager
                                                   else span.source == "calendar" or span.is_firm)
        self.day_view.agenda.read_tool = lambda name, args: (self.conv_manager.execute_tool(name, args)
                                                             if self.conv_manager is not None else "")
        self.content_toolbar.set_content(self.day_view)
        self._wire_timeline(self.day_view.timeline)

        self.conv_manager = None
        self.chat_panel = ChatPanel(on_plan_updated=self._on_plan_updated, external_header=True)
        self.chat_toolbar.set_content(self.chat_panel)
        self.chat_header.set_title_widget(self.chat_panel.conversations_btn)
        for button in reversed(self.chat_panel.header_buttons):
            self.chat_header.pack_end(button)

        # Event connections
        self.prev_day_btn.connect("clicked", self._on_prev_day)
        self.next_day_btn.connect("clicked", self._on_next_day)
        self.today_btn.connect("clicked", self._on_today_clicked)
        self.date_calendar.connect("day-selected", self._on_calendar_day_selected)
        self.split_view.connect("notify::show-sidebar", self._on_chat_visibility_changed)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        self._install_actions()
        self._install_type_to_chat()

        self._last_store_refresh = time.monotonic()
        self._refreshing = False  # a background reload is running
        self._local_changes = 0  # changes made in the day view (see _on_store_refreshed)
        self._timers = [
            GLib.timeout_add_seconds(TICK_SECONDS, self._on_tick),
            GLib.timeout_add_seconds(STORE_REFRESH_SECONDS, self._on_store_refresh_timer),
        ]

        self._load_day(self.current_date)
        GLib.idle_add(self.chat_panel.focus_input)
        GLib.idle_add(self._mention_outdated_memory)

    # --- setup ---

    def _install_actions(self) -> None:
        toggle = Gio.SimpleAction.new("toggle-chat", None)
        toggle.connect("activate", lambda *_: self.chat_toggle_btn.set_active(not self.chat_toggle_btn.get_active()))
        self.add_action(toggle)

        brain = Gio.SimpleAction.new_stateful(
            "brain", GLib.VariantType.new("s"),
            GLib.Variant("s", brain_choice(self.config, self.scheduler.state_db)))
        brain.connect("change-state", self._on_brain_change)
        self.add_action(brain)

        profile = Gio.SimpleAction.new("profile-memory", None)
        profile.connect("activate", lambda *_: self._show_profile_dialog())
        self.add_action(profile)

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.toggle-chat", ["F9"])

    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def _on_brain_change(self, action, value) -> None:
        """Main menu → Assistant: switch brains on this device; the conversation carries on."""
        name = value.get_string()
        if name == action.get_state().get_string():
            return
        from thirties_core.brains import brain_unavailable_reason
        why = brain_unavailable_reason(name, self.config)
        if why:
            self._toast(f"{BRAINS[name].split(' (')[0]} isn't available: {why}")
            return
        if self.chat_panel.is_busy:
            self._toast("Switch after the current reply arrives.")
            return
        if self.conv_manager is not None:
            self.conv_manager.set_brain(name)
        else:
            from thirties_core.settings import Settings
            Settings(self.scheduler.state_db).set("assistant.brain", name)
        action.set_state(value)
        self._toast(f"Now using {BRAINS[name]}" + (". It takes a few seconds to load." if name == "slm" else "."))

    def _sync_brain_action(self) -> None:
        """Show a switch made elsewhere (/brain in Telegram) in the menu."""
        action = self.lookup_action("brain")
        if action is not None and self.conv_manager is not None and \
                action.get_state().get_string() != self.conv_manager.brain_name:
            action.set_state(GLib.Variant("s", self.conv_manager.brain_name))

    def _install_type_to_chat(self) -> None:
        """Typing anywhere in the window goes to the chat input while the chat is shown."""
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(keys)

    def _on_window_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if not self.split_view.get_show_sidebar() or self.get_visible_dialog() is not None:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        char = chr(Gdk.keyval_to_unicode(keyval) or 0)
        if not char.isprintable() or char.isspace() or char == "\0":
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.Text, Gtk.TextView)):
            return False
        text_view = self.chat_panel.text_view
        if not text_view.get_sensitive():
            return False
        text_view.grab_focus()
        return controller.forward(text_view)

    def _on_close_request(self, _window) -> bool:
        for source_id in self._timers:
            GLib.source_remove(source_id)
        self._timers = []
        if self._on_settings_changed in self.core.listeners:
            self.core.listeners.remove(self._on_settings_changed)
        return False

    def _on_settings_changed(self) -> None:
        """Preferences changed: pick up the new engines and reload the day (keeping its conversation)."""
        self.joplin_engine = self.core.joplin_engine
        self.inference_engine = self.core.inference_engine
        self.sync_engine = self.core.sync_engine
        self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
        self.scheduler.attach_history_to_tasks(self.backlog_tasks)
        if not self.chat_panel.is_busy:
            self._follow_week()
            self._load_day(self.current_date)
        self._sync_brain_action()

    def _follow_week(self) -> None:
        """Work and Sleep already on the plan follow new hours from Preferences, as they do after set_week (except
        where they were changed by hand)."""
        assistant = self.conv_manager.tools.assistant if self.conv_manager else None
        if assistant is not None:
            from thirties_core.plan_log import caused_by
            try:
                with caused_by("new work hours in Preferences"):  # the plan change log's cause
                    assistant.routines._refresh_envelopes()
            except Exception as e:
                logger.warning("Updating Work and Sleep to the new hours failed: %s", e)

    def _day_routines(self, plan):
        assistant = self.conv_manager.tools.assistant if self.conv_manager else None
        return assistant.routines.day_routines(plan) if assistant is not None else []

    def _ongoing(self):
        """span -> bool, from the assistant's reading of kinds (list roles, the user's word), read once."""
        from thirties_core.kinds import ONGOING
        assistant = self.conv_manager.tools.assistant if self.conv_manager else None
        if assistant is None:
            raise LookupError("no assistant yet")
        ctx = assistant._kind_context()
        return lambda span: assistant.kind_of(span, ctx) == ONGOING

    # --- the Timeline page ---

    def _wire_timeline(self, page) -> None:
        """What the Timeline page reads and changes, all through the core (timeline_layout, placement, horizons)."""
        from thirties_core.timeline_layout import DEFAULT_UNITS, UNITS_SETTING, stretch_options, what_to_plan

        def tools():
            return self.conv_manager.tools if self.conv_manager is not None else None

        def options(stretch, minutes):
            t = tools()
            return stretch_options(t, t.day_plan, stretch, minutes, page.now()) if t else []

        def suggestions():
            t = tools()
            return what_to_plan(t, t.day_plan.target_date) if t else []

        def set_units(units):
            self.core.settings.set(UNITS_SETTING, units)
            self._push_sync()

        def toast(title, undo):
            t = Adw.Toast(title=title, button_label="Undo", timeout=6)
            t.connect("button-clicked", lambda *_: undo())
            self.toast_overlay.add_toast(t)

        page.toast = toast
        page.load_week = self._load_week
        page.stretch_options = options
        page.suggestions = suggestions
        page.get_units = lambda: self.core.settings.get(UNITS_SETTING, DEFAULT_UNITS)
        page.set_units = set_units

    def _load_week(self, anchor: date, days):
        """The week's plans and its chips (pinned to the week, no day yet), read on the Timeline's worker thread;
        never during an assistant turn."""
        from thirties_core.horizons import HorizonPlanner
        from thirties_core.timeline_layout import unplaced_in_week
        manager = self.conv_manager
        if manager is None:
            return [], []
        if not manager.turn_lock.acquire(timeout=60):
            raise TimeoutError("the assistant is busy")
        try:
            plans = [manager.day_plan if d == manager.day_plan.target_date else self.scheduler.build_day_plan(d)[0]
                     for d in days]
            try:
                chips = unplaced_in_week(HorizonPlanner(manager.tools), anchor)
            except Exception as e:
                logger.warning("Reading the week's plan failed: %s", e)
                chips = []
        finally:
            manager.turn_lock.release()
        return plans, chips

    # --- loading days ---

    def _new_conversation_manager(self, plan, ambiguous) -> ConversationManager:
        manager = ConversationManager(
            day_plan=plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
            active_notes=self.active_notes,
        )
        manager.warm_in_background()  # the on-device brain loads its model off the UI thread
        return manager

    def _load_day(self, target_date: date) -> None:
        """Show a day: its plan on the left and its current conversation on the right."""
        self.current_date = target_date
        self._update_day_chrome()

        # Sync GtkCalendar selected day
        try:
            gdt = GLib.DateTime.new_local(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            self.date_calendar.select_day(gdt)
        except Exception:
            pass

        # The conversation manager owns the day's plan; the schedule shows the same object.
        plan, ambiguous = self.scheduler.build_day_plan(target_date)
        self.conv_manager = self._new_conversation_manager(plan, ambiguous)
        from thirties_core.horizons import week_start_index
        self.day_view.timeline.week_start = week_start_index(self.config)
        self.day_view.refresh_plan(self.conv_manager.day_plan)
        self.day_view.scroll_to_now()
        self.chat_panel.set_conversation_manager(self.conv_manager)
        if self.split_view.get_show_sidebar():
            GLib.idle_add(self.chat_panel.focus_input)

    def _update_day_chrome(self) -> None:
        self.today_btn.set_sensitive(self.current_date != self.active_date)
        self.day_view.set_today(self.active_date)
        target = self.current_date
        if target == self.active_date:
            subtitle = "Today"
        elif target == self.active_date - timedelta(days=1):
            subtitle = "Yesterday"
        elif target == self.active_date + timedelta(days=1):
            subtitle = "Tomorrow"
        else:
            subtitle = target.strftime("%A, %B %d, %Y")
        self.window_title.set_subtitle(subtitle)

    # --- live updates ---

    def _on_tick(self) -> bool:
        """Every TICK_SECONDS: advance "now" and follow the diurnal rollover at sunrise."""
        active = self._get_active_diurnal_date()
        if active != self.active_date:
            was_viewing_today = self.current_date == self.active_date
            self.active_date = active
            if was_viewing_today and not self.chat_panel.is_busy:
                self._load_day(active)
                return True
            self._update_day_chrome()
        self.day_view.tick()
        return True

    def _on_store_refresh_timer(self) -> bool:
        self._refresh_from_store()
        return True

    def _on_active_changed(self, _window, _param) -> None:
        if self.is_active() and time.monotonic() - self._last_store_refresh >= FOCUS_REFRESH_MIN_SECONDS:
            self._refresh_from_store()

    def _refresh_from_store(self) -> None:
        """Pick up changes other clients (e.g. the Telegram bot) made to this day and its conversation.

        The reload (Joplin's database, the calendar over D-Bus, the plan) runs on a worker thread, so the window
        never stalls every minute; the result is drawn back on the UI thread. One refresh at a time, and never
        during an assistant turn (the worker takes the turn lock).
        """
        manager = self.conv_manager
        if manager is None or self.chat_panel.is_busy or self._refreshing:
            return
        self._refreshing = True
        self._last_store_refresh = time.monotonic()
        taps_before = self._local_changes

        def worker():
            # never during an assistant turn: the turn lock is taken here, on the worker (it's re-entrant, so the
            # thread that takes it releases it); a turn under way means the next timer picks the changes up
            if not manager.turn_lock.acquire(blocking=False):
                GLib.idle_add(self._on_store_refreshed, manager, False, taps_before)
                return
            ok = True
            try:
                manager.refresh()
            except Exception as e:
                logger.warning("Refreshing from the database failed: %s", e)
                ok = False
            finally:
                manager.turn_lock.release()
            GLib.idle_add(self._on_store_refreshed, manager, ok, taps_before)

        threading.Thread(target=worker, daemon=True).start()

    def _on_store_refreshed(self, manager, ok: bool, taps_before: int) -> bool:
        self._refreshing = False
        if not ok or manager is not self.conv_manager:
            return False  # failed, or another day was opened meanwhile
        if self._local_changes != taps_before:
            # a change made in the day view while the reload ran may be missing from the reloaded plan
            manager.reload_plan()
        self.day_view.refresh_plan(manager.day_plan)
        self.chat_panel.after_store_refresh()
        self._sync_brain_action()
        return False

    def _on_plan_updated(self, plan) -> None:
        """Triggered after an assistant turn, which may have changed the plan."""
        if self.conv_manager is not None and plan.target_date == self.current_date:
            self.day_view.refresh_plan(self.conv_manager.day_plan)
        self._sync_brain_action()  # "/brain gemma" typed in the chat
        self._push_sync()

    def _run_day_tool(self, name: str, args: dict) -> str:
        """A change made in the day view (a tap, a drag, Replan) goes through the same tools as the assistant."""
        if self.conv_manager is None:
            return ""
        from thirties_core.plan_tools import NOT_DONE
        from thirties_core.plan_log import caused_by
        self._local_changes += 1
        with caused_by("Day page"):  # the plan change log says "Day page drag" etc. (plan_log.py)
            result = self.conv_manager.execute_tool(name, args)
        if result.startswith(NOT_DONE):
            self._toast(result.split(":", 1)[1].split(". Ask the user")[0].strip())
        self.day_view.refresh_plan(self.conv_manager.day_plan)
        self._push_sync()
        return result

    def _on_replanned(self, result: str) -> None:
        from thirties_core.plan_tools import NOT_DONE
        if result.startswith(NOT_DONE):
            return
        missed = next((line.split(": ", 1)[1] for line in result.splitlines()
                       if line.startswith("Doesn't fit")), "")
        toast = Adw.Toast(title="Replanned the rest of today" + (f". Didn't fit: {missed.split('.')[0]}" if missed else ""),
                          timeout=8, button_label="Undo")
        toast.connect("button-clicked", lambda *_: self._run_day_tool("undo_redraw", {}))
        self.toast_overlay.add_toast(toast)

    def _push_sync(self) -> None:
        if self.config.sync.enabled and self.config.sync.auto_push_on_change:
            try:
                self.sync_engine.push()
            except Exception as e:
                logger.debug("Auto-push sync failed: %s", e)

    # --- chat visibility ---

    def _on_chat_visibility_changed(self, split_view, _param) -> None:
        if split_view.get_show_sidebar():
            GLib.idle_add(self.chat_panel.focus_input)
        elif self.chat_panel.get_focus_child() is not None:
            self.set_focus(None)  # don't leave focus inside the hidden chat

    # --- profile & memory ---

    def _mention_outdated_memory(self) -> bool:
        """At startup: memory notes that work around since-fixed bugs are listed for the user (never deleted)."""
        try:
            from thirties_core.memory_audit import to_review
            count = len(to_review(self.scheduler.state_db))
        except Exception as e:
            logger.warning("Checking memory notes failed: %s", e)
            return False
        if count:
            toast = Adw.Toast(title=f"{count} memory note{'s look' if count > 1 else ' looks'} outdated",
                              button_label="Review", timeout=8)
            toast.connect("button-clicked", lambda *_: self._show_profile_dialog(page="memory"))
            self.toast_overlay.add_toast(toast)
        return False

    def _show_profile_dialog(self, page: str = "") -> None:
        services = self.conv_manager.tools.services if self.conv_manager else None
        from thirties_core.lists import ListCatalog
        engine = self.joplin_engine
        dialog = ProfileMemoryDialog(
            profile=services.profile if services else None,
            memory=services.memory if services else None,
            on_changed=self._push_sync,
            lists=ListCatalog(self.scheduler.state_db) if self.scheduler else None,
            load_lists=lambda: (engine.fetch_active_notes(), engine.fetch_tasks()) if engine else ([], []),
            routines=lambda: (self.conv_manager.tools.assistant.routines
                              if self.conv_manager and self.conv_manager.tools.assistant else None),
        )
        if page:
            dialog.stack.set_visible_child_name(page)
        dialog.present(self)

    # --- navigation ---

    def _on_prev_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date - timedelta(days=1))

    def _on_next_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date + timedelta(days=1))

    def _get_active_diurnal_date(self) -> date:
        gen = self.config.general
        return get_diurnal_date(
            lat=gen.latitude,
            lon=gen.longitude,
            tz_name=gen.timezone,
        )

    def _on_today_clicked(self, _btn: Gtk.Button) -> None:
        self.active_date = self._get_active_diurnal_date()
        self._load_day(self.active_date)

    def _on_calendar_day_selected(self, calendar: Gtk.Calendar) -> None:
        gdt = calendar.get_date()
        selected_date = date(gdt.get_year(), gdt.get_month(), gdt.get_day_of_month())
        if selected_date != self.current_date:
            self._load_day(selected_date)
            self.calendar_popover.popdown()
