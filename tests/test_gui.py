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
    from thirties.views import settings_rows
    from thirties.views.preferences import PreferencesDialog
    from thirties.views.setup import SetupWindow
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

    def test_the_day_page_shows_where_the_day_stands(self) -> None:
        from thirties_core.models import SpanStatus
        done = next(s for s in self.plan.spans if s.id == "research")
        done.status, done.actual_start, done.actual_end = SpanStatus.DONE, at("13:15"), at("14:20")
        self.sched.state_db.upsert_span(done)
        plan, _ = self.sched.build_day_plan(DAY)
        calls = []
        view = DayView(plan)
        view.set_actions(lambda name, args: calls.append((name, args)) or "Redrew the rest of today:", None)
        groups = view.agenda.groups
        self.assertEqual(list(groups), ["behind", "now", "next", "done"])  # what's not marked yet comes first

        def titles(group):
            rows = []

            def walk(w):
                if isinstance(w, Adw.ActionRow):
                    rows.append(w)
                c = w.get_first_child()
                while c is not None:
                    walk(c)
                    c = c.get_next_sibling()
            walk(group)
            return [(r.get_title(), r.get_subtitle() or "") for r in rows]
        self.assertEqual(titles(groups["now"]), [("Free until 8:00 PM", "")])
        self.assertEqual(titles(groups["behind"]), [("Dinner", "planned 6:00 PM – 7:00 PM"),
                                                    ("Audiobook", "planned 6:00 PM – 7:00 PM")])
        self.assertIn(("Movie", "8:00 PM – 9:30 PM"), titles(groups["next"]))
        self.assertEqual(titles(groups["done"]), [("Research", "1:00 PM – 2:30 PM")])  # when it really happened
        early = []

        def find(w):
            if isinstance(w, Gtk.Button) and w.get_tooltip_text() == "Mark Movie done":
                early.append(w)
            c = w.get_first_child()
            while c is not None:
                find(c)
                c = c.get_next_sibling()
        find(groups["next"])
        early[0].emit("clicked")  # done early, from Next
        self.assertEqual(calls[-1][1]["event"], "movie")
        self.assertEqual((calls[-1][0], calls[-1][1]["status"]), ("log_outcome", "done"))
        self.assertNotIn("finished", calls[-1][1])  # done early: Thirties picks the times, on blocks (soak 3)
        view.agenda.replan_btn.emit("clicked")
        self.assertEqual(calls[-1], ("redraw", {}))
        self.assertTrue(view.move_to("movie", at("21:00")))
        self.assertEqual(calls[-1], ("update_event", {"event": "movie", "start": "9:00 PM"}))  # nothing else moves

    def test_items_past_their_time_are_not_assumed_done(self) -> None:
        set_debug_time(at("21:40"))
        view = DayView(self.plan)
        calls = []
        view.set_actions(lambda name, args: calls.append((name, args)) or "", None)
        self.assertIn("behind", view.agenda.groups)
        buttons = []
        def walk(w):
            if isinstance(w, Gtk.Button) and (w.get_tooltip_text() or "").startswith("Mark Movie"):
                buttons.append(w)
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(view.agenda.groups["behind"])
        self.assertEqual([b.get_tooltip_text() for b in buttons], ["Mark Movie done", "Mark Movie skipped"])
        buttons[0].emit("clicked")
        self.assertEqual(calls, [("log_outcome", {"event": "movie", "status": "done"})])

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

    def test_greeting_is_kept_once_the_user_replies(self) -> None:
        panel = ChatPanel(self.manager())
        self.assertEqual(len(self.bubbles(panel)), 1)
        greeting = self.bubbles(panel)[0]
        self.assertEqual(panel.conversation_manager.history(), [])  # not stored until the user replies

        panel.send_user_text("Plan my evening")
        self.assertTrue(_iterate_until(lambda: not panel.is_busy))
        self.assertEqual([r for r, _ in panel.chat_history], ["assistant", "user", "assistant"])

        # Reopening the day shows the stored conversation, greeting included
        panel.set_conversation_manager(self.manager())
        self.assertEqual(self.bubbles(panel)[:2], [greeting, "Plan my evening"])

    def test_a_turn_holds_the_store_lock(self) -> None:
        # The window's background refresh takes the same lock, so it never reloads the manager mid-turn.
        manager = self.manager()
        panel = ChatPanel(manager)
        seen = []
        original = manager.send_user_message

        def send(text):
            seen.append(panel.store_lock.locked())
            return original(text)
        manager.send_user_message = send
        panel.send_user_text("Plan my evening")
        self.assertTrue(_iterate_until(lambda: not panel.is_busy))
        self.assertEqual(seen, [True])
        self.assertFalse(panel.store_lock.locked())

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

    def test_outdated_memory_notes_are_marked_and_can_be_kept(self) -> None:
        from thirties_core.memory_audit import to_review
        manager = ConversationManager(*self._day(), inference_engine=MockInferenceEngine(), active_notes=[])
        services = manager.tools.services
        services.memory.add("Probation items are maybes")
        services.memory.add("Items don't cross block boundaries")
        dialog = ProfileMemoryDialog(services.profile, services.memory)
        flagged = [r for r in dialog._note_rows if r.has_css_class("warning")]
        self.assertEqual([r.get_text() for r in flagged], ["Items don't cross block boundaries"])
        def walk(widget):
            for child in _children(widget):
                yield child
                yield from walk(child)
        keep = next(c for c in walk(flagged[0]) if isinstance(c, Gtk.Button) and c.get_label() == "Keep")
        keep.emit("clicked")
        self.assertEqual(to_review(manager.scheduler.state_db), [])
        self.assertFalse(any(r.has_css_class("warning") for r in dialog._note_rows))
        self.assertEqual(len(services.memory.notes()), 2)  # kept, not deleted

    def test_lists_roles_and_urgency(self) -> None:
        from thirties_core.lists import ListCatalog
        from thirties_core.models import TaskItem
        sched = make_scheduler()
        catalog = ListCatalog(sched.state_db)
        catalog.set_role("n1", "list", "Errands", source="guess")
        catalog.set_urgency("t1", "soon", source="guess")
        notes = [{"id": "n1", "title": "Errands", "source_notebook": "Tasks", "body": ""},
                 {"id": "n2", "title": "Morning routine", "source_notebook": "Tasks", "body": ""}]
        tasks = [TaskItem(id=t, source_notebook="Tasks", title=title, is_checkbox_item=True, parent_note_id="n1",
                          parent_note_title="Errands") for t, title in (("t1", "Call the bank"), ("t2", "Buy stamps"))]
        changes = []
        dialog = ProfileMemoryDialog(None, None, on_changed=lambda: changes.append(1), lists=catalog,
                                     load_lists=lambda: (notes, tasks))
        page = dialog.lists_page
        page.reload()
        errands = page.note_rows["n1"]
        self.assertEqual(errands.get_subtitle(), "List · guessed · 2 items")
        self.assertEqual(page.note_rows["n2"].get_subtitle(), "Not sorted yet")
        errands.set_expanded(True)
        self.assertTrue(page.item_buttons["t1"]["soon"].get_active())  # the guess is the starting value
        page.item_buttons["t1"]["whenever"].set_active(True)
        row = catalog.urgencies()["t1"]
        self.assertEqual((row["urgency"], row["source"]), ("whenever", "user"))
        self.assertEqual(changes, [1])

    def _day(self):
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        return plan, [], pending, sched


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _combo_rows(widget):
    """Every ComboRow under ``widget``, by title."""
    found = {}
    for child in _children(widget):
        if isinstance(child, Adw.ComboRow):
            found[child.get_title()] = child
        found.update(_combo_rows(child))
    return found



@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class SetupAndPreferencesTests(unittest.TestCase):
    def setUp(self) -> None:
        from unittest import mock
        from thirties_core.app_core import AppCore
        from thirties_core.config import ThirtiesConfig
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": tempfile.mkdtemp(prefix="thirties_gui_data_")})
        env.start()
        self.addCleanup(env.stop)
        self.core = AppCore(use_keyring=False, config=ThirtiesConfig())
        self.app = mock.MagicMock()

    def test_setup_saves_choices_applies_them_and_finishes(self):
        done = []
        win = SetupWindow(application=None, core=self.core, on_done=lambda: done.append(True))
        win.app = self.app
        win._finish(None)  # no location yet: refused
        self.assertEqual(done, [])
        win.ctx.set("general.latitude", 51.5)
        win.ctx.set("general.longitude", -0.12)
        win.ctx.set("general.work_days", ["sat", "sun"])
        win._finish(None)
        self.assertEqual(done, [True])
        self.assertTrue(self.core.setup_done)
        self.assertEqual(self.core.settings.get("assistant.brain"), "slm")  # Gemma unless chosen otherwise
        self.assertEqual(self.core.config.general.work_days, ["sat", "sun"])  # applied live
        self.app.request_background_permission.assert_called_once()

    def test_setup_sorts_the_lists_and_shows_its_guesses(self):
        from unittest import mock
        notes = [{"id": "n1", "title": "Errands", "source_notebook": "1. Tasks", "body": "- [ ] Call the bank"},
                 {"id": "n2", "title": "Cheat sheet", "source_notebook": "5. Misc", "body": "# Python"}]
        self.core.joplin_engine = mock.MagicMock()
        self.core.joplin_engine.fetch_active_notes.return_value = notes
        self.core.joplin_engine.fetch_tasks.return_value = []
        sorter = mock.MagicMock()
        sorter.role.side_effect = lambda note: {"n1": "list", "n2": "ignore"}[note["id"]]
        with mock.patch("thirties_core.brains.list_sorter.list_sorter_for", return_value=sorter):
            win = SetupWindow(application=None, core=self.core, on_done=lambda: None)
            page = next(p for p in win._pages if p.get_title() == "Your Lists")
            page.emit("shown")
            self.assertTrue(_iterate_until(lambda: page.status.get_title().startswith("Here's what")))
        self.assertEqual(page.lists.note_rows["n1"].get_subtitle(), "List · guessed")
        self.assertTrue(page.lists.notebook_switches["1. Tasks"].get_active())
        self.assertFalse(page.lists.notebook_switches["5. Misc"].get_active())  # nothing to do in it, it seems
        sorter.urgencies.assert_not_called()  # left for the background

    def test_preferences_has_every_page(self):
        dialog = PreferencesDialog(self.core, app=None)
        self.assertIsNotNone(dialog)
        group = settings_rows.schedule_group(settings_rows.SettingsContext(self.core))
        self.assertIsNotNone(group)

    def test_each_calendar_is_mine_show_only_or_ignored(self):
        from unittest import mock
        self.core.scheduler.calendar_engine = mock.MagicMock()
        self.core.scheduler.calendar_engine.list_calendars.return_value = [
            {"name": "matt@example.com", "uid": "u1", "mine": True, "mode": ""},
            {"name": "Sarah", "uid": "u2", "mine": False, "mode": ""}]
        ctx = settings_rows.SettingsContext(self.core)
        group = settings_rows.calendars_group(ctx)
        rows = {}
        self.assertTrue(_iterate_until(lambda: rows.update(_combo_rows(group)) or len(rows) == 2))
        self.assertEqual(rows["matt@example.com"].get_subtitle(), "Your commitments: planned around")
        self.assertEqual(rows["Sarah"].get_subtitle(), "Thirties asks once per event (once per series if it repeats)")
        rows["Sarah"].set_selected(2)  # Mine, Ask, Show only, Ignore
        self.assertEqual(self.core.settings.get("calendar.modes"), {"sarah": "show"})


if __name__ == "__main__":
    unittest.main()
