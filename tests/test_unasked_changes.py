"""Only what the user asked about changes: moves, new items and usual hours need their words, like removals do.

Seen in simulated days: asked to move one item, the model also moved two others nobody named; asked about one
thing, it shrank another and planned a second copy of it; a new item nobody mentioned went on the plan; told only a
bedtime, the usual week was saved with work hours too, and today's Work changed with it; starting one item sent an
item already under way, and another planned beside it, hours later. Now:

- update_event, add_event and set_week, called for the user's message, change only the items and settings the
  message refers to (by name, by a pointer back, or by the time it gives for them), unless the user asks for a
  bulk change ("redo the afternoon"); otherwise the model is told to leave it, put the named item alongside, or ask;
- Thirties' own planning tools (a day proposal, a routine, the morning set-up) place what they need;
- nobody talking (the app's own controls, the background), and Thirties' own code calling a tool, are never held
  here;
- saving a bedtime leaves the usual work hours and today's Work as they were;
- starting something now never moves an item already under way, and moves what it covers only nearby.

On invented items only.
"""

import unittest

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_log import model_call, tool_call
from thirties_core.plan_tools import NOT_DONE


class UnaskedCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.manager.tools
        for title, start in (("Sand the bench", "10:00 AM"), ("Repot the fern", "2:00 PM"),
                             ("Wash the van", "5:00 PM")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def say(self, message, tool, args):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def span(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title)


class TestMovingWhatTheUserDidntName(UnaskedCase):

    def test_an_item_the_user_didnt_name_stays(self):
        out = self.say("move the van to 6", "update_event", {"event": "Sand the bench", "start": "11:00 AM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Sand the bench", out)
        self.assertEqual(self.span("Sand the bench").start_dt, at("10:00"))

    def test_the_item_the_user_named_moves(self):
        out = self.say("move the van to 6", "update_event", {"event": "Wash the van", "start": "6:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Wash the van").start_dt, at("18:00"))

    def test_its_new_time_said_another_way(self):
        out = self.say("the bench job is 11:30 now", "update_event", {"event": "Sand the bench", "start": "11:30 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_bulk_request_moves_what_it_needs(self):
        out = self.say("redo the whole afternoon, it's a mess", "update_event",
                       {"event": "Repot the fern", "start": "3:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_pointer_back_to_the_item(self):
        self.t.prior_exchange = ("", "Want Repot the fern at 3:00 PM instead?")
        out = self.say("yes do that", "update_event", {"event": "Repot the fern", "start": "3:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_nobody_talking_is_never_held(self):
        out = self.say(None, "update_event", {"event": "Sand the bench", "start": "11:00 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_thirties_own_planning_moves_what_it_needs(self):
        self.t.user_message = "plan my day"
        try:
            with model_call(), tool_call("propose_day"):
                out = self.t.update_event("Sand the bench", start="11:00 AM")
        finally:
            self.t.user_message = None
        self.assertFalse(out.startswith(NOT_DONE), out)


class TestInATurn(UnaskedCase):
    """Through a turn with the model: its calls are held to the user's words; its true reply goes out."""

    def test_the_model_moving_an_unnamed_item_is_refused(self):
        from tests.test_conversation import reply
        said = "Wash the van is at 6:00 PM now."
        self.engine.responses = [
            reply('CALL update_event {"event": "Wash the van", "start": "6:00 PM"}\n'
                  'CALL update_event {"event": "Sand the bench", "start": "11:00 AM"}'),
            reply(said)]
        out = self.manager.send_user_message("move the van to 6")
        self.assertEqual(self.span("Wash the van").start_dt, at("18:00"))
        self.assertEqual(self.span("Sand the bench").start_dt, at("10:00"))
        results = [str(m.get("content")) for m in self.manager.brain.messages if m.get("role") == "tool"]
        self.assertTrue(any(r.startswith(NOT_DONE) and "Sand the bench" in r for r in results), results)
        self.assertIn(said, out)


class TestANewItemTheUserDidntAskFor(UnaskedCase):

    def test_an_item_nobody_mentioned_isnt_added(self):
        out = self.say("move the van to 6", "add_event", {"title": "Polish the brass", "start": "3:00 PM",
                                                          "duration": "30 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertFalse(any(s.title == "Polish the brass" for s in self.manager.day_plan.spans))

    def test_an_item_the_user_asked_for_is_added(self):
        out = self.say("polish the brass at 3 please", "add_event", {"title": "Polish the brass", "start": "3:00 PM",
                                                                      "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_second_copy_of_an_item_nobody_named(self):
        out = self.say("move the van to 6", "add_event", {"title": "Repot the fern", "start": "4:00 PM",
                                                          "duration": "30 min", "again": True})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_asked_to_plan_the_day_it_may_add(self):
        out = self.say("can you plan out my afternoon?", "add_event", {"title": "Polish the brass",
                                                                         "start": "3:00 PM", "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)


class TestTheUsualWeek(UnaskedCase):

    def test_work_hours_the_user_didnt_talk_about_stay(self):
        out = self.say("bed is 10:30 on weeknights", "set_week",
                       {"days": "weekdays", "bedtime": "10:30 PM", "work_start": "9:00 AM", "work_end": "5:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("work", out.lower())

    def test_a_bedtime_leaves_todays_work_as_it_was(self):
        work = self.span("Work")  # today's Work, made before the usual hours were last changed
        work.start_dt, work.end_dt = at("09:00"), at("14:30")
        self.manager.scheduler.state_db.upsert_span(work)
        self.manager.scheduler.state_db.save_day_meta(self.manager.scheduler.state_db.get_day_meta(DAY))
        out = self.say("I go to bed at 10:30 on weeknights", "set_week", {"days": "weekdays", "bedtime": "10:30 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        work = self.span("Work")
        self.assertEqual((work.start_dt, work.end_dt), (at("09:00"), at("14:30")))

    def test_work_hours_the_user_gives_are_saved(self):
        out = self.say("I work 9 to 5 on weekdays", "set_week",
                       {"days": "weekdays", "work_start": "9:00 AM", "work_end": "5:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)


class TestStartingSomethingNow(UnaskedCase):

    def test_an_item_under_way_isnt_moved_away(self):
        set_debug_time(at("13:40"))
        self.say("starting the fern now, it'll take a couple of hours", "doing_now",
                 {"item": "Repot the fern", "duration": "2 hours"})
        set_debug_time(at("14:10"))
        out = self.say("grabbing a quick sandwich now", "doing_now", {"item": "Quick sandwich", "duration": "20 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        fern = self.span("Repot the fern")
        self.assertEqual(fern.start_dt, at("13:30"))  # still where it's going on, not sent to the evening

    def test_what_it_covers_moves_only_nearby(self):
        self.t.user_message = None
        for title, start in (("Fold the towels", "3:00 PM"), ("Oil the hinges", "4:00 PM"),
                             ("Sort the screws", "5:30 PM"), ("Label the jars", "7:00 PM")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "90 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("14:00"))
        self.say("repotting the fern now, about 2 hours", "doing_now", {"item": "Repot the fern", "duration": "2 hours"})
        towels = self.span("Fold the towels")
        self.assertEqual(towels.start_dt, at("15:00"))  # stays beside it, not shipped to the night


if __name__ == "__main__":
    unittest.main()
