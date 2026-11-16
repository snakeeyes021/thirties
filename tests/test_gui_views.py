"""Headless checks of the views' widgets: the Timeline's ticks, marks, Week and Year, the
Agenda's new rows, the chat box, all conversations, effort and the list-structure settings.

Run with ``python3 tests/headless.py`` (a private headless session); skipped anywhere else. Test data is invented.
"""

from __future__ import annotations

import unittest
import unittest.mock
from datetime import date, timedelta

from tests.fixtures import DAY, at, event, make_scheduler
from tests.test_gui import GTK, _iterate_until, _span
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

if GTK:
    from tests.test_gui import Adw, GLib, Gtk  # noqa: F401  (the package symlink is set up there)
    from thirties.views.day_view import DayView


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class TimelineLaneTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sched = make_scheduler([event("lunch", "12:00", "12:30", "Lunch with Kim")])
        _span(self.sched, "dinner", "Dinner", "18:00", "19:00")
        _span(self.sched, "movie", "Movie", "20:00", "21:30")
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

    def test_a_not_reported_card_can_be_ticked_from_the_timeline(self) -> None:
        dinner = next(s for s in self.plan.spans if s.id == "dinner")
        dinner.status = SpanStatus.UNKNOWN
        self.view.refresh_plan(self.plan, force=True)
        card = self.cards()["Dinner"]
        self.assertTrue(card.tick.get_sensitive())
        self.assertEqual(card.tick.get_tooltip_text(), "Mark Dinner done")
        card.tick.emit("clicked")
        self.assertEqual(self.calls[-1], ("log_outcome", {"event": "dinner", "status": "done"}))

    def test_the_tick_shows_a_pointing_hand(self) -> None:
        cards = self.cards()
        self.assertEqual(cards["Movie"].tick.get_cursor().get_name(), "pointer")
        dinner = next(s for s in self.plan.spans if s.id == "dinner")
        dinner.status = SpanStatus.DONE
        self.view.refresh_plan(self.plan, force=True)
        self.assertIsNone(self.cards()["Dinner"].tick.get_cursor())  # nothing to tick: no pointer

    def test_the_year_names_its_quarters_by_their_months(self) -> None:
        from thirties.views.timeline import quarter_name
        self.assertEqual([quarter_name(date(2026, m, 1)) for m in (1, 4, 7, 10)],
                         ["January–March", "April–June", "July–September", "October–December"])
        self.assertEqual(quarter_name(date(2026, 8, 1)), "July–September")
        from thirties_core.horizons import HorizonPlans, period_of
        from thirties_core.period_summary import load_year
        plans = HorizonPlans(self.sched.state_db)
        plans.put("t_shed", "Shed clear-out", "n1", "Home", period_of("year", DAY), "2026-10-01")
        self.page.threaded_week = False
        self.page.load_year = lambda y: load_year(self.sched, y, DAY)
        self.page.stack.set_visible_child_name("year")
        self.assertEqual([h for h, _ in self.page.year.efforts.lines], ["October–December"])

    def test_the_week_fits_the_space_it_has(self) -> None:
        from thirties.views import timeline
        week = self.page.week
        self.assertEqual(week.fit_height(2000, 1200), timeline.WEEK_HEIGHT)  # room: names in a thirty's bar
        self.assertEqual(week.fit_height(600, 900), timeline.WEEK_MIN_HEIGHT)  # a short screen: it gives way
        self.assertEqual(week.fit_height(800, 900), 380)  # in between: just what fits (the rest is 420 px)
        self.page.threaded_week = False
        self.page.load_week = lambda anchor, days: ([self.sched.build_day_plan(d)[0] for d in days], [])
        self.view.stack.set_visible_child_name("timeline")
        self.page.stack.set_visible_child_name("week")
        win = Gtk.Window(child=self.view, default_width=683, default_height=690)
        win.present()
        self.assertTrue(_iterate_until(lambda: week.get_vadjustment().get_page_size() > 0))
        _iterate_until(lambda: week.column_height == timeline.WEEK_MIN_HEIGHT, 2)
        self.assertEqual(week.column_height, timeline.WEEK_MIN_HEIGHT)  # a laptop with the chat open
        self.assertTrue(all(a.get_content_height() == timeline.WEEK_MIN_HEIGHT for a in week.areas))
        hadj = week.get_hadjustment()
        self.assertLessEqual(hadj.get_upper() - hadj.get_page_size(), 0)  # never sideways
        win.destroy()

    def test_the_header_arcs_label_steps_aside_from_the_sun(self) -> None:
        from thirties.widgets.solar_arc import MARKER_CLEAR, apex_label_place
        self.assertEqual(apex_label_place(300, 120, None), (300, 0.5))
        self.assertEqual(apex_label_place(300, 120, 100), (300, 0.5))  # the sun far off: centred
        self.assertEqual(apex_label_place(300, 120, 310), (310 - MARKER_CLEAR, 1.0))  # past the top: to its left
        self.assertEqual(apex_label_place(300, 120, 290), (290 + MARKER_CLEAR, 0.0))  # before it: to its right

    def test_a_cards_popover_takes_the_users_effort_estimate(self) -> None:
        kept = {"t_movie": (5.0, "thirties")}
        self.page.estimate_of = lambda task_id: kept.get(task_id, (0.0, "hours"))
        movie = next(s for s in self.plan.spans if s.id == "movie")
        movie.task_id = "t_movie"
        self.view.refresh_plan(self.plan, force=True)
        cards = self.cards()
        editor = self.page.canvas._open_card(cards["Movie"]).extra
        self.assertEqual((editor.amount.get_value(), editor.chosen_unit()), (5, "thirties"))  # shown as "blocks"
        editor.unit.set_selected(1)  # hours
        editor.amount.set_value(3)
        editor.save.emit("clicked")
        self.assertEqual(self.calls[-1:], [("set_estimate", {"task": "t_movie", "estimate": "3 hours"})])
        self.assertEqual(editor.save.get_label(), "Saved")
        editor.unit.set_selected(4)  # points: not the usual unit, so it becomes it first
        editor.amount.set_value(2)
        editor.save.emit("clicked")
        self.assertEqual(self.calls[-2:], [("set_effort_unit", {"unit": "points"}),
                                           ("set_estimate", {"task": "t_movie", "estimate": "2 points"})])
        dinner = self.page.canvas._open_card(cards["Dinner"])
        self.assertFalse(hasattr(dinner, "extra"))  # not an item from the lists: nothing to keep it on
        lunch = self.page.canvas._open_card(cards["Lunch with Kim"])
        self.assertFalse(hasattr(lunch, "extra"))  # a calendar event


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class PreferencesLaneTests(unittest.TestCase):

    def setUp(self) -> None:
        import os
        from unittest import mock
        from thirties_core.app_core import AppCore
        from thirties_core.config import ThirtiesConfig
        from tests.fixtures import temp_dir
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": temp_dir("thirties_gui_data_"),
                                           "THIRTIES_JOPLIN_PEER_HOME": temp_dir("thirties_gui_peer_")})
        env.start()
        self.addCleanup(env.stop)
        self.core = AppCore(use_keyring=False, config=ThirtiesConfig())

    def test_the_list_structure_in_what_to_read(self) -> None:
        import sys
        import types
        from unittest import mock
        from thirties.views import settings_rows
        import thirties_core
        list_settings = types.SimpleNamespace(ONE_OFF_KEY="lists.one_off_notes", DONE_NOTE_KEY="lists.done_note",
                                              ONE_OFF_CHOICES={"item": "", "someday": "", "ignore": ""})
        self.enterContext(mock.patch.dict(sys.modules, {"thirties_core.list_settings": list_settings}))
        self.enterContext(mock.patch.object(thirties_core, "list_settings", list_settings, create=True))
        from thirties_core.lists import ListCatalog
        ListCatalog(self.core.scheduler.state_db).set_role("n_home", "list", "Home")
        notes = [{"id": "n_home", "title": "Home", "body": ""}, {"id": "n_log", "title": "What I did", "body": ""}]
        ctx = settings_rows.SettingsContext(self.core)
        with mock.patch.object(self.core.joplin_engine, "list_notebooks", return_value=["1. Tasks", "Archive"]), \
                mock.patch.object(self.core.joplin_engine, "fetch_active_notes", return_value=notes):
            group = settings_rows.notebooks_group(ctx)
        group.also_row.set_text("Packing list, Garden plan")
        self.assertTrue(_iterate_until(lambda: self.core.settings.get("joplin.whitelist_notes") ==
                                       ["Packing list", "Garden plan"], 3))
        self.assertEqual(group.one_off_row.get_selected(), 0)  # one thing to do, unless they say
        group.one_off_row.set_selected(1)
        self.assertEqual(self.core.settings.get("lists.one_off_notes"), "someday")
        self.assertEqual([c["title"] if c else None for c in group.done_row.choices], [None, "What I did"])  # no list
        group.done_row.set_selected(1)
        self.assertEqual(self.core.settings.get("lists.done_note"), {"id": "n_log", "title": "What I did"})
        group.done_row.set_selected(0)
        self.assertIsNone(self.core.settings.get("lists.done_note"))  # back to the monthly Done notes
        window = mock.MagicMock()
        ctx.parent = window
        group.each_row.emit("activated")
        window.activate_action.assert_called_once_with("win.lists", None)

    def test_without_the_list_settings_only_whats_kept_today_shows(self) -> None:
        import sys
        from unittest import mock
        import thirties_core
        from thirties.views import settings_rows
        saved = thirties_core.__dict__.pop("list_settings", None)  # imported earlier in the run: as if not built in
        try:
            with mock.patch.dict(sys.modules, {"thirties_core.list_settings": None}), \
                    mock.patch.object(self.core.joplin_engine, "list_notebooks", return_value=[]):
                group = settings_rows.notebooks_group(settings_rows.SettingsContext(self.core))
        finally:
            if saved is not None:
                thirties_core.list_settings = saved
        self.assertIsNone(group.one_off_row)
        self.assertIsNone(group.done_row)

    def lane_m(self, notes=()):
        """Tickets, people and engineer_notes, stood in by their own names (they aren't merged here)."""
        import sys
        import types
        from unittest import mock
        modules = {"thirties_core.tickets": types.SimpleNamespace(SETTING="tickets.rush"),
                   "thirties_core.people": types.SimpleNamespace(SETTING="people.nudges"),
                   "thirties_core.engineer_notes": types.SimpleNamespace(
                       EngineerNotes=lambda db: types.SimpleNamespace(notes=lambda: list(notes)))}
        self.enterContext(mock.patch.dict(sys.modules, modules))
        import thirties_core
        for name, module in modules.items():
            self.enterContext(mock.patch.object(thirties_core, name.split(".")[1], module, create=True))

    def test_rush_tickets_and_keep_in_touch_switches(self) -> None:
        from thirties.views import settings_rows
        self.lane_m()
        group = settings_rows.reminders_group(settings_rows.SettingsContext(self.core))
        rush, nudges = group.rows["tickets.rush"], group.rows["people.nudges"]
        self.assertEqual(rush.get_title(), "Rush tickets")
        self.assertEqual(nudges.get_title(), "Nudges to keep in touch")
        self.assertFalse(rush.get_active())  # off unless turned on
        self.assertTrue(nudges.get_active())  # on unless turned off
        rush.set_active(True)
        nudges.set_active(False)
        self.assertIs(self.core.settings.get("tickets.rush"), True)
        self.assertIs(self.core.settings.get("people.nudges"), False)

    def test_without_those_parts_the_reminders_are_hidden(self) -> None:
        import sys
        from unittest import mock
        from thirties.views import settings_rows
        with mock.patch.dict(sys.modules, {"thirties_core.tickets": None, "thirties_core.people": None,
                                           "thirties_core.engineer_notes": None}):
            ctx = settings_rows.SettingsContext(self.core)
            self.assertFalse(settings_rows.reminders_group(ctx).get_visible())
            self.assertFalse(settings_rows.engineer_notes_group(ctx).get_visible())

    def test_the_notes_left_for_the_engineers(self) -> None:
        import types
        from thirties.views import settings_rows
        from thirties.views.engineer_notes_dialog import EngineerNotesDialog
        notes = [types.SimpleNamespace(id="a1", at="2026-11-14T09:05:00-05:00", via="telegram",
                                       text="the check-in came twice", message="note to the engineers: the check-in "
                                       "came twice. Also move lunch to 1", state="open"),
                 types.SimpleNamespace(id="b2", at="2026-11-15T14:10:00-05:00", via="app",
                                       text="the week view is cramped", message="the week view is cramped",
                                       state="open")]
        self.lane_m(notes)
        group = settings_rows.engineer_notes_group(settings_rows.SettingsContext(self.core))
        self.assertTrue(group.get_visible())
        self.assertEqual(group.row.get_subtitle(), "2 waiting to be read")
        dialog = EngineerNotesDialog(notes)
        self.assertEqual([r.get_title() for r in dialog.rows], ["the week view is cramped", "the check-in came twice"])
        self.assertEqual(dialog.rows[0].get_subtitle(), "Sunday, November 15, 2:10 PM, in the app")
        self.assertIn("Saturday, November 14, 9:05 AM, on Telegram\nSaid in: note to the engineers",
                      dialog.rows[1].get_subtitle())
        self.assertEqual(EngineerNotesDialog([]).rows, [])


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class BeforeTodayNarrowTests(unittest.TestCase):
    """At phone width (360 px) a Before today row's title gets the row's width, with its buttons under it, never
    squeezed to a letter a line beside them; on a wide page the buttons stay beside the title."""

    ROWS = {"keep": [{"title": "Return the library books before the weekend", "when": "this week", "at": None,
                      "why": "a week old"}],
            "drop": [{"title": "Watch the meteor shower", "when": "next week", "at": None, "why": "the night is gone"}],
            "restore": ["Repaint the porch railing"]}

    def show(self, width):
        from thirties.views.before_today import BeforeTodayPage
        page = BeforeTodayPage()
        page.run_tool = lambda name, args: ""
        page.rows_provider = lambda: self.ROWS
        page.offers_provider = lambda: [{"task_id": "t_tax", "title": "File the quarterly taxes", "asked": "",
                                         "answer": ""}]
        page.refresh()
        page.set_size_request(width, 700)  # exactly this wide, as the screenshots draw it
        fixed = Gtk.Fixed()
        fixed.put(page, 0, 0)
        win = Gtk.Window(child=fixed, default_width=width, default_height=700)
        win.present()
        self.addCleanup(win.destroy)
        _iterate_until(lambda: page.get_width() > 0 and page.narrow == (width < page.NARROW_BELOW), 3)
        _iterate_until(lambda: False, 0.3)  # the rows laid out again at that width
        return page

    @staticmethod
    def title_label(row):
        """The label showing the row's title, and the row it sits in."""
        found = []

        def walk(w):
            if isinstance(w, Gtk.Label) and w.get_label() == row.get_title() and w.get_mapped():
                found.append(w)
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(row)
        return found[0]

    def rows(self, page):
        out = []

        def walk(w):
            if isinstance(w, Adw.ActionRow) and w.get_mapped():
                out.append(w)
            c = w.get_first_child()
            while c is not None:
                walk(c)
                c = c.get_next_sibling()
        walk(page)
        return out

    def test_at_phone_width_the_title_gets_the_row(self):
        page = self.show(360)
        self.assertTrue(page.narrow)
        rows = self.rows(page)
        self.assertEqual(len(rows), 4)  # keep, drop, restore and the put-off offer
        for row in rows:
            label = self.title_label(row)
            self.assertGreaterEqual(label.get_width(), 0.6 * row.get_width(), row.get_title())
        for (*_, action), button in page.buttons.items():  # every button still there, and reachable
            self.assertTrue(button.get_mapped(), action)

    def test_wide_the_buttons_stay_beside_the_title(self):
        page = self.show(900)
        self.assertFalse(page.narrow)
        row = next(r for r in self.rows(page) if r.get_title().startswith("Return the library"))
        button = page.buttons[("Return the library books before the weekend", "accept")]
        self.assertTrue(button.is_ancestor(row))


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class AgendaLaneTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sched = make_scheduler([event("lunch", "12:00", "12:30", "Lunch with Kim")])
        _span(self.sched, "dinner", "Dinner", "18:00", "19:00")
        self.plan, _ = self.sched.build_day_plan(DAY)
        self.plan.spans.append(EventSpan("cal-r", "Book group", at("19:30"), at("21:00"), SpanCategory.CALENDAR,
                                         source="calendar", shown_only=True))
        set_debug_time(at("17:10"))
        self.calls = []
        self.view = DayView()
        self.view.set_actions(lambda name, args: self.calls.append((name, args)) or "", None)

    def tearDown(self) -> None:
        set_debug_time(None)

    def test_one_switch_shows_or_hides_other_peoples_events(self) -> None:
        from tests.test_gui import _rows
        stored = {"on": True}
        self.view.set_theirs_provider(lambda: stored["on"], lambda on: stored.update(on=on))
        self.view.refresh_plan(self.plan)
        agenda = self.view.agenda
        group = agenda.groups["not_yours"]
        self.assertTrue(agenda.not_yours_switch.get_active())
        self.assertIn("Book group", [r.get_title() for r in _rows(group)])
        self.assertEqual([n.text for n in self.view.timeline.layout.notes], ["Book group (not yours)"])
        agenda.not_yours_switch.set_active(False)
        self.assertTrue(_iterate_until(lambda: not stored["on"]))
        _iterate_until(lambda: "Book group" not in [r.get_title() for r in _rows(agenda.groups["not_yours"])], 2)
        rows = [r.get_title() for r in _rows(agenda.groups["not_yours"])]
        self.assertEqual(rows, ["1 hidden"])  # one quiet line, with the way back
        self.assertEqual(self.view.timeline.layout.notes, [])  # the Timeline follows the same switch
        show = [b for b in _buttons(agenda.not_yours_hidden_row) if b.get_label() == "Show"][0]
        show.emit("clicked")
        self.assertTrue(_iterate_until(lambda: stored["on"]))
        _iterate_until(lambda: "Book group" in [r.get_title() for r in _rows(agenda.groups["not_yours"])], 2)
        self.assertEqual([n.text for n in self.view.timeline.layout.notes], ["Book group (not yours)"])

    def test_work_started_then_finished_with_a_tap(self) -> None:
        from thirties_core.plan_tools import PlanTools
        tools = PlanTools(self.plan, [], self.sched)
        hooked = []
        set_debug_time(at("09:40"))
        self.view.set_actions(lambda name, args: self.calls.append((name, args)) or tools.execute(name, args), None)
        self.view.agenda.on_work_times = hooked.append
        self.view.refresh_plan(self.plan)
        agenda = self.view.agenda
        self.assertEqual(agenda.work_btn.get_label(), "Started work")
        agenda.work_btn.emit("clicked")
        self.assertEqual(self.calls[-1], ("log_day_times", {"work_started": "9:40 AM"}))
        self.assertEqual(hooked, ["Recorded: started work 9:40 AM."])  # the window's hook: the day is replanned from it
        set_debug_time(at("16:20"))
        self.view.refresh_plan(tools.day_plan, force=True)
        self.assertEqual(agenda.work_btn.get_label(), "Finished work")
        agenda.work_btn.emit("clicked")
        self.assertEqual(self.calls[-1], ("log_day_times", {"work_ended": "4:20 PM"}))
        self.view.refresh_plan(tools.day_plan, force=True)
        self.assertIsNone(agenda.work_btn)  # both said

    def test_the_agenda_has_no_energy_row(self) -> None:
        self.view.refresh_plan(self.plan)
        self.assertFalse(hasattr(self.view.agenda, "energy_row"))
        self.assertFalse(hasattr(self.view.agenda, "energy_today"))

    def test_if_theres_time_on_the_timeline_with_the_agendas_actions(self) -> None:
        agenda = self.view.agenda
        agenda.read_tool = lambda name, args: "If there's time:\n- Repot the fern" \
            if name == "if_there_is_time_items" else ""
        self.view.refresh_plan(self.plan, force=True)
        page = self.view.timeline
        self.assertTrue(page.untimed_group.get_visible())
        self.assertEqual(page.untimed_title.get_text(), "If there's time")
        chip = page.untimed_chips["Repot the fern"]
        self.assertEqual(sorted(chip.actions), ["done", "drop", "schedule", "tomorrow", "week"])
        chip.actions["schedule"].emit("clicked")
        self.assertEqual(self.calls[-1], ("if_there_is_time", {"item": "Repot the fern", "action": "schedule"}))
        chip.actions["week"].emit("clicked")
        self.assertEqual(self.calls[-1], ("if_there_is_time", {"item": "Repot the fern", "action": "week"}))

    def test_a_later_days_pencilled_in_rows_and_chips_have_actions(self) -> None:
        later, _ = self.sched.build_day_plan(DAY + timedelta(days=1))
        acted = []
        agenda = self.view.agenda
        agenda.read_tool = lambda name, args: "On the list, no time yet:\n- Buy stamps" if name == "pencilled_in" else ""
        agenda.pencil_action = lambda title, action: acted.append((title, action)) or "ok"
        self.view.refresh_plan(later)
        done, schedule, later_btn, box = agenda.pencil_rows["Buy stamps"]
        self.assertEqual(later_btn.get_label(), "Not that day")
        done.emit("clicked")
        schedule.emit("clicked")
        self.assertEqual(acted, [("Buy stamps", "done"), ("Buy stamps", "schedule")])
        chip = self.view.timeline.untimed_chips["Buy stamps"]
        self.assertEqual(self.view.timeline.untimed_title.get_text(), "On the list, no time yet")
        chip.actions["next_day"].emit("clicked")
        chip.actions["drop"].emit("clicked")
        self.assertEqual(acted[2:], [("Buy stamps", "next_day"), ("Buy stamps", "drop")])


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class ChatLaneTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sched = make_scheduler()
        set_debug_time(at("19:10"))

    def tearDown(self) -> None:
        set_debug_time(None)

    def test_all_conversations_by_day_with_a_search_and_one_opens(self) -> None:
        from thirties.views.conversations_dialog import AllConversationsDialog
        from thirties_core.memory import ConversationStore
        from tests.fixtures import TZ
        store = ConversationStore(self.sched.state_db)
        for day, words in ((DAY, "Move lunch to 1"), (DAY - timedelta(days=3), "Plan the shed clear-out")):
            conv = store.create(day)
            conv.messages = [{"role": "user", "content": words}, {"role": "assistant", "content": "Done."}]
            store.save(conv)
        opened = []
        dialog = AllConversationsDialog(store, DAY, TZ, lambda d, i: opened.append((d, i)))
        self.assertEqual([g.get_title() for g in dialog.groups], ["Today", "Monday, October 12"])
        self.assertEqual(sorted(r.get_title() for r in dialog.rows.values()),
                         ["Move lunch to 1", "Plan the shed clear-out"])
        dialog.search.set_text("shed")
        self.assertTrue(_iterate_until(lambda: len(dialog.rows) == 1))
        (conv_id, row), = dialog.rows.items()
        row.emit("activated")
        self.assertEqual(opened, [(DAY - timedelta(days=3), conv_id)])
        dialog.search.set_text("boat")
        self.assertTrue(_iterate_until(lambda: dialog.stack.get_visible_child_name() == "empty"))

    def test_the_picker_offers_all_conversations(self) -> None:
        from tests.test_gui import ChatPanel
        panel = ChatPanel()
        asked = []
        panel.on_all_conversations = lambda: asked.append(True)
        panel.all_conversations_btn.emit("clicked")
        self.assertEqual(asked, [True])

    def test_the_message_box_checks_spelling(self) -> None:
        import gi
        try:
            gi.require_version("Spelling", "1")
            from gi.repository import Spelling  # noqa: F401
        except (ImportError, ValueError):
            self.skipTest("libspelling isn't installed here")
        from tests.test_gui import ChatPanel
        panel = ChatPanel()
        self.assertIsNotNone(panel.spelling)
        self.assertTrue(panel.spelling.get_enabled())
        self.assertIsNotNone(panel.text_view.get_extra_menu())  # suggestions in the right-click menu
        self.assertIsInstance(panel.text_view, Gtk.TextView)  # everything else about the box is as it was
        panel.text_buffer.set_text("Plan my evning")
        self.assertEqual(panel.text_buffer.get_text(*panel.text_buffer.get_bounds(), True), "Plan my evning")

    def test_without_the_spelling_library_the_box_still_works(self) -> None:
        from thirties.views import chat_panel
        import unittest.mock
        real = __import__("gi").require_version

        def no_spelling(name, version):
            if name == "Spelling":
                raise ValueError("Namespace Spelling not available")
            return real(name, version)
        with unittest.mock.patch("gi.require_version", no_spelling):
            view, adapter = chat_panel.spell_checked_view()
        self.assertIsNone(adapter)
        self.assertIsInstance(view, Gtk.TextView)


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class BeforeTodayLaneTests(unittest.TestCase):
    """Breaking an item down (break_down.py), from the Before today tab."""

    def setUp(self) -> None:
        from thirties.views.before_today import BeforeTodayPage
        self.page = BeforeTodayPage()
        self.calls, self.notices, self.broken = [], [], []
        self.result = "Shed clear-out: to be broken down. Now propose 2 to 5 small, concrete first steps."
        self.page.run_tool = lambda name, args: self.calls.append((name, args)) or self.result
        self.page.on_notice = self.notices.append
        self.page.on_break = self.broken.append
        self.page.rows_provider = lambda: {"ask": [{"title": "Shed clear-out", "why": "missed 3 days running"}]}

    def test_break_it_down_beside_keep_park_and_drop(self) -> None:
        self.page.offers_provider = lambda: []
        self.page.refresh()
        labels = [self.page.buttons[("still", "Shed clear-out", c)].get_label()
                  for c in ("keep", "park", "drop", "break")]
        self.assertEqual(labels, ["Keep for this week", "Park for later", "Drop", "Break It Down"])
        self.page.buttons[("still", "Shed clear-out", "break")].emit("clicked")
        self.assertEqual(self.calls, [("still_want", {"item": "Shed clear-out", "choice": "break"})])
        self.assertEqual(self.notices, ["Shed clear-out stays off the plan until it has pieces. Send the message in "
                                        "the chat for a few first steps."])  # never the assistant's instruction
        self.assertEqual(self.broken, ["Shed clear-out"])

    def test_the_put_off_offers_have_the_same_button_and_keep(self) -> None:
        self.page.rows_provider = lambda: {}
        self.page.offers_provider = lambda: [{"task_id": "t_tax", "title": "File the taxes", "asked": "2026-11-15",
                                              "answer": ""}]
        self.page.refresh()
        self.assertIn("offers", self.page.groups)
        self.page.buttons[("offer", "File the taxes", "keep")].emit("clicked")
        self.assertEqual(self.calls[-1], ("still_want", {"item": "File the taxes", "choice": "keep"}))
        self.page.buttons[("offer", "File the taxes", "break")].emit("clicked")
        self.assertEqual(self.calls[-1], ("still_want", {"item": "File the taxes", "choice": "break"}))
        self.assertEqual(self.broken, ["File the taxes"])

    def test_a_refusal_isnt_followed_by_the_chat(self) -> None:
        self.page.offers_provider = lambda: []
        self.result = "NOT_DONE: Shed clear-out can't be broken down here."
        self.page.refresh()
        self.page.buttons[("still", "Shed clear-out", "break")].emit("clicked")
        self.assertEqual((self.notices, self.broken), ([], []))  # the window shows the refusal itself

    def test_where_breaking_down_isnt_built_in_theres_no_button(self) -> None:
        self.page.refresh()
        self.assertNotIn(("still", "Shed clear-out", "break"), self.page.buttons)

    def test_the_chat_box_takes_the_ask_for_first_steps(self) -> None:
        from tests.test_gui import ChatPanel
        panel = ChatPanel()
        panel.suggest("Help me break Shed clear-out into a few small first steps.")
        self.assertEqual(panel.text_buffer.get_text(*panel.text_buffer.get_bounds(), True),
                         "Help me break Shed clear-out into a few small first steps.")


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class DaysAheadTests(unittest.TestCase):
    """The work button's reply and the days ahead pencilled in."""

    def setUp(self) -> None:
        self.sched = make_scheduler()
        set_debug_time(at("19:10"))
        self.later = DAY + timedelta(days=2)
        self.plan, _ = self.sched.build_day_plan(self.later)
        self.drafted = {self.later: [{"title": "Shed clear-out", "task_id": "t_shed",
                                      "start": at("14:00", 2).isoformat(), "minutes": 90},
                                     {"title": "Call the plumber", "task_id": "", "start": at("17:30", 2).isoformat(),
                                      "minutes": 30}]}
        self.view = DayView()
        self.view.set_actions(lambda *_: "", None)
        self.view.set_today(DAY)
        self.view.set_drafts_provider(lambda day: self.drafted.get(day, []))

    def tearDown(self) -> None:
        set_debug_time(None)

    def test_a_later_days_draft_is_in_pencil_on_the_agenda(self) -> None:
        from tests.test_gui import _rows
        self.view.refresh_plan(self.plan)
        agenda = self.view.agenda
        rows = _rows(agenda.groups["drafts"])
        self.assertEqual([r.get_title() for r in rows], ["<i>Shed clear-out</i>", "<i>Call the plumber</i>"])
        self.assertTrue(all(r.has_css_class("pencilled") for r in rows))
        planned = [r.get_title() for r in _rows(agenda.groups["plan"])]
        self.assertNotIn("Shed clear-out", planned)  # never a planned item
        self.assertEqual(agenda.groups["drafts"].get_title(), "Pencilled in")

    def test_and_on_the_timeline_as_marks_not_cards(self) -> None:
        self.view.refresh_plan(self.plan)
        page = self.view.timeline
        self.assertEqual([m.title for m in page.layout.pencil], ["Shed clear-out", "Call the plumber"])
        self.assertNotIn("Shed clear-out", [w.card.span.title for w in page.canvas.cards])
        mark = page.layout.pencil[0]
        self.assertEqual(mark.bottom - mark.top, 3.0)  # an hour and a half: three thirties
        import cairo
        from thirties.views import timeline
        drawn = []
        real = timeline.draw_pencil
        with unittest.mock.patch.object(timeline, "draw_pencil", lambda *a: drawn.append(a[2].title) or real(*a)):
            win = Gtk.Window(child=self.view, default_width=700, default_height=800)
            self.view.stack.set_visible_child_name("timeline")
            win.present()
            _iterate_until(lambda: bool(drawn), 3)
            win.destroy()
        self.assertIn("Shed clear-out", drawn)
        _ = cairo

    def test_today_and_past_days_have_no_draft_shown(self) -> None:
        today, _ = self.sched.build_day_plan(DAY)
        self.drafted[DAY] = self.drafted[self.later]
        self.view.refresh_plan(today)
        self.assertNotIn("drafts", self.view.agenda.groups)
        self.assertEqual(self.view.timeline.layout.pencil, [])

    def test_the_work_reply_is_split_for_a_notice_and_the_chat(self) -> None:
        from thirties_core.day_actions import work_reply
        self.assertEqual(work_reply("Recorded: started work 9:40 AM."), ("Started work 9:40 AM", ""))
        self.assertEqual(work_reply("Recorded: finished work 4:35 PM. After work: 2 daylight thirties and 8 dark "
                                    "thirties free before bed. A few that fit:\nWalk (30 min)"),
                         ("Finished work 4:35 PM", "After work: 2 daylight thirties and 8 dark thirties free before "
                                                   "bed. A few that fit:\nWalk (30 min)"))


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class TimelineNarrowTests(unittest.TestCase):
    """Items sharing a block sit side by side; on a phone each title must still be readable."""

    TITLES = ["Water the plants", "Wash the car", "Call the dentist", "Pack the gym bag", "Sort the receipts"]

    def setUp(self) -> None:
        sched = make_scheduler([])
        for sid, title, start, end in (("plants", self.TITLES[0], "09:00", "09:30"),
                                       ("car", self.TITLES[1], "09:00", "09:30"),
                                       ("dentist", self.TITLES[2], "10:00", "10:30"),
                                       ("bag", self.TITLES[3], "10:00", "10:30"),
                                       ("receipts", self.TITLES[4], "10:00", "10:30")):
            _span(sched, sid, title, start, end)
        self.plan, _ = sched.build_day_plan(DAY)
        set_debug_time(at("08:00"))
        self.calls = []

    def tearDown(self) -> None:
        set_debug_time(None)

    def built(self):
        """The Day tab with its cards, in no window: nothing depends on what size a compositor grants."""
        view = DayView()
        view.set_actions(lambda name, args: self.calls.append((name, args)) or "", None)
        view.set_today(DAY)
        view.refresh_plan(self.plan)
        view.stack.set_visible_child_name("timeline")
        view.timeline.stack.set_visible_child_name("day")
        return view

    def shown(self, width: int):
        """The Day tab's canvas laid out exactly `width` px wide: fitted and allocated directly, in no window, so
        nothing depends on the size a compositor grants or on when a frame is painted."""
        canvas = self.built().timeline.canvas
        height = canvas.measure(Gtk.Orientation.VERTICAL, width)[0]
        canvas.measure(Gtk.Orientation.HORIZONTAL, -1)
        for _ in range(3):  # allocate, fit each card to its width, allocate again with the cards reshaped
            canvas.allocate(width, height, -1, None)
            canvas._refit(width)
        canvas.allocate(width, height, -1, None)
        self.assertEqual(canvas.get_width(), width)
        self.assertTrue(all(w.fit == canvas.fit_for(canvas.lane_rect(width, w.card)[2]) for w in canvas.cards))
        return canvas

    @staticmethod
    def readable(card) -> bool:
        """The whole title shows: not cut with an ellipsis, and no word broken in the middle."""
        label = card.title_label
        layout = label.get_layout()
        if layout.is_ellipsized():
            return False
        widest_word = max(label.create_pango_layout(word).get_pixel_size()[0] for word in label.get_text().split())
        return label.get_width() >= widest_word

    def test_every_title_at_phone_width_shows_in_full_or_wraps_at_words(self) -> None:
        canvas = self.shown(360)
        self.assertEqual(sorted(w.title_label.get_text() for w in canvas.cards), sorted(self.TITLES))
        for card in canvas.cards:
            self.assertTrue(self.readable(card), f"{card.title_label.get_text()} is cut off")
        rows = {w.card.span.title: w.card.row for w in canvas.cards}
        self.assertEqual(rows["Wash the car"], rows["Water the plants"])  # still side by side, not hidden or merged
        self.assertEqual(rows["Call the dentist"], rows["Pack the gym bag"])

    def test_the_desktop_layout_is_unchanged(self) -> None:
        canvas = self.built().timeline.canvas
        canvas._refit(900)  # a desktop width, given directly
        self.assertEqual(len(canvas.cards), len(self.TITLES))
        for card in canvas.cards:
            self.assertEqual(card.fit, "roomy")
            self.assertFalse(card.title_label.get_wrap())
            self.assertTrue(card.tick.get_visible())
        canvas._refit(360)  # and the same cards narrow, then wide again: nothing is left over
        self.assertEqual({c.fit for c in canvas.cards}, {"tight", "bare"})
        canvas._refit(900)
        self.assertEqual({c.fit for c in canvas.cards}, {"roomy"})
        self.assertTrue(all(c.tick.get_visible() and not c.title_label.get_wrap() for c in canvas.cards))

    def test_a_card_without_room_for_its_tick_keeps_it_in_its_popover(self) -> None:
        canvas = self.shown(360)
        card = next(w for w in canvas.cards if w.card.span.title == "Call the dentist")
        self.assertEqual(card.fit, "bare")
        self.assertFalse(card.tick.get_visible())
        pop = canvas._open_card(card)
        pop.done_button.emit("clicked")
        self.assertEqual(self.calls[-1], ("log_outcome", {"event": "dentist", "status": "done"}))
        two = next(w for w in canvas.cards if w.card.span.title == "Wash the car")
        self.assertEqual(two.fit, "tight")
        self.assertTrue(two.tick.get_visible())  # room enough beside one other: the tick stays on the card
        self.assertFalse(hasattr(canvas._open_card(two), "done_button"))


def _buttons(widget):
    from tests.test_gui import _rows_of_type
    return _rows_of_type(widget, Gtk.Button)


if __name__ == "__main__":
    unittest.main()
