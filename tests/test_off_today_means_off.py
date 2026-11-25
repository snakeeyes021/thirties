"""What the user takes off today leaves today: off the plan and off today's list, so it never lingers as "no time
yet" (off_today.py). Whatever takes an item off today's plan on the user's words (remove_event, clear_time, a new
item put in its place, a redraw with no room) takes its line off the Today note too; what the user meant for each
line is the model's reading (today_list), never a wording check, and a removal asked as a question stands. Data
guard: the home list is never written to, nothing is ticked, Dropped is filed only on a drop word, and a write that
fails says its real reason. Invented fixtures: a community garden on a wet afternoon."""

import unittest

from tests.fixtures import DAY, at
from tests.test_if_time import Base
from thirties_core.carry import DROPPED, WEEK, OpenPool
from thirties_core.day_lists import AheadLists, pencilled_in
from thirties_core.joplin_writeback import JoplinWriter, current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

HOME = "- [ ] Call the bank\n- [ ] Buy stamps\n"
COMPOST, BEANS, TINS = "Turn the compost bays", "Stake the bean rows", "Sort the seed swap tins"


class OffBase(Base):
    def setUp(self):
        super().setUp()
        self.a = self.t.assistant
        for title, start in ((COMPOST, "4:40 PM"), (BEANS, "5:20 PM"), (TINS, "6:10 PM")):
            out = self.say(f"{title.lower()} today", "add_to_today", {"items": [title]})
            self.assertFalse(out.startswith(NOT_DONE), out)
            span = self.span(title)
            out = self.say(f"{title.lower()} at {start}", "update_event", {"event": span.id, "start": start})
            self.assertFalse(out.startswith(NOT_DONE), out)
        for title in (COMPOST, BEANS, TINS):
            self.assertIn(f"- [ ] {title}", self.body("n_today"))

    def said(self, message, tool, args):
        with model_call():
            return self.say(message, tool, args)

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def assert_guarded(self):
        """The home list as it was, nothing ticked, no Dropped note."""
        self.assertEqual(self.body("n_errands"), HOME)
        self.assertEqual([u for u in self.api.updates if u[0] == "n_errands"], [])
        self.assertNotIn("[x]", self.body("n_today"))
        self.assertFalse([n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped")])


class ClearTimeTest(OffBase):
    def test_cleared_items_leave_todays_list_too(self):
        out = self.said("the plot's under water, wipe what's left of the afternoon", "clear_time",
                        {"start": "4:20 PM", "end": "11:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        for title in (COMPOST, BEANS, TINS):
            self.assertEqual(self.planned(title), [])
            self.assertNotIn(title, self.body("n_today"))
            self.assertIn(f"{title} was also on today's list: taken off", out)
            self.assertNotIn(title, pencilled_in(self.a))  # never "no time yet"
        self.assert_guarded()

    def test_keep_leaves_the_lines(self):
        out = self.said("open a gap 4:20 to 5:50 but leave their lines be", "clear_time",
                        {"start": "4:20 PM", "end": "5:50 PM", "today_list": "keep"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn(f"- [ ] {COMPOST}", self.body("n_today"))
        self.assertIn(f"{COMPOST} is still on today's list", out)
        self.assertIn(f"- [ ] {TINS}", self.body("n_today"))  # never cleared: untouched
        self.assert_guarded()

    def test_a_trimmed_item_keeps_its_line(self):
        out = self.said("clear 4:50 to 5:00 for a quick call", "clear_time", {"start": "4:50 PM", "end": "5:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertTrue(self.planned(COMPOST))
        self.assertIn(f"- [ ] {COMPOST}", self.body("n_today"))


class RemoveEventTest(OffBase):
    def test_a_removal_asked_as_an_angry_question_goes_through(self):
        out = self.said("ugh, bean rows again?! strike them off for the day", "remove_event",
                        {"event": self.planned(BEANS)[0].id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned(BEANS), [])
        self.assertNotIn(BEANS, self.body("n_today"))
        self.assert_guarded()

    def test_an_untimed_line_asked_off_in_a_question(self):
        self.api.notes["n_today"]["body"] += "- [ ] Rinse the watering cans\n"
        self.assertIn("Rinse the watering cans", pencilled_in(self.a))
        out = self.said("rinse the cans?! nope, scrap it from my list", "remove_event",
                        {"event": "Rinse the watering cans"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Rinse the watering cans", self.body("n_today"))
        self.assertNotIn("Rinse the watering cans", pencilled_in(self.a))
        self.assert_guarded()

    def test_off_by_default_and_it_doesnt_come_back_as_a_leftover(self):
        OpenPool(self.sched.state_db).add([{"title": TINS, "task_id": "", "horizon": WEEK}], DAY)  # from before
        out = self.said("scrap the seed tins", "remove_event", {"event": self.planned(TINS)[0].id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("won't come back by itself", out)
        self.assertNotIn(TINS.lower(), OpenPool(self.sched.state_db).all(DAY))  # never offered again by itself
        self.assertEqual(OpenPool(self.sched.state_db).all()[TINS.lower()]["horizon"], DROPPED)  # can come back
        self.assert_guarded()

    def test_later_and_tomorrow_bring_it_back_another_day(self):
        out = self.said("bean rows another day", "remove_event",
                        {"event": self.planned(BEANS)[0].id, "today_list": "later"})
        self.assertIn("waits for", out)
        self.assertIsNotNone(OpenPool(self.sched.state_db).all().get(BEANS.lower()))
        out = self.said("compost bays tomorrow then", "remove_event",
                        {"event": self.planned(COMPOST)[0].id, "today_list": "tomorrow"})
        self.assertIn("tomorrow's list", out)
        from datetime import timedelta
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1))],
                         [COMPOST])
        for title in (BEANS, COMPOST):
            self.assertNotIn(title, self.body("n_today"))
        self.assert_guarded()

    def test_a_home_list_item_loses_only_its_today_copy(self):
        self.say("bank today", "add_to_today", {"items": ["Call the bank"]})
        out = self.said("no bank today", "remove_event", {"event": self.planned("Call the bank")[0].id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("it stays on its own list", out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assert_guarded()

    def test_items_the_words_name_none_of_still_come_off_today(self):
        # a reply to a list just shown can name no item: which ones it means is the model's
        # reading, and the answer says what came off
        for title in (BEANS, TINS):
            out = self.said("the whole lot after the compost: nope, wrong day for it", "remove_event",
                            {"event": self.planned(title)[0].id})
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertIn(f"Removed {title}", out)
            self.assertIn(f"{title} was also on today's list: taken off", out)
            self.assertNotIn(title, self.body("n_today"))
        self.assertTrue(self.planned(COMPOST))
        self.assert_guarded()

    def test_a_message_of_only_questions_still_takes_them_off_today(self):
        out = self.said("beans, in this weather? and the tins as well?", "remove_event",
                        {"event": self.planned(BEANS)[0].id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned(BEANS), [])
        self.assertNotIn(BEANS, self.body("n_today"))
        self.assert_guarded()

    def test_a_later_day_still_needs_the_users_words_for_it(self):
        from datetime import timedelta
        out = self.say("put the bean rows on tomorrow too", "add_event",
                       {"title": BEANS, "start": "9:40 AM", "duration": "30 min", "day": "tomorrow",
                        "because": "put the bean rows on tomorrow too"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        later = self.t.plan_for(DAY + timedelta(days=1))
        span = next(s for s in later.spans if s.title == BEANS)
        out = self.said("the rain gauge says 2 inches", "remove_event", {"event": span.id, "day": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertTrue(any(s.title == BEANS for s in self.t.plan_for(DAY + timedelta(days=1)).spans))

    def test_a_refused_write_says_its_real_reason(self):
        self.api.notes["n_today"]["body"] += "- [ ] Rinse the watering cans\n"
        self.sched.config.joplin.write_back = False
        out = self.said("drop the watering cans off today", "remove_event", {"event": "Rinse the watering cans"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("just now", out)
        self.assertIn("allowed", out)


class ReplacedAndNoRoomTest(OffBase):
    def test_an_item_put_in_its_place_takes_its_line(self):
        out = self.said("shed sweep replaces the compost bays, same 4:40 slot", "add_event",
                        {"title": "Sweep the tool shed", "start": "4:40 PM", "duration": "30 min",
                         "replace_overlaps": True, "because": "shed sweep replaces the compost bays"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned(COMPOST), [])
        self.assertNotIn(COMPOST, self.body("n_today"))
        self.assertNotIn(COMPOST, pencilled_in(self.a))

    def test_no_room_takes_the_line_too(self):
        from thirties_core.off_today import after_no_room
        span = self.planned(TINS)[0]
        self.t._drop(span)
        self.assertEqual(after_no_room(self.t, [span]), [TINS])
        self.assertNotIn(TINS, self.body("n_today"))
        self.assertNotIn(TINS, pencilled_in(self.a))


class GuardTest(OffBase):
    """The Joplin write guard on the paths these removals use."""

    def submit(self, said, op):
        token = current_turn.set(said)
        try:
            return JoplinWriter(self.sched.config, self.sched.state_db, self.api).submit(op)
        finally:
            current_turn.reset(token)

    def test_a_today_line_comes_off_on_a_question(self):
        status, why = self.submit("[app] compost bays, seriously?? gone, please",
                                  {"op": "remove", "note_id": "n_today", "title": COMPOST, "state": "open"})
        self.assertIn(status, ("done", "queued"), why)
        self.assertNotIn(COMPOST, self.body("n_today"))

    def test_a_home_line_still_needs_a_drop_word(self):
        status, _ = self.submit("[app] bank call, seriously?? gone, please",
                                {"op": "remove", "note_id": "n_errands", "title": "Call the bank"})
        self.assertEqual(status, "failed")
        self.assertEqual(self.body("n_errands"), HOME)

    def test_dropped_still_needs_a_drop_word(self):
        from thirties_core.done_log import file_dropped_op
        status, _ = self.submit("[app] take the compost off?",
                                file_dropped_op("n_today", COMPOST, "Today To-Do", DAY, 0, ""))
        self.assertEqual(status, "failed")
        self.assertFalse([n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped")])


if __name__ == "__main__":
    unittest.main()
