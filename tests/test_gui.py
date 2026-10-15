"""Headless checks of the GTK desktop client's widgets.

Skipped when GTK 4 / libadwaita or a display is unavailable. The client package
is imported as ``thirties`` (as installed by meson) through a temporary symlink
to ``src/``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory

SRC = Path(__file__).resolve().parent.parent / "src"


def _load_gtk():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError):
        return None
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        return None
    from gi.repository import Adw
    Adw.init()
    return Adw, GLib, Gtk


GTK = _load_gtk()
if GTK:
    Adw, GLib, Gtk = GTK
    _pkg_dir = tempfile.mkdtemp(prefix="thirties_gui_pkg_")
    os.symlink(SRC, os.path.join(_pkg_dir, "thirties"))
    atexit.register(shutil.rmtree, _pkg_dir, True)
    sys.path.insert(0, _pkg_dir)
    from thirties.views.chat_panel import ChatPanel, persist_conversation
    from thirties.views.day_view import DayView
    from thirties.views.profile_dialog import ProfileMemoryDialog
    from thirties.widgets.block_widget import GroupedBlockWidget


def _span(sched, sid, title, start, end, category=SpanCategory.ACTIVITY):
    sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), category))


def _iterate_until(predicate, timeout=5.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.01)
    while ctx.pending():
        ctx.iteration(False)
    return predicate()


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class DayViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sched = make_scheduler([event("lunch", "12:00", "12:30", "Lunch with Sam")])
        _span(self.sched, "dinner", "Dinner", "18:00", "19:00")
        _span(self.sched, "audio", "Audiobook", "18:00", "19:00")
        _span(self.sched, "movie", "Movie", "20:00", "21:30")
        _span(self.sched, "research", "Research", "13:00", "14:00", SpanCategory.TASK)
        self.plan, _ = self.sched.build_day_plan(DAY)
        set_debug_time(at("19:10"))

    def tearDown(self) -> None:
        set_debug_time(None)

    def rows(self, view):
        return [(r.title_label.get_text(), isinstance(r, GroupedBlockWidget), len(getattr(r, "blocks", [None])))
                for r in view._rows]

    def test_multi_thirty_items_collapse_into_cards(self) -> None:
        view = DayView(self.plan)
        rows = self.rows(view)
        self.assertIn(("Dinner + Audiobook", True, 2), rows)
        self.assertIn(("Movie", True, 3), rows)
        self.assertIn(("Research", True, 2), rows)
        self.assertIn(("Lunch with Sam", False, 1), rows)
        self.assertIn(("Sleep", True, 16), rows)
        # Work is split around lunch and research, each part saying what it belongs to
        work = [r for r in view._rows if r.title_label.get_text() == "Work"]
        self.assertGreaterEqual(len(work), 3)
        grouped_work = [r for r in work if isinstance(r, GroupedBlockWidget)]
        self.assertTrue(all(r.context and r.context.startswith("Part of Work") for r in grouped_work))
        # Open thirties stay individual
        self.assertTrue(all(not g for t, g, _ in rows if t.startswith("Open")))

    def test_now_highlight_and_tallies_follow_the_clock(self) -> None:
        view = DayView(self.plan)
        current = [r for r in view._rows if r.is_current]
        self.assertEqual([r.title_label.get_text() for r in current], ["Open Dark Thirty"])
        self.assertTrue(current[0].has_css_class("block-current"))
        before = view.dark_pill.get_text()

        set_debug_time(at("20:40"))
        view.tick()
        current = [r for r in view._rows if r.is_current]
        self.assertEqual([r.title_label.get_text() for r in current], ["Movie"])
        self.assertTrue(current[0].now_badge.get_visible())
        self.assertNotEqual(view.dark_pill.get_text(), before)
        counts = self.plan.availability(at("20:40"))
        self.assertEqual(view.dark_pill.get_text(), f"☾ Dark: {counts['dark_open']}/{counts['dark_total']} available")

    def test_other_days_count_every_thirty(self) -> None:
        set_debug_time(at("12:00") + timedelta(days=3))
        view = DayView(self.plan)
        counts = self.plan.availability()
        self.assertEqual(view.daylight_pill.get_text(),
                         f"☼ Daylight: {counts['daylight_open']}/{counts['daylight_total']} available")
        self.assertFalse(any(r.is_current for r in view._rows))

    def test_expanded_cards_stay_open_across_rerenders(self) -> None:
        view = DayView(self.plan)
        movie = next(r for r in view._rows if r.title_label.get_text() == "Movie")
        movie._on_toggle(None)
        view.refresh_plan(self.plan, force=True)
        movie = next(r for r in view._rows if r.title_label.get_text() == "Movie")
        self.assertTrue(movie.expanded)


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class ChatPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sched = make_scheduler()
        set_debug_time(at("19:10"))

    def tearDown(self) -> None:
        set_debug_time(None)

    def manager(self):
        plan, pending = self.sched.build_day_plan(DAY)
        return ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(), active_notes=[])

    def bubbles(self, panel):
        return [c.text for c in list(_children(panel.messages_box)) if hasattr(c, "text")]

    def test_greeting_only_when_empty_and_turns_persist(self) -> None:
        panel = ChatPanel(self.manager())
        self.assertEqual(len(self.bubbles(panel)), 1)
        self.assertEqual(panel.chat_history, [])  # the greeting is not part of the transcript

        panel.send_user_text("Plan my evening")
        self.assertTrue(_iterate_until(lambda: not panel.is_busy))
        self.assertEqual([r for r, _ in panel.chat_history], ["user", "assistant"])

        # Reopening the day shows the stored conversation instead of a greeting
        panel.set_conversation_manager(self.manager())
        self.assertEqual(self.bubbles(panel)[0], "Plan my evening")

    def test_new_conversation_and_switching_survive_refresh(self) -> None:
        manager = self.manager()
        manager.send_user_message("first")
        persist_conversation(manager)
        panel = ChatPanel(manager)
        panel.new_conversation()
        self.assertEqual(manager.history(), [])
        manager.send_user_message("second")
        persist_conversation(manager)

        convs = manager.conversations_for_day()
        self.assertEqual(len(convs), 2)
        panel.switch_conversation(convs[0].id)
        manager.refresh()  # would move to the latest conversation
        panel.after_store_refresh()
        self.assertEqual(manager.conversation.id, convs[0].id)
        self.assertEqual(self.bubbles(panel)[0], "first")


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class ProfileDialogTests(unittest.TestCase):
    def test_edit_profile_and_memory(self) -> None:
        manager = ConversationManager(*self._day(), inference_engine=MockInferenceEngine(), active_notes=[])
        services = manager.tools.services
        services.memory.add("Probation items are maybes")
        services.memory.add("Wrong note")
        changes = []
        dialog = ProfileMemoryDialog(services.profile, services.memory, on_changed=lambda: changes.append(1))

        buf = dialog.profile_buffer
        buf.set_text(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                     .replace("## Preferences\n(empty)", "## Preferences\n- Deep work before lunch"))
        dialog._on_save_profile()
        self.assertEqual(services.profile.sections()["Preferences"], "- Deep work before lunch")

        dialog._note_rows[0].set_text("Probation items are maybes; never push them")
        dialog._on_note_apply(dialog._note_rows[0])
        dialog._on_note_delete(None, dialog._note_rows[1])
        self.assertEqual([n.text for n in services.memory.notes()], ["Probation items are maybes; never push them"])
        self.assertEqual(len(changes), 3)

    def _day(self):
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        return plan, [], pending, sched


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


if __name__ == "__main__":
    unittest.main()
