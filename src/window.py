"""Main application window for Thirties."""

from datetime import date, timedelta
from gi.repository import Adw, GLib, Gtk

from thirties_core.config import load_config
from thirties_core.conversation import ConversationManager
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.scheduler import DeterministicScheduler
from thirties.views.chat_panel import ChatPanel
from thirties.views.day_view import DayView


@Gtk.Template(resource_path='/tech/redfoxlabs/Thirties/window.ui')
class ThirtiesWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ThirtiesWindow'

    prev_day_btn = Gtk.Template.Child()
    next_day_btn = Gtk.Template.Child()
    calendar_menu_btn = Gtk.Template.Child()
    calendar_popover = Gtk.Template.Child()
    date_calendar = Gtk.Template.Child()
    view_switcher_title = Gtk.Template.Child()
    view_stack = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.config = load_config()
        self.scheduler = DeterministicScheduler(self.config)
        self.joplin_engine = JoplinEngine(self.config)
        self.current_date = date.today()

        # Ingest backlog tasks from Joplin
        self.backlog_tasks = self.joplin_engine.fetch_tasks()
        self.scheduler.attach_history_to_tasks(self.backlog_tasks)

        # Inference backend
        litert_engine = LiteRTInferenceEngine(self.config)
        self.inference_engine = litert_engine if litert_engine.is_available() else MockInferenceEngine()

        # 1. Schedule View (DayView)
        self.day_view = DayView()
        self.view_stack.add_titled_with_icon(
            self.day_view,
            "schedule",
            "Schedule",
            "x-office-calendar-symbolic",
        )

        # 2. Assistant Chat View (ChatPanel)
        initial_plan, ambiguous_events = self.scheduler.build_day_plan(self.current_date)
        self.conv_manager = ConversationManager(
            day_plan=initial_plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous_events,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
        )

        self.chat_panel = ChatPanel(
            conversation_manager=self.conv_manager,
            on_plan_updated=self._on_plan_updated,
        )
        self.view_stack.add_titled_with_icon(
            self.chat_panel,
            "chat",
            "Assistant Chat",
            "chat-symbolic",
        )

        # Event connections
        self.prev_day_btn.connect("clicked", self._on_prev_day)
        self.next_day_btn.connect("clicked", self._on_next_day)
        self.date_calendar.connect("day-selected", self._on_calendar_day_selected)

        self._load_day(self.current_date)

    def _load_day(self, target_date: date) -> None:
        self.current_date = target_date

        # Sync GtkCalendar selected day
        try:
            gdt = GLib.DateTime.new_local(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            self.date_calendar.select_day(gdt)
        except Exception:
            pass

        # Build plan for target date
        plan, ambiguous = self.scheduler.build_day_plan(target_date)
        self.day_view.refresh_plan(plan)

        # Update conversational manager for this day
        self.conv_manager = ConversationManager(
            day_plan=plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
        )
        self.chat_panel.set_conversation_manager(
            self.conv_manager,
            on_plan_updated=self._on_plan_updated,
        )

    def _on_plan_updated(self, plan) -> None:
        """Triggered when tool calls (allocate block, resolve event) modify the plan."""
        self.day_view.refresh_plan(plan)

    def _on_prev_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date - timedelta(days=1))

    def _on_next_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date + timedelta(days=1))

    def _on_calendar_day_selected(self, calendar: Gtk.Calendar) -> None:
        gdt = calendar.get_date()
        selected_date = date(gdt.get_year(), gdt.get_month(), gdt.get_day_of_month())
        if selected_date != self.current_date:
            self._load_day(selected_date)
            self.calendar_popover.popdown()
