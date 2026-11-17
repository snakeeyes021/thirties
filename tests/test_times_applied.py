"""A time the user gives for an item is applied, on every path, and a reply never says so when it wasn't.

Seen in simulated days: the user gave an item's time three or four times over and each change came back refused,
while the replies said it was locked in. The causes, each covered here:

- the item was already at that very time under the hood (placed by a routine, or a calendar event at its own
  time), only shown by its block; the change was refused as "nothing to change", or, for a calendar event, as a
  time the user didn't give; the time was never marked as the user's, so the block went on showing;
- the user's words named the item loosely ("the afternoon stroll" for Stroll by the canal, beside another stroll), so
  the time wasn't read as theirs;
- a routine step with its own time the user gave was placed there but shown by its block;
- a time this morning that hasn't come yet, before the day's plan begins, was refused as tomorrow's;
- a reply saying a time was "locked in" or "all set" over a refused change passed the checks.

On invented items only.
"""

import unittest

from tests.fixtures import DAY, at, event
from tests.test_conversation import ConversationTestCase, reply
from tests.test_given_times import GivenCase
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE
from thirties_core.shown_times import span_when, start_exact


class TestACalendarEventAtTheTimeTheUserGives(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.events = [event("cal-pottery", "10:15", "10:50", "Pottery class")]
        self.manager = self.make_manager(events=self.events)
        self.t = self.manager.tools

    def change(self, said, **args):
        self.t.user_message = said
        try:
            return self.t.execute("update_event", {"event": "Pottery class", **args})
        finally:
            self.t.user_message = None

    def pottery(self):
        return next(s for s in self.manager.day_plan.spans if s.title == "Pottery class")

    def test_the_time_it_already_has_is_not_refused(self):
        out = self.change("no, pottery is 10:15 to 10:50, those are fixed", start="10:15 AM", end="10:50 AM")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("10:15 AM", out)
        self.assertEqual((self.pottery().start_dt, self.pottery().end_dt), (at("10:15"), at("10:50")))
        self.assertEqual((self.events[0].start_dt, self.events[0].end_dt), (at("10:15"), at("10:50")))

    def test_its_start_alone_at_the_time_it_has(self):
        out = self.change("pottery starts 10:15 not 10", start="10:15 AM")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_users_start_with_the_calendars_own_end(self):
        out = self.change("pottery starts 10:15, not 10", start="10:15 AM", end="10:50 AM")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_time_the_user_didnt_give_is_still_refused(self):
        out = self.change("sort my morning out", start="10:15 AM")
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestAnItemAlreadyAtTheTimeTheUserGives(GivenCase):
    """Placed at 1:10 by Thirties (shown in its 1:00 block): the user saying 1:10 makes the time theirs."""

    def place(self, title, start, minutes=15):
        self.t.user_message = None
        out = self.t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def test_the_same_time_is_kept_as_theirs_not_refused(self):
        span = self.place("Stroll by the canal", "1:10 PM")
        self.assertFalse(start_exact(span))  # Thirties' time: shown by its block
        self.t.user_message = "the canal stroll is 1:10, it was never 1"
        out = self.t.execute("update_event", {"event": span.id, "start": "1:10 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(span.given_start, at("13:10"))
        self.assertTrue(span_when(span).startswith("1:10 PM"), span_when(span))
        self.assertEqual(self.sched.state_db.get_span(span.id).given_start, at("13:10"))

    def test_an_item_named_loosely_beside_another_like_it(self):
        self.place("Stroll to the bakery", "4:00 PM")
        span = self.place("Stroll by the canal", "1:00 PM")
        self.t.user_message = "oh and the afternoon stroll is 1:10, not 1"
        out = self.t.execute("update_event", {"event": span.id, "start": "1:10 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertEqual((span.start_dt, span.given_start), (at("13:10"), at("13:10")))
        self.assertTrue(span_when(span).startswith("1:10 PM"), span_when(span))

    def test_nothing_to_change_when_the_user_gave_no_time(self):
        span = self.place("Stroll by the canal", "1:10 PM")
        self.t.user_message = "tidy up the afternoon"
        out = self.t.execute("update_event", {"event": span.id, "start": "1:10 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIsNone(span.given_start)

    def test_a_time_for_another_item_is_still_not_this_ones(self):
        self.place("Stroll by the canal", "1:00 PM")
        span = self.place("Repot the fern", "2:00 PM")
        self.t.user_message = "the canal stroll is at 2:15"
        self.t.execute("update_event", {"event": span.id, "start": "2:15 PM", "alongside": True})
        span = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertIsNone(span.given_start)


class TestARoutineStepAtTheTimeTheUserGives(GivenCase):

    def test_a_step_with_its_own_time_the_user_gave_is_shown_as_said(self):
        self.t.user_message = "deep clean this afternoon please, and mop at 2:15"
        out = self.t.execute("put_routine", {"routine": "Deep Clean", "parts": [
            {"start": "2:00 PM", "items": ["Vacuum", "14:15 Mop"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        mop = next(s for s in self.t.day_plan.spans if s.title == "Mop")
        self.assertEqual((mop.start_dt, mop.given_start), (at("14:15"), at("14:15")))
        vacuum = next(s for s in self.t.day_plan.spans if s.title == "Vacuum")
        self.assertIsNone(vacuum.given_start)  # its time is Thirties' own

    def test_a_steps_own_time_from_the_note_alone_stays_by_its_block(self):
        self.t.user_message = "deep clean this afternoon please"
        self.t.execute("put_routine", {"routine": "Deep Clean", "parts": [
            {"start": "2:00 PM", "items": ["Vacuum", "14:15 Mop"]}]})
        mop = next(s for s in self.t.day_plan.spans if s.title == "Mop")
        self.assertIsNone(mop.given_start)


class TestThisMorningBeforeThePlanBegins(GivenCase):
    """Up at 6:20, the day's plan starts at 7:00: a time the user gives for later this morning is planned there."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("06:20"))

    def add_early(self, args, message):
        self.t.user_message = message
        return self.t.execute("add_event", args)

    def test_a_time_still_ahead_this_morning_is_planned_there(self):
        out = self.add_early({"title": "Porridge for the twins", "start": "6:40 AM", "duration": "30 min"},
                             "porridge for the twins is 6:40, put it on")
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Porridge for the twins")
        self.assertEqual((span.start_dt, span.given_start), (at("06:40"), at("06:40")))
        self.assertNotIn("tomorrow", out)
        from thirties_core.messages import schedule_rows
        self.assertIn("Porridge for the twins", "\n".join(schedule_rows(self.t.day_plan)))

    def test_one_that_ends_before_the_plan_begins_stays_on_the_day(self):
        set_debug_time(at("05:40"))
        out = self.add_early({"title": "Feed the hens", "start": "6:00 AM", "duration": "20 min"},
                             "feed the hens at 6:00")
        self.assertFalse(out.startswith(NOT_DONE), out)
        plan, _ = self.sched.build_day_plan(DAY)
        from thirties_core.day_edges import this_mornings
        self.assertIn("Feed the hens", [s.title for s in this_mornings(plan)])

    def test_a_time_the_user_didnt_give_is_still_refused(self):
        out = self.add_early({"title": "Porridge for the twins", "start": "6:40 AM", "duration": "30 min"},
                             "get the twins fed")
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_one_already_gone_is_still_refused(self):
        out = self.add_early({"title": "Porridge for the twins", "start": "6:00 AM", "duration": "15 min"},
                             "porridge for the twins at 6:00")
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestATimeClaimOverARefusal(ConversationTestCase):
    def setUp(self):
        super().setUp()
        t = self.manager.tools
        out = t.execute("add_event", {"title": "Stroll by the canal", "start": "1:10 PM", "duration": "15 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_locked_in_over_a_refused_change_is_sent_back(self):
        true = "That didn't go through: Stroll by the canal is still in the 1:00 PM block."
        self.engine.responses = [
            reply('CALL update_event {"event": "Stroll by the canal", "start": "1:10 PM"}'),
            reply("All set: Stroll by the canal is locked in at 1:10 PM."),
            reply(true)]
        out = self.manager.send_user_message("tidy up my afternoon")
        self.assertEqual(out, true)

    def test_a_true_claim_passes(self):
        said = "Stroll by the canal is locked in at 1:10 PM."
        self.engine.responses = [
            reply('CALL update_event {"event": "Stroll by the canal", "start": "1:10 PM"}'),
            reply(said)]
        out = self.manager.send_user_message("the canal stroll is 1:10, it was never 1")
        self.assertEqual(out, said)


if __name__ == "__main__":
    unittest.main()
