"""Render the Agenda and the Timeline's Day with other people's calendar events (others_events.py) to PNGs, light
and dark, from an invented day on a temp database.

    python3 tests/headless.py -m tests.others_screenshot [out_dir]   (default docs/eval_reports/ui/others/, gitignored)

Runs only in a private headless session (tests/headless.py); never touches the user's data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from tests.fixtures import DAY, at, make_scheduler  # noqa: E402
from thirties_core.astronomy import set_debug_time  # noqa: E402
from thirties_core.calendar_engine import CalendarEvent  # noqa: E402
from thirties_core.models import EventSpan, SpanCategory, SpanStatus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _pump(seconds: float = 0.6) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def _ev(eid, title, start, end, calendar, mine=False, invited=False, status="accepted"):
    begin = at(start)
    sid = f"{eid}@example.org"
    return CalendarEvent(id=f"{sid}@{int(begin.timestamp())}", summary=title, start_dt=begin, end_dt=at(end),
                         calendar_id=f"uid-{calendar.lower()}", calendar_name=calendar, is_primary=mine,
                         is_ambiguous=not mine and not invited, response_status=status, invited=invited,
                         series_id=sid)


def _day():
    events = [
        _ev("dentist", "Dentist", "10:00", "11:00", "Alex", mine=True),
        _ev("bookclub", "Book club", "13:00", "15:00", "Jamie"),
        _ev("pottery", "Pottery class", "18:30", "21:00", "Jamie"),
        _ev("famdinner", "Family dinner", "17:30", "18:30", "Family"),
        _ev("okafor", "Drinks with the Okafors", "21:00", "22:00", "Jamie"),
        _ev("okafor", "Drinks with the Okafors", "21:00", "22:00", "Alex", mine=True),
        _ev("band", "Band practice", "19:00", "22:00", "Robin"),
    ]
    sched = make_scheduler(events)
    from thirties_core.others_events import set_owner
    set_owner(sched.state_db, "Jamie", "my partner")
    for sid, title, start, end, status in (
            ("coffee", "Coffee, feed the fish", "07:00", "07:30", SpanStatus.DONE),
            ("logo", "Logo drafts", "08:30", "10:00", SpanStatus.DONE),
            ("lunch", "Lunch", "12:00", "12:30", SpanStatus.DONE),
            ("invoice", "Send the invoice", "15:00", "15:30", SpanStatus.PLANNED),
            ("rake", "Rake the yard", "16:00", "17:00", SpanStatus.PLANNED)):
        sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=status))
    plan, _ = sched.build_day_plan(DAY)
    return sched, plan


def _scrolled_content(widget):
    """The visible scrolled area's whole content (the window is the compositor's size, the page is longer)."""
    stack = [widget]
    while stack:
        w = stack.pop(0)
        if isinstance(w, Gtk.ScrolledWindow) and w.get_mapped() and w.get_child() is not None:
            child = w.get_child()
            return child.get_child() if isinstance(child, Gtk.Viewport) and child.get_child() else child
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return None


def main() -> int:
    from tests.test_gui import isolated
    if not isolated():
        print("Run this through tests/headless.py: it needs a private headless session.", file=sys.stderr)
        return 2
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "eval_reports" / "ui" / "others"
    out.mkdir(parents=True, exist_ok=True)
    pkg = tempfile.mkdtemp(prefix="thirties_shot_pkg_")
    os.symlink(ROOT / "src", os.path.join(pkg, "thirties"))
    sys.path.insert(0, pkg)
    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(str(ROOT / "src" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    from thirties.views.day_view import DayView
    from gi.repository import Graphene

    set_debug_time(at("14:10"))
    sched, plan = _day()
    written = []
    warm = Adw.ApplicationWindow(content=Gtk.Label(label=""))  # the compositor sizes the first window late
    warm.present()
    _pump(1.5)
    warm.destroy()
    try:
        for scheme, name in ((Adw.ColorScheme.FORCE_LIGHT, "light"), (Adw.ColorScheme.FORCE_DARK, "dark")):
            Adw.StyleManager.get_default().set_color_scheme(scheme)
            for tab, height, narrow in (("day", 1640, False), ("agenda", 1500, False), ("day", 1640, True),
                                        ("agenda", 1500, True)):
                view = DayView()
                view.set_actions(lambda *_: "", None)
                page = view.timeline
                page.threaded_week = False
                page.load_week = lambda anchor, days: ([plan if d == DAY else sched.build_day_plan(d)[0]
                                                        for d in days], [])
                view.set_today(DAY)
                view.refresh_plan(plan)
                if tab == "agenda":
                    view.stack.set_visible_child_name("day")
                else:
                    view.stack.set_visible_child_name("timeline")
                    page.stack.set_visible_child_name(tab)
                content = Adw.Clamp(child=view, maximum_size=360, tightening_threshold=360) if narrow else view
                win = Adw.ApplicationWindow(content=content, default_width=640, default_height=height)
                win.present()
                _pump(1.5)
                target = _scrolled_content(view.agenda.page if tab == "agenda" else page) or win.get_content()
                paintable = Gtk.WidgetPaintable.new(target)
                w, h = target.get_width(), target.get_height()
                for _ in range(5):  # the compositor may size a new window late: wait for the full width
                    if narrow or w >= 640:
                        break
                    _pump(1.0)
                    w, h = target.get_width(), target.get_height()
                snap = Gtk.Snapshot()
                rgba = Gdk.RGBA()
                rgba.parse("#222226" if Adw.StyleManager.get_default().get_dark() else "#fafafb")
                snap.append_color(rgba, Graphene.Rect().init(0, 0, w, h))
                paintable.snapshot(snap, w, h)
                texture = win.get_renderer().render_texture(snap.to_node(), None)
                path = out / (f"others_{tab if tab == 'agenda' else 'timeline_day'}_{name}"
                              + ("_360px" if narrow else "") + ".png")
                texture.save_to_png(str(path))
                written.append(path)
                win.destroy()
                _pump(0.1)
    finally:
        set_debug_time(None)
        shutil.rmtree(pkg, True)
    for p in written:
        print(p)
    _ = datetime
    return 0


if __name__ == "__main__":
    sys.exit(main())
