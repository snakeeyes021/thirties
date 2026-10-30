"""Render the Timeline page to PNGs (light and dark, Day and Week) for review, with a made-up day on a temp database.

    /usr/bin/python3 -m tests.timeline_screenshot [out_dir]   (default docs/eval_reports/ui/, gitignored)

Needs GTK 4, libadwaita and a display; never touches the user's data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from tests.fixtures import DAY, at, event, make_scheduler  # noqa: E402
from thirties_core.astronomy import set_debug_time  # noqa: E402
from thirties_core.models import EventSpan, SpanCategory, SpanStatus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _pump(seconds: float = 0.6) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def _day():
    sched = make_scheduler([event("standup", "10:00", "10:30", "Team standup")])
    spans = [
        ("kitties", "Kitties, caffeinate, water, breakfast", "07:00", "07:30", SpanStatus.DONE),
        ("shower", "Throne, shower, PT, brush teeth", "07:30", "08:00", SpanStatus.DONE),
        ("email", "Email back GitHub", "08:00", "08:30", SpanStatus.UNKNOWN),
        ("lunch", "Lunch", "12:00", "12:30", SpanStatus.DONE),
        ("call", "Call the ENT", "12:00", "12:30", SpanStatus.PLANNED),
        ("dev", "Torquio dev", "16:00", "17:30", SpanStatus.PLANNED),
        ("cats", "Cats' dinner", "17:00", "17:30", SpanStatus.PLANNED),
        ("dinner", "Dinner", "18:30", "19:30", SpanStatus.PLANNED),
        ("audio", "Audiobook", "18:30", "19:30", SpanStatus.PLANNED),
        ("laundry", "Laundry", "21:00", "21:30", SpanStatus.PLANNED),
    ]
    for sid, title, start, end, status in spans:
        sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=status))
    plan, _ = sched.build_day_plan(DAY)
    plan.spans.append(EventSpan("cal-r", "Rehearsal (Sarah)", at("19:30"), at("21:00"), SpanCategory.CALENDAR,
                                source="calendar", shown_only=True))
    return sched, plan


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "eval_reports" / "ui"
    out.mkdir(parents=True, exist_ok=True)
    pkg = tempfile.mkdtemp(prefix="thirties_shot_pkg_")
    os.symlink(ROOT / "src", os.path.join(pkg, "thirties"))
    sys.path.insert(0, pkg)
    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(str(ROOT / "src" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    from thirties.views.day_view import DayView
    from thirties_core.timeline_layout import week_days

    set_debug_time(at("15:40"))
    sched, plan = _day()
    written = []
    try:
        for scheme, name in ((Adw.ColorScheme.FORCE_LIGHT, "light"), (Adw.ColorScheme.FORCE_DARK, "dark")):
            Adw.StyleManager.get_default().set_color_scheme(scheme)
            for tab, height in (("day", 1640), ("week", 760)):
                view = DayView()
                view.set_actions(lambda *_: "", None)
                page = view.timeline
                page.threaded_week = False
                page.load_week = lambda anchor, days: (
                    [plan if d == DAY else sched.build_day_plan(d)[0] for d in days],
                    [{"item_id": "door", "title": "Fix the door", "list": "Home"},
                     {"item_id": "paint", "title": "Paint the hall", "list": "Home"},
                     {"item_id": "tax", "title": "File the taxes", "list": "Admin"}])
                view.set_today(DAY)
                view.refresh_plan(plan)
                view.stack.set_visible_child_name("timeline")
                page.stack.set_visible_child_name(tab)
                win = Adw.ApplicationWindow(content=view, default_width=640, default_height=height)
                win.present()
                _pump()
                paintable = Gtk.WidgetPaintable.new(win.get_content())
                w, h = win.get_content().get_width(), win.get_content().get_height()
                snap = Gtk.Snapshot()
                bg = Adw.StyleManager.get_default().get_dark()
                rgba = Gdk.RGBA()
                rgba.parse("#222226" if bg else "#fafafb")
                from gi.repository import Graphene
                snap.append_color(rgba, Graphene.Rect().init(0, 0, w, h))
                paintable.snapshot(snap, w, h)
                node = snap.to_node()
                texture = win.get_renderer().render_texture(node, None)
                path = out / f"timeline_{tab}_{name}.png"
                texture.save_to_png(str(path))
                written.append(path)
                win.destroy()
                _pump(0.1)
    finally:
        set_debug_time(None)
        shutil.rmtree(pkg, True)
    for p in written:
        print(p)
    _ = week_days, timedelta
    return 0


if __name__ == "__main__":
    sys.exit(main())
