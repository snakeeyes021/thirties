"""Render the Timeline's Day tab at phone width (360 px) to one PNG, with made-up items that share a block.

    python3 tests/headless.py -m tests.timeline_narrow_screenshot [out.png]   (default docs/eval_reports/ui/timeline_narrow.png)

Needs GTK 4 and libadwaita, and runs only in a private headless session (tests/headless.py), never on the user's
desktop; never touches the user's data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from tests.fixtures import DAY, at, make_scheduler  # noqa: E402
from thirties_core.astronomy import set_debug_time  # noqa: E402
from thirties_core.models import EventSpan, SpanCategory, SpanStatus  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WIDTH = 360


def _pump(seconds: float = 0.6) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def narrow_day():
    """A made-up day where pairs and a trio of items share a block."""
    sched = make_scheduler([])
    spans = [
        ("plants", "Water the plants", "09:00", "09:30", SpanStatus.PLANNED),
        ("car", "Wash the car", "09:00", "09:30", SpanStatus.PLANNED),
        ("tax", "Sort the receipts", "10:00", "11:00", SpanStatus.PLANNED),
        ("dentist", "Call the dentist", "10:00", "10:30", SpanStatus.PLANNED),
        ("bag", "Pack the gym bag", "10:00", "10:30", SpanStatus.PLANNED),
        ("lunch", "Lunch", "12:00", "12:30", SpanStatus.PLANNED),
    ]
    for sid, title, start, end, status in spans:
        sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=status))
    plan, _ = sched.build_day_plan(DAY)
    return sched, plan


def main() -> int:
    from tests.test_gui import isolated
    if not isolated():
        print("Run this through tests/headless.py: it needs a private headless session.", file=sys.stderr)
        return 2
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "eval_reports" / "ui" / "timeline_narrow.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    pkg = tempfile.mkdtemp(prefix="thirties_shot_pkg_")
    os.symlink(ROOT / "src", os.path.join(pkg, "thirties"))
    sys.path.insert(0, pkg)
    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(str(ROOT / "src" / "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    from thirties.views.day_view import DayView

    set_debug_time(at("08:00"))
    sched, plan = narrow_day()
    try:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        view = DayView()
        view.set_actions(lambda *_: "", None)
        view.set_today(DAY)
        view.refresh_plan(plan)
        view.stack.set_visible_child_name("timeline")
        view.timeline.stack.set_visible_child_name("day")
        # a window under the headless compositor fills the screen, so the phone width is a clamp around the view
        clamp = Adw.Clamp(maximum_size=WIDTH, tightening_threshold=WIDTH, child=view)
        win = Adw.ApplicationWindow(content=clamp, default_width=WIDTH, default_height=760)
        win.present()
        for _ in range(150):  # until the compositor has given the window its size and the first frames are drawn
            _pump(0.1)
            if view.get_height() >= 700:
                break
        _pump(0.8)
        if view.get_height() < 700:
            print("The window never reached its full height.", file=sys.stderr)
            return 1
        content = view
        w, h = content.get_width(), content.get_height()
        snap = Gtk.Snapshot()
        rgba = Gdk.RGBA()
        rgba.parse("#fafafb")
        snap.append_color(rgba, Graphene.Rect().init(0, 0, w, h))
        Gtk.WidgetPaintable.new(content).snapshot(snap, w, h)
        win.get_renderer().render_texture(snap.to_node(), None).save_to_png(str(out))
        print(out)
        win.destroy()
        _pump(0.1)
    finally:
        set_debug_time(None)
        shutil.rmtree(pkg, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
