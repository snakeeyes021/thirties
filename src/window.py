"""Main application window for Thirties."""

from datetime import date, timedelta
from gi.repository import Adw, Gtk

from thirties_core.config import load_config
from thirties_core.scheduler import DeterministicScheduler
from thirties.views.day_view import DayView


@Gtk.Template(resource_path='/tech/redfoxlabs/Thirties/window.ui')
class ThirtiesWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ThirtiesWindow'

    date_label = Gtk.Template.Child()
    prev_day_btn = Gtk.Template.Child()
    next_day_btn = Gtk.Template.Child()
    main_content_box = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.config = load_config()
        self.scheduler = DeterministicScheduler(self.config)
        self.current_date = date.today()

        self.day_view = DayView()
        self.main_content_box.append(self.day_view)

        self.prev_day_btn.connect("clicked", self._on_prev_day)
        self.next_day_btn.connect("clicked", self._on_next_day)

        self._load_day(self.current_date)

    def _load_day(self, target_date: date) -> None:
        self.current_date = target_date

        today = date.today()
        if target_date == today:
            date_str = f"Today ({target_date.strftime('%b %d')})"
        elif target_date == today - timedelta(days=1):
            date_str = f"Yesterday ({target_date.strftime('%b %d')})"
        elif target_date == today + timedelta(days=1):
            date_str = f"Tomorrow ({target_date.strftime('%b %d')})"
        else:
            date_str = target_date.strftime("%a, %b %d")

        self.date_label.set_text(date_str)

        # Generate plan deterministically
        plan, _ = self.scheduler.build_day_plan(target_date)
        self.day_view.refresh_plan(plan)

    def _on_prev_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date - timedelta(days=1))

    def _on_next_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date + timedelta(days=1))
