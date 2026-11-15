"""Render the Timeline's Month and Year tabs (and the Week, for its names) to PNGs for review, light theme, at a
desktop width and at a phone's (360 px), with an invented October on a temp database.

    python3 tests/headless.py -m tests.period_screenshot [out_dir]   (default docs/eval_reports/ui/, gitignored)

The view is sized by hand inside a Gtk.Fixed (the headless session's compositor gives every window the whole
output), grown to its full height, and drawn whole. Needs GTK 4 and libadwaita, and runs only in a private headless
session (tests/headless.py), never on the user's desktop; never touches the user's data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from tests.fixtures import DAY, TZ, at, make_scheduler  # noqa: E402
from thirties_core.astronomy import set_debug_time  # noqa: E402
from thirties_core.calendar_engine import CalendarEvent  # noqa: E402
from thirties_core.horizons import HorizonPlans, period_of  # noqa: E402
from thirties_core.models import EventSpan, SpanCategory, SpanStatus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DONE, PLANNED, MOOT, SKIPPED = SpanStatus.DONE, SpanStatus.PLANNED, SpanStatus.UNKNOWN, SpanStatus.SKIPPED

# An invented October 2026 (DAY is Thursday the 15th): (day, title, start, minutes, status)
ITEMS = [
    (1, "Call the plumber", "09:30", 30, DONE), (1, "Spanish practice", "19:00", 30, DONE),
    (2, "Morning run", "07:30", 30, DONE), (2, "Pick up the parcel", "17:30", 30, DONE),
    (3, "Shed clear-out", "10:00", 90, DONE), (3, "Bake bread", "15:00", 120, DONE),
    (5, "Morning run", "07:30", 30, DONE), (5, "Spanish practice", "19:00", 30, DONE),
    (6, "Renew the library books", "12:30", 30, MOOT),
    (7, "Morning run", "07:30", 30, SKIPPED), (7, "Garden app dev", "19:00", 90, DONE),
    (8, "Spanish practice", "19:00", 30, DONE), (9, "Morning run", "07:30", 30, DONE),
    (10, "Shed clear-out", "10:00", 90, DONE), (10, "Farmers market", "09:00", 60, DONE),
    (10, "Quiz night", "20:00", 120, DONE),
    (12, "Spanish practice", "19:00", 30, DONE), (12, "**Shelf brackets: measure the alcove** (see sketch)",
                                                   "18:00", 30, DONE),
    (13, "Morning run", "07:30", 30, DONE), (14, "Garden app dev", "19:00", 90, DONE),
    (15, "Lunch", "12:00", 30, DONE), (15, "Call the garage", "12:30", 30, DONE),
    (15, "Garden app dev", "16:00", 90, PLANNED), (15, "Dinner", "18:30", 60, PLANNED),
    (16, "Morning run", "07:30", 30, PLANNED), (17, "Bake bread", "14:00", 120, PLANNED),
    (17, "Repot the fern", "11:00", 30, PLANNED),
    (19, "Spanish practice", "19:00", 30, PLANNED), (20, "Morning run", "07:30", 30, PLANNED),
    (22, "Spanish practice", "19:00", 30, PLANNED), (23, "Morning run", "07:30", 30, PLANNED),
    (24, "Shed clear-out", "10:00", 90, PLANNED), (24, "Clean the gutters", "14:00", 60, PLANNED),
    (26, "Garden app dev", "19:00", 90, PLANNED), (28, "Write the garden plan", "19:00", 60, PLANNED),
    (31, "Carve the pumpkin", "17:00", 60, PLANNED), (31, "Hand out sweets", "18:00", 120, PLANNED),
]
# Calendar events: (day, title, start, minutes)
CALENDAR = [(6, "Dentist", "14:00", 60), (15, "Team standup", "10:00", 30), (21, "Book club", "19:30", 90),
            (27, "Haircut", "17:00", 30), (29, "Parent evening", "18:00", 60)]
# Earlier months, for the year's counts: (month, day, title, status)
EARLIER = [(7, 4, "Paint the fence", DONE), (7, 18, "Bake bread", DONE), (8, 2, "Morning run", DONE),
           (8, 9, "Clear the gutters", DONE), (8, 21, "Quiz night", DONE), (9, 5, "Shed clear-out", DONE),
           (9, 12, "Farmers market", DONE), (9, 19, "Bake bread", DONE), (9, 26, "Morning run", DONE),
           (11, 7, "Plant the bulbs", PLANNED), (11, 14, "Winter tyres on", PLANNED)]


def _oct(day: int, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(date(2026, 10, day), clock(h, m), tzinfo=TZ)


def invented():
    events = [CalendarEvent(id=f"cal{i}", summary=title, start_dt=_oct(d, s), end_dt=_oct(d, s) + timedelta(minutes=n))
              for i, (d, title, s, n) in enumerate(CALENDAR)]
    sched = make_scheduler(events)
    db = sched.state_db
    for i, (d, title, s, n, status) in enumerate(ITEMS):
        start = _oct(d, s)
        db.upsert_span(EventSpan(f"s{i}", title, start, start + timedelta(minutes=n), SpanCategory.ACTIVITY,
                                 status=status))
    for i, (m, d, title, status) in enumerate(EARLIER):
        start = datetime.combine(date(2026, m, d), clock(10), tzinfo=TZ)
        db.upsert_span(EventSpan(f"e{i}", title, start, start + timedelta(hours=1), SpanCategory.ACTIVITY,
                                 status=status))
    plans = HorizonPlans(db)

    def pin(item, title, level, when, slot="", sessions=0, minutes=0, list_title="Home"):
        p = period_of(level, when)
        plans.put(item, title, "n_home", list_title, p, slot)
        if sessions:
            plans.set_effort(f"{p.key}|{item}", sessions, minutes)

    pin("t_shed", "Shed clear-out", "month", DAY, sessions=4, minutes=90)
    pin("t_spanish", "Spanish practice", "month", DAY, sessions=8, minutes=30, list_title="Learning")
    pin("t_frames", "Hang the frames", "month", DAY)
    pin("t_door", "Fix the door", "week", DAY, slot=(DAY + timedelta(days=1)).isoformat())
    pin("t_tax", "File the taxes", "week", DAY, list_title="Admin")
    pin("t_paint", "Paint the hall", "week", date(2026, 10, 19), slot="2026-10-25")
    pin("t_bulbs", "Plant the bulbs", "month", date(2026, 11, 1))
    pin("t_tyres", "Winter tyres on", "month", date(2026, 11, 1), list_title="Car")
    pin("t_cards", "Holiday cards", "month", date(2026, 12, 1))
    pin("t_wreath", "Make a wreath", "month", date(2026, 12, 1))
    pin("t_lights", "Put up the lights", "month", date(2026, 12, 1))
    pin("t_pantry", "Sort the pantry", "month", date(2026, 9, 1))
    pin("t_chords", "Learn ten guitar chords", "year", DAY, list_title="Learning")
    pin("t_garden", "Write the garden plan", "year", DAY, slot="2026-10-01", sessions=6, minutes=60)
    pin("t_bike", "Service the bike", "year", DAY, slot="2026-07-01")
    return sched


def _pump(seconds: float = 0.5) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def _scrolled(widget):
    """The scrolled window showing the visible tab's content."""
    out = []

    def walk(w):
        if isinstance(w, Gtk.ScrolledWindow) and w.get_mapped():
            out.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()
    walk(widget)
    return out[-1] if out else None


def shoot(win, view, width: int, path: Path) -> Path:
    """Size the view to ``width`` and to its whole height (its scrolled content shown in full), and draw it."""
    height = 900
    for _ in range(4):
        view.set_size_request(width, height)
        _pump(0.4)
        scroll = _scrolled(view)
        if scroll is None:
            break
        adj = scroll.get_vadjustment()
        extra = adj.get_upper() - adj.get_page_size()
        if extra <= 1:
            break
        height += int(extra) + 4
    paintable = Gtk.WidgetPaintable.new(view)
    w, h = view.get_width(), view.get_height()
    snap = Gtk.Snapshot()
    rgba = Gdk.RGBA()
    rgba.parse("#fafafb")
    snap.append_color(rgba, Graphene.Rect().init(0, 0, w, h))
    paintable.snapshot(snap, w, h)
    texture = win.get_renderer().render_texture(snap.to_node(), None)
    texture.save_to_png(str(path))
    return path


def main() -> int:
    from tests.test_gui import isolated
    if not isolated():
        print("Run this through tests/headless.py: it needs a private headless session.", file=sys.stderr)
        return 2
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "eval_reports" / "ui"
    out.mkdir(parents=True, exist_ok=True)
    pkg = tempfile.mkdtemp(prefix="thirties_shot_pkg_")
    os.symlink(ROOT / "src", os.path.join(pkg, "thirties"))
    sys.path.insert(0, pkg)
    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(str(ROOT / "src" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    from thirties.views.day_view import DayView
    from thirties_core.period_summary import load_month, load_year

    set_debug_time(at("15:40"))
    sched = invented()
    plan, _ = sched.build_day_plan(DAY)
    written = []
    try:
        for tab, widths in (("month", (960, 360)), ("year", (960, 360)), ("week", (960,))):
            for width in widths:
                view = DayView()
                view.set_actions(lambda *_: "", None)
                page = view.timeline
                page.threaded_week = False
                page.load_month = lambda m: load_month(sched, m, DAY)
                page.load_year = lambda y: load_year(sched, y, DAY)
                page.load_week = lambda anchor, days: ([plan if d == DAY else sched.build_day_plan(d)[0]
                                                        for d in days], [])
                view.set_today(DAY)
                view.refresh_plan(plan)
                view.stack.set_visible_child_name("timeline")
                page.stack.set_visible_child_name(tab)
                fixed = Gtk.Fixed()
                fixed.put(view, 0, 0)
                win = Adw.ApplicationWindow(content=fixed, default_width=width, default_height=900)
                win.present()
                _pump()
                name = f"timeline_{tab}_light" + ("_narrow" if width < 600 else "") + ".png"
                written.append(shoot(win, view, width, out / name))
                win.destroy()
                _pump(0.1)
    finally:
        set_debug_time(None)
        shutil.rmtree(pkg, True)
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
