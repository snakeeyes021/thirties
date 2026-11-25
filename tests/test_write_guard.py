"""Data guard (write_guard.py): in a conversation, three Joplin writes need the user's own words for them: filing
as dropped, taking a line off a list, and a tick. Taking something off today touches only today's plan and the Today
note; something under way or merely all right is never ticked done. Through the real tool paths, on the mock
Joplin API. Fixtures are invented: a late-shift potter who paddles a canoe on days off."""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import OpenPool
from thirties_core.joplin_writeback import JoplinWriter, current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE
from thirties_core.write_guard import asks_to_drop, says_done

HOME = "- [ ] Pay the studio rent\n- [ ] Return the canoe paddles\n- [ ] Renew the kiln permit\n"


class WordsTest(unittest.TestCase):
    def test_drop_words(self):
        for said in ("drop the glaze order", "I'm not doing it", "forget it", "cross it off for good",
                     "delete the paddle order", "never going to do that"):
            self.assertTrue(asks_to_drop(said), said)
        for said in ("take that off", "take the glaze order off", "skip it today", "not today", "forget it for today",
                     "drop off the canoe paddles", "I'm not doing it today", "", None):
            self.assertFalse(asks_to_drop(said), said)

    def test_done_words(self):
        for said, title in (("the studio rent is paid", "Pay the studio rent"),
                            ("renewed the kiln permit", "Renew the kiln permit"),
                            ("kiln's done", "Fire the kiln"), ("yes", "Fire the kiln"),
                            ("returned the paddles this morning", "Return the canoe paddles")):
            self.assertTrue(says_done(said, title), said)
        for said, title in (("the kiln IS firing", "Fire the kiln"), ("kiln's running", "Fire the kiln"),
                            ("the kiln's fine i guess", "Fire the kiln"), ("kiln is on", "Fire the kiln"),
                            ("started the kiln", "Fire the kiln"), ("the glaze test is in progress", "Glaze test"),
                            ("haven't paid the rent", "Pay the studio rent"), ("take the rent off", "Pay the studio rent"),
                            ("", "Fire the kiln")):
            self.assertFalse(says_done(said, title), said)


class GuardBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = HOME
        self.services.tasks[:] = [item("t_rent", "Pay the studio rent"), item("t_paddles", "Return the canoe paddles"),
                                  item("t_permit", "Renew the kiln permit")]
        for t in self.services.tasks:
            t.list_role = "list"

    def say(self, message, tool, args):
        """One tool call as part of a turn of the user's: the plan sees their words, and so does every Joplin write."""
        self.t.user_message = message
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute(tool, args)
        finally:
            current_turn.reset(token)
            self.t.user_message = None

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if (n.get("title") or "").startswith("Dropped ")), None)

    def done_notes(self):
        return [n for n in self.api.notes.values() if (n.get("title") or "").startswith("Done ")]

    def untimed(self, title):
        self.api.notes["n_today"]["body"] += f"- [ ] {title}\n"

    def assert_untouched_home(self):
        self.assertEqual(self.body("n_errands"), HOME)
        self.assertEqual([u for u in self.api.updates if u[0] == "n_errands"], [])

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]


class TakeOffIsNotDropTest(GuardBase):
    def test_take_off_an_untimed_today_only_item_never_files_it_dropped(self):
        self.untimed("Mix the celadon glaze")
        out = self.say("take the celadon glaze off, the oxides aren't in yet", "remove_event",
                       {"event": "Mix the celadon glaze", "today_list": "later"})  # the model: back another day
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Mix the celadon glaze", self.body("n_today"))
        self.assertIsNone(self.dropped_note())
        rec = OpenPool(self.sched.state_db).all().get("mix the celadon glaze")
        self.assertIsNotNone(rec)  # it waits for a day later this week: still to do
        self.assertNotEqual(rec.get("horizon"), "dropped")
        self.assert_untouched_home()

    def test_drop_for_good_without_a_drop_word_never_files_it_dropped(self):
        self.untimed("Mix the celadon glaze")
        out = self.say("take the celadon glaze off", "remove_event",
                       {"event": "Mix the celadon glaze", "today_list": "drop"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Mix the celadon glaze", self.body("n_today"))  # off today all the same
        self.assertIsNone(self.dropped_note())  # filed as dropped only on a drop word
        self.assertIn("isn't filed as dropped for good", out)
        self.assert_untouched_home()

    def test_take_off_a_timed_list_item_leaves_its_list_alone(self):
        self.t.execute("add_event", {"title": "Rent", "start": "3:00 PM", "duration": "30 min", "task_id": "t_rent"})
        span = next(s for s in self.t.day_plan.spans if s.task_id == "t_rent")
        out = self.say("take that rent thing off", "remove_event", {"event": span.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned(span.title), [])
        self.assertNotIn("Pay the studio rent", self.body("n_today"))
        self.assertIsNone(self.dropped_note())
        self.assertNotIn("[x]", self.body("n_today"))
        self.assert_untouched_home()

    def test_if_there_is_time_drop_on_take_off_words_files_nothing(self):
        self.untimed("Mix the celadon glaze")
        self.say("take that off", "if_there_is_time", {"item": "Mix the celadon glaze", "action": "drop"})
        self.assertIsNone(self.dropped_note())
        self.assert_untouched_home()

    def test_a_home_line_is_never_removed_on_take_off_words(self):
        token = current_turn.set("[app] take the rent off")
        try:
            status, _ = JoplinWriter(self.sched.config, self.sched.state_db, self.api).submit(
                {"op": "remove", "note_id": "n_errands", "title": "Pay the studio rent"})
        finally:
            current_turn.reset(token)
        self.assertEqual(status, "failed")
        self.assert_untouched_home()

    def test_drop_words_still_file_it_dropped(self):
        self.untimed("Mix the celadon glaze")
        out = self.say("drop the celadon glaze for good", "remove_event",
                       {"event": "Mix the celadon glaze", "today_list": "drop"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIsNotNone(self.dropped_note())
        self.assertIn("Mix the celadon glaze", self.dropped_note()["body"])
        self.assert_untouched_home()


class RunningIsNotDoneTest(GuardBase):
    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": "Fire the kiln", "start": "10:00 AM", "duration": "60 min"})
        set_debug_time(at("11:30"))

    def test_running_or_fine_never_ticks(self):
        for said in ("the kiln IS firing", "kiln's running", "the kiln's fine i guess", "started the kiln"):
            with self.subTest(said=said):
                self.say(said, "log_outcome", {"event": "Fire the kiln", "status": "done"})
                self.assertNotIn("[x]", self.body("n_today"))
                self.assertEqual(self.done_notes(), [])

    def test_if_there_is_time_done_on_running_never_ticks(self):
        self.untimed("Wedge the clay")
        self.say("the wedging's going fine", "if_there_is_time", {"item": "Wedge the clay", "action": "done"})
        self.assertIn("- [ ] Wedge the clay", self.body("n_today"))
        self.assertNotIn("[x]", self.body("n_today"))

    def test_a_later_done_still_ticks(self):
        self.say("kiln's running", "log_outcome", {"event": "Fire the kiln", "status": "done"})
        self.assertNotIn("[x]", self.body("n_today"))
        out = self.say("kiln's done", "log_outcome", {"event": "Fire the kiln", "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("[x] Fire the kiln", self.body("n_today"))


class RefusedTickWritesNothingElseTest(GuardBase):
    def test_a_refused_tick_leaves_both_notes_as_they_were(self):
        self.t.execute("add_event", {"title": "Rent", "start": "10:00 AM", "duration": "30 min", "task_id": "t_rent"})
        set_debug_time(at("11:00"))
        today = self.body("n_today")
        self.say("the rent's fine for now", "log_outcome", {"event": "Pay the studio rent", "status": "done"})
        self.assertEqual(self.body("n_today"), today)
        self.assert_untouched_home()
        self.assertIsNone(self.dropped_note())
        self.assertEqual(self.done_notes(), [])


class PaidAndTakeOffIsDoneTest(GuardBase):
    """Said to have happened and to come off: a done, ticked on both notes, never filed as dropped, and never taken
    off before the tick."""

    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": "Rent", "start": "10:00 AM", "duration": "30 min", "task_id": "t_rent"})
        self.t.execute("add_event", {"title": "Paddles", "start": "10:30 AM", "duration": "30 min",
                                     "task_id": "t_paddles"})
        set_debug_time(at("11:30"))

    def test_paid_and_take_off_ticks_done(self):
        span = next(s for s in self.t.day_plan.spans if s.task_id == "t_rent")
        out = self.say("the studio rent is paid, take it off", "remove_event", {"event": span.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [x] Pay the studio rent", self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.done_notes()))
        self.assertIsNone(self.dropped_note())
        self.assertNotIn("- [ ] Pay the studio rent", self.body("n_errands"))
        self.assertIn("- [ ] Return the canoe paddles", self.body("n_errands"))  # the other item is untouched

    def test_clear_words_with_paid_let_the_done_through(self):
        out = self.say("rent's paid, take that off", "log_outcome", {"event": "Pay the studio rent", "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIsNone(self.dropped_note())

    def test_paid_said_of_another_item_is_no_done_for_this_one(self):
        span = next(s for s in self.t.day_plan.spans if s.task_id == "t_paddles")
        self.say("rent's paid, and take the paddles off", "remove_event", {"event": span.id})
        self.assertIn("- [ ] Return the canoe paddles", self.body("n_errands"))
        self.assertIsNone(self.dropped_note())

    def test_a_failed_tick_writes_nothing_else(self):
        span = next(s for s in self.t.day_plan.spans if s.task_id == "t_rent")
        today, before = self.body("n_today"), len(self.api.updates)
        from unittest.mock import patch
        with patch("thirties_core.joplin_writeback.JoplinWriter.submit", return_value=("failed", "1")):
            out = self.say("the studio rent is paid, take it off", "remove_event", {"event": span.id})
        self.assertEqual(self.body("n_today"), today)
        self.assert_untouched_home()
        self.assertEqual(len(self.api.updates), before)
        self.assertIsNone(self.dropped_note())
        self.assertNotIn("Removed", out)

    def test_untimed_line_said_done_and_taken_off_is_ticked(self):
        self.untimed("Sweep the wheel room")
        out = self.say("swept the wheel room, take it off", "remove_event", {"event": "Sweep the wheel room"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [x] Sweep the wheel room", self.body("n_today"))
        self.assertIsNone(self.dropped_note())

    def test_a_failed_tick_on_an_untimed_line_says_so(self):
        self.untimed("Sweep the wheel room")
        from unittest.mock import patch
        with patch("thirties_core.joplin_writeback.JoplinWriter.submit", return_value=("failed", "1")):
            out = self.say("swept the wheel room", "if_there_is_time", {"item": "Sweep the wheel room",
                                                                        "action": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("isn't ticked", out)
        self.assertIn("- [ ] Sweep the wheel room", self.body("n_today"))


class LooseWordsOnlyPointTest(GuardBase):
    """A reply to a question about one item points to it, however loosely; a reply saying it's going or fine is
    still no done."""

    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": "Fire the kiln", "start": "10:00 AM", "duration": "60 min"})
        set_debug_time(at("11:30"))

    def ask(self, message):
        self.t.prior_exchange = ("", "Is Fire the kiln finished?")
        try:
            return self.say(message, "log_outcome", {"event": "Fire the kiln", "status": "done"})
        finally:
            self.t.prior_exchange = None

    def test_a_reply_that_its_going_is_no_done(self):
        for said in ("it IS running", "it's fine i guess", "still on"):
            with self.subTest(said=said):
                out = self.ask(said)
                self.assertTrue(out.startswith(NOT_DONE), out)
                self.assertNotIn("[x]", self.body("n_today"))

    def test_a_reply_that_its_done_still_goes(self):
        out = self.ask("yes, it's done")
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
