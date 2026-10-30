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
from thirties_core.clock import format_clock
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
    from thirties.views.timeline import DayCanvas, PlanHerePopover, TimelinePage


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

    @staticmethod
    def action_rows(group):
        rows = []

        def walk(w):
            if isinstance(w, Adw.ActionRow):
                rows.append((w.get_title(), w.get_subtitle() or ""))
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(group)
        return rows

    def test_moot_ongoing_and_someone_elses_events_display_sensibly(self) -> None:
        from thirties_core.models import SpanStatus
        research = next(s for s in self.plan.spans if s.id == "research")
        research.title, research.status = "Torquio dev", SpanStatus.DONE  # ongoing by its wording: a session
        research.actual_start, research.actual_end = at("13:00"), at("14:00")
        dinner = next(s for s in self.plan.spans if s.id == "dinner")
        dinner.status = SpanStatus.UNKNOWN  # gone moot with nothing said
        self.plan.spans.append(EventSpan("cal-r", "Rehearsal", at("19:00"), at("21:00"), SpanCategory.CALENDAR,
                                         source="calendar", shown_only=True))
        view = DayView(self.plan)  # the timeline drew no card for a moot item before (KeyError)
        done = dict(self.action_rows(view.agenda.groups["done"]))
        self.assertEqual(done["Torquio dev"], "worked on · 1:00 PM – 2:00 PM")
        self.assertEqual(done["Dinner"], "not reported")
        now = self.action_rows(view.agenda.groups["now"])
        self.assertEqual(now[0], ("Free until 8:00 PM", ""))  # someone else's event doesn't make the user busy
        self.assertEqual(now[1], ("Rehearsal", "7:00 PM – 9:00 PM · not yours (shown only)"))
        cards = {w.card.span.title: w for w in view.timeline.canvas.cards}
        self.assertEqual(cards["Torquio dev"].card.status, "Worked on")
        self.assertNotIn("<s>", cards["Torquio dev"].title_label.get_label())  # a session isn't struck through
        self.assertEqual(cards["Dinner"].card.status, "Not reported")
        self.assertNotIn("Rehearsal", cards)  # a quiet note where it starts, never a card
        self.assertEqual([lbl.get_text() for lbl, _ in view.timeline.canvas.notes], ["Rehearsal (not yours)"])

    def test_a_routine_can_be_skipped_three_ways_from_the_day_page(self) -> None:
        from datetime import date
        calls, notices = [], []
        view = DayView()
        choices = [{"how": "today", "label": "Skip today", "back": date(2026, 10, 16)},
                   {"how": "this_time", "label": "Skip this time", "back": date(2026, 10, 27)},
                   {"how": "until", "label": "Skip until…", "back": None}]
        view.set_actions(lambda name, args: calls.append((name, args)) or "Skipped Deep Clean. Next: Tue Oct 27.",
                         on_notice=notices.append,
                         routines=lambda plan: [{"title": "Deep Clean", "state": "On the plan at 6:00 PM",
                                                 "choices": choices}])
        view.refresh_plan(self.plan)
        self.assertIn(("Deep Clean", "On the plan at 6:00 PM"), self.action_rows(view.agenda.groups["routines"]))
        menu = view.agenda.skip_menus["Deep Clean"]
        self.assertEqual(menu.choices["today"].get_tooltip_text(), "Skip today: Back tomorrow")
        self.assertEqual(menu.choices["this_time"].get_tooltip_text(), "Skip this time: Back Tue Oct 27")
        menu.choices["this_time"].emit("clicked")
        self.assertEqual(calls[-1], ("skip_routine", {"routine": "Deep Clean", "how": "this_time"}))
        self.assertEqual(notices, ["Skipped Deep Clean. Next: Tue Oct 27."])
        menu.choices["until"].emit("clicked")  # shows the calendar, two days on
        self.assertTrue(menu.confirm.get_parent().get_visible())
        menu.calendar.set_date(GLib.DateTime.new_local(2026, 10, 19, 0, 0, 0))
        menu.confirm.emit("clicked")
        self.assertEqual(calls[-1], ("skip_routine", {"routine": "Deep Clean", "how": "until", "until": "2026-10-19"}))
        menu.calendar.set_date(GLib.DateTime.new_local(2026, 10, 15, 0, 0, 0))  # not after the day: refused
        menu.confirm.emit("clicked")
        self.assertEqual(len(calls), 2)
        self.assertEqual(notices[-1], "Pick a day after this one.")


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class TimelineTests(unittest.TestCase):
    """The Timeline page (views/timeline.py): Day and Week tabs over thirties_core/timeline_layout.py."""

    def setUp(self) -> None:
        self.sched = make_scheduler([event("lunch", "12:00", "12:30", "Lunch with Sam")])
        _span(self.sched, "dinner", "Dinner", "18:00", "19:00")
        _span(self.sched, "audio", "Audiobook", "18:00", "19:00")
        _span(self.sched, "movie", "Movie", "20:00", "21:30")
        _span(self.sched, "research", "Research", "13:00", "14:00", SpanCategory.TASK)
        self.plan, _ = self.sched.build_day_plan(DAY)
        set_debug_time(at("19:10"))
        self.calls = []
        self.view = DayView()
        self.view.set_actions(lambda name, args: self.calls.append((name, args)) or "", None)
        self.view.refresh_plan(self.plan)
        self.page = self.view.timeline

    def tearDown(self) -> None:
        set_debug_time(None)

    def cards(self):
        return {w.card.span.title: w for w in self.page.canvas.cards}

    def test_it_builds_a_slot_per_thirty_of_the_waking_day(self) -> None:
        from thirties.views import timeline
        self.assertEqual(self.view.stack.get_visible_child_name(), "day")  # the Agenda stays the first page
        self.assertIsNotNone(self.view.stack.get_child_by_name("timeline"))
        self.assertEqual(len(self.page.layout.slots), 32)  # 7 AM to 11 PM, no clock times anywhere
        height = self.page.canvas.measure(Gtk.Orientation.VERTICAL, -1)[0]
        self.assertEqual(height, timeline.TOP * 2 + 32 * timeline.PITCH)
        self.assertEqual(set(self.cards()), {"Lunch with Sam", "Research", "Dinner", "Audiobook", "Movie"})
        self.assertIn("8:00 PM – 9:30 PM", self.cards()["Movie"].get_tooltip_text())  # real times on hover
        win = Gtk.Window(child=self.view, default_width=800, default_height=700)
        self.view.stack.set_visible_child_name("timeline")
        win.present()
        self.assertTrue(_iterate_until(lambda: self.page.canvas.get_width() > 0))
        movie = self.cards()["Movie"]
        self.assertEqual(movie.get_allocated_height(), 3 * timeline.PITCH - timeline.GAP)  # three thirties
        dinner, audio = self.cards()["Dinner"], self.cards()["Audiobook"]
        self.assertAlmostEqual(dinner.get_allocated_width(), audio.get_allocated_width(), delta=1)  # side by side
        win.destroy()

    def test_free_stretches_are_from_now_and_never_inside_work(self) -> None:
        free = [(f.start, f.end) for _b, f in self.page.canvas.frees]
        self.assertEqual(free, [(at("19:00"), at("20:00")), (at("21:30"), at("23:00"))])
        set_debug_time(at("06:00"))  # before the day starts: all of it
        view = DayView(self.plan)
        frees = [f for _b, f in view.timeline.canvas.frees]
        self.assertTrue(frees)
        self.assertFalse(any(f.start < at("16:00") and f.end > at("08:30") for f in frees))
        self.assertTrue(all(s.work for s in view.timeline.layout.slots[3:18]))  # Work is hatched slots

    def test_the_tick_logs_the_outcome_like_the_day_page(self) -> None:
        set_debug_time(at("20:10"))
        self.view.refresh_plan(self.plan, force=True)
        cards = self.cards()
        cards["Movie"].tick.emit("clicked")
        self.assertEqual(self.calls[-1], ("log_outcome", {"event": "movie", "status": "done", "finished": "8:10 PM"}))
        cards["Lunch with Sam"].tick.emit("clicked")  # the user's own event, past: it happened at its time
        self.assertEqual(self.calls[-1], ("log_outcome", {"event": "cal:lunch", "status": "done"}))

    def test_a_done_item_is_struck_through_and_its_tick_filled(self) -> None:
        from thirties_core.models import SpanStatus
        movie = next(s for s in self.plan.spans if s.id == "movie")
        movie.status = SpanStatus.DONE
        view = DayView(self.plan)
        card = {w.card.span.title: w for w in view.timeline.canvas.cards}["Movie"]
        self.assertIn("<s>Movie</s>", card.title_label.get_label())
        self.assertTrue(card.tick.has_css_class("checked"))
        self.assertFalse(card.tick.get_sensitive())
        self.assertTrue(card.has_css_class("done"))

    def test_the_hand_moves_with_the_clock(self) -> None:
        before = self.page.layout.now_row
        set_debug_time(at("19:40"))
        self.view.tick()
        self.assertAlmostEqual(self.page.layout.now_row - before, 1.0)
        self.assertFalse(self.page.layout.now_daylight)  # after sunset: the moon

    def test_plan_something_in_a_free_stretch(self) -> None:
        from thirties_core.plan_tools import PlanTools
        from thirties_core.timeline_layout import stretch_options
        tools = PlanTools(self.plan, [], self.sched)
        self.page.stretch_options = lambda stretch, minutes: stretch_options(tools, self.plan, stretch, minutes,
                                                                             at("19:10"))
        self.page.suggestions = lambda: [{"task_id": "t1", "title": "Call the bank", "why": "due today"}]
        button, evening = self.page.canvas.frees[1]
        win = Gtk.Window(child=self.view, default_width=800, default_height=700)
        self.view.stack.set_visible_child_name("timeline")
        win.present()
        self.assertTrue(_iterate_until(button.get_mapped))
        pop = self.page._plan_here(button, evening)
        self.assertIsInstance(pop, PlanHerePopover)
        self.assertIn("Right after Movie", [c.get_label() for c, _ in pop.choices])
        pop._pick({"task_id": "t1", "title": "Call the bank"})
        pop.blocks.set_value(2)
        pop.choices[-1][0].set_active(True)
        pop.add_btn.emit("clicked")
        name, args = self.calls[-1]
        self.assertEqual(name, "add_event")
        self.assertEqual(args, {"title": "Call the bank", "start": format_clock(pop.choices[-1][1].start),
                                "duration": "60 minutes", "category": "task", "task_id": "t1"})
        win.destroy()

    def test_the_week_shows_seven_days_their_free_time_and_the_weeks_chips(self) -> None:
        from thirties_core.timeline_layout import week_days
        stored = {"units": "blocks"}
        self.page.get_units = lambda: stored["units"]
        self.page.set_units = lambda units: stored.update(units=units)
        self.page.threaded_week = False
        self.page.load_week = lambda anchor, days: ([self.sched.build_day_plan(d)[0] for d in days],
                                                    [{"item_id": "door", "title": "Fix the door", "list": "Home"}])
        self.page.stack.set_visible_child_name("week")
        week = self.page.week
        self.assertEqual([c.day for c in week.layout.columns], week_days(DAY))
        self.assertEqual(week.title.get_text(), "This week")
        self.assertTrue(week.total.get_text().endswith("blocks free"))
        self.assertEqual(week.frees[3].get_text(), "5 blocks free")  # today, from now on
        self.assertEqual(week.frees[0].get_text(), "")  # Monday is over
        self.assertTrue(week.heads[3].has_css_class("accent"))
        chip = week.chip_box.get_first_child()
        self.assertEqual(chip.chip["title"], "Fix the door")
        week.on_pin("door", DAY + timedelta(days=1))  # dropped onto Friday
        self.assertEqual(self.calls[-1], ("pin_to_period", {"task": "door", "when": "2026-10-16"}))
        self.assertTrue(self.page.units_toggle.get_visible())  # the units toggle is on the Week tab
        self.page.units_toggle.set_active_name("hours")
        self.assertEqual(stored["units"], "hours")
        self.assertEqual(week.frees[3].get_text(), "2½ h free")
        self.page.stack.set_visible_child_name("day")
        self.assertFalse(self.page.units_toggle.get_visible())

    def test_work_is_one_hatched_card_under_the_items(self) -> None:
        work = [(lbl.get_text(), c.row, c.rows) for lbl, c in self.page.canvas.work]
        self.assertEqual(work, [("Work", 3, 15)])
        self.assertNotIn("Work", self.cards())  # no tick, not draggable: a card of its own kind

    def present_timeline(self):
        win = Gtk.Window(child=self.view, default_width=800, default_height=700)
        self.view.stack.set_visible_child_name("timeline")
        win.present()
        self.assertTrue(_iterate_until(lambda: self.page.canvas.get_width() > 0 and self.cards()["Movie"].get_mapped()))
        self.addCleanup(win.destroy)
        return win

    def test_drag_moves_and_edges_resize_through_update_event(self) -> None:
        from thirties.views import timeline
        self.present_timeline()
        canvas = self.page.canvas
        movie = self.cards()["Movie"]
        x, y, w, h = canvas.lane_rect(canvas.get_width(), movie.card)
        body = canvas.begin_gesture(x + w / 2, y + timeline.PITCH + 10)  # grabbed by its second thirty
        self.assertEqual((body["mode"], body["grab"]), ("move", 1))
        self.assertEqual(canvas.gesture_call(body, 2 * timeline.PITCH),
                         ("update_event", {"event": "movie", "start": "9:00 PM"}))
        self.assertEqual(canvas.gesture_call(body, -4 * timeline.PITCH, push=True)[1],
                         {"event": "movie", "start": "6:00 PM", "push_later": True})  # Shift: push what follows
        bottom = canvas.begin_gesture(x + w / 2, y + h - 2)
        self.assertEqual(bottom["mode"], "bottom")
        self.assertEqual(canvas.gesture_call(bottom, timeline.PITCH), ("update_event", {"event": "movie",
                                                                                         "end": "10:00 PM"}))
        top = canvas.begin_gesture(x + w / 2, y + 2)
        self.assertEqual(canvas.gesture_call(top, timeline.PITCH)[1],
                         {"event": "movie", "start": "8:30 PM", "end": "9:30 PM"})
        from gi.repository import Graphene
        _ok, point = movie.tick.compute_point(canvas, Graphene.Point().init(5, 5))
        self.assertIsNone(canvas.begin_gesture(point.x, point.y))  # pressing the tick doesn't drag
        lunch = self.cards()["Lunch with Sam"]
        lx, ly, lw, _lh = canvas.lane_rect(canvas.get_width(), lunch.card)
        self.assertIsNone(canvas.begin_gesture(lx + lw / 2, ly + 10))  # calendar events are fixed
        self.assertTrue(lunch.has_css_class("fixed"))

    def test_delete_removes_it_with_undo(self) -> None:
        toasts = []
        self.page.toast = lambda title, undo: toasts.append((title, undo))
        self.present_timeline()
        movie = self.cards()["Movie"]
        pop = self.page.canvas._open_card(movie)  # a tap: its real times, and Remove
        pop.remove_button.emit("clicked")
        self.assertEqual(self.calls[-1], ("remove_event", {"event": "movie"}))
        self.assertEqual(toasts[0][0], "Removed Movie")
        toasts[0][1]()  # Undo
        self.assertEqual(self.calls[-1], ("add_event", {"title": "Movie", "start": "8:00 PM", "end": "9:30 PM",
                                                         "alongside": True}))
        lunch = self.cards()["Lunch with Sam"]
        self.page.canvas.on_delete(lunch.card)
        self.assertEqual(self.calls[-1][0], "add_event")  # a calendar event can't be removed here

    def test_units_follow_the_setting(self) -> None:
        self.page.get_units = lambda: "hours"
        self.view.refresh_plan(self.plan)  # nothing changed in the plan: the units are still read
        self.assertEqual(self.page.units_toggle.get_active_name(), "hours")

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

    def test_messages_show_when_they_arrived(self) -> None:
        manager = self.manager()
        manager.send_user_message("first")
        panel = ChatPanel(manager)
        times = [c.time_text for c in _children(panel.messages_box) if hasattr(c, "time_text")]
        self.assertEqual(times, ["7:10 PM", "7:10 PM"])  # soak friction 2

    def test_more_can_be_sent_while_a_reply_is_on_its_way(self) -> None:
        panel = ChatPanel(self.manager())
        panel.send_user_text("one")
        self.assertTrue(panel.text_view.get_sensitive())
        panel.send_user_text("two")
        self.assertEqual(self.bubbles(panel)[-2:], ["one", "two"])  # shown at once, before the reply
        self.assertTrue(_iterate_until(lambda: not panel.is_busy, timeout=10))
        self.assertEqual([m["role"] for m in panel.conversation_manager.history()],
                         ["assistant", "user", "user", "assistant"])  # one reply for both (A8)

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

    def test_each_weekday_can_differ_and_the_week_can_start_on_sunday(self):
        ctx = settings_rows.SettingsContext(self.core)
        group = settings_rows.week_group(ctx)
        expander, rows = group.days["sat"]
        self.assertEqual(expander.get_subtitle(), "Usual hours")
        self.assertFalse(rows["work_start_thirty"].get_sensitive())  # Saturday isn't a work day
        rows["sleep_end_thirty"].set_selected(18)  # up at 9 on Saturdays
        self.assertEqual(self.core.settings.get("general.week"), {"sat": {"sleep_end_thirty": 18}})
        self.assertEqual(expander.get_subtitle(), "No work · bed 11:00 PM · up 9:00 AM")
        expander, rows = group.days["fri"]
        rows["work_end_thirty"].set_selected(26)  # Fridays end at 1 PM: the last work thirty is 12:30
        self.assertEqual(self.core.settings.get("general.week")["fri"], {"work_end_thirty": 25})
        rows["work_end_thirty"].set_selected(32)  # back to the usual 4 PM: nothing differs
        self.assertNotIn("fri", self.core.settings.get("general.week"))
        first = _combo_rows(group)["Week starts on"]
        first.set_selected(6)
        self.assertEqual(self.core.settings.get("general.week_start"), "sun")
        ctx.flush()
        self.assertEqual(self.core.config.general.week, {"sat": {"sleep_end_thirty": 18}})  # applied live

    def test_free_time_units_in_preferences(self):
        group = settings_rows.timeline_group(settings_rows.SettingsContext(self.core))
        self.assertEqual(group.units_row.get_selected(), 0)  # blocks by default
        group.units_row.set_selected(1)
        self.assertEqual(self.core.settings.get("timeline.free_units"), "hours")

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
