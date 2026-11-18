"""A time the user gave for an item wins over Thirties' own placing: a routine, a leftover (routine_times.py).

A routine step the user gave a time for ("log the firing at 2:05") went at the routine's usual time when
put_routine placed it, and stayed there. Covered here: this message, an earlier message today, another day's plan,
a time that has passed, the routine's own name, a leftover, and a reply saying the time.

On invented items only: someone who fires a pottery kiln on a late shift and paddles a canoe.
"""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_conversation import reply
from tests.test_routines import RoutineCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE
from thirties_core.shown_times import kept_times, span_when, start_exact

KILN = ["Vent the kiln", "Log the firing", "Sweep the glaze bench"]

class KilnCase(RoutineCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_kiln"] = {"id": "n_kiln", "title": "Kiln check",
                                    "body": "".join(f"- [ ] {s}\n" for s in KILN)}
        self.catalog.set_role("n_kiln", "routine", "Kiln check")

    def put(self, message, start="2:00 PM", tools=None, **extra):
        tools = tools or self.t
        tools.user_message = message
        try:
            out = tools.execute("put_routine", {"routine": "Kiln check", "parts": [{"start": start, "items": KILN}],
                                                **extra})
        finally:
            tools.user_message = None
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out

    def step(self, title, tools=None):
        return next(s for s in (tools or self.t).day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED)

    def earlier(self, text, hour="07:30", day=DAY, tools=None):
        (tools or self.t).said_earlier = [(at(hour).replace(day=day.day).isoformat(), text)]

class TestAStepTimeTheUserGaveThisMessage(KilnCase):

    def test_the_step_goes_at_the_users_time_not_the_routines(self):
        self.put("kiln check this afternoon, and log the firing at 2:05")
        log = self.step("Log the firing")
        self.assertEqual((log.start_dt, log.given_start), (at("14:05"), at("14:05")))
        self.assertTrue(start_exact(log))
        self.assertTrue(span_when(log).startswith("2:05 PM"), span_when(log))
        self.assertIn(at("14:05"), kept_times(log))  # a reply may say it as said
        vent = self.step("Vent the kiln")
        self.assertIsNone(vent.given_start)  # its time is the routine's own
        self.assertEqual(vent.start_dt, at("14:00"))

    def test_two_steps_each_get_their_own(self):
        self.put("vent the kiln at 10:20, then log the firing at 2:05 pm", start="10:00 AM")
        self.assertEqual(self.step("Vent the kiln").given_start, at("10:20"))
        self.assertEqual(self.step("Log the firing").given_start, at("14:05"))
        self.assertIsNone(self.step("Sweep the glaze bench").given_start)

    def test_the_routines_own_name_gives_no_step_a_time(self):
        self.put("kiln check at 2:05 please")
        for title in KILN:
            self.assertIsNone(self.step(title).given_start, title)

    def test_with_no_message_nothing_is_the_users(self):
        self.put(None)
        self.assertTrue(all(self.step(t).given_start is None for t in KILN))

class TestAStepTimeGivenEarlierToday(KilnCase):

    def test_an_earlier_message_still_stands(self):
        self.earlier("heads up, I'll log the firing at 2:05 today")
        self.put("ok put the kiln check on")
        log = self.step("Log the firing")
        self.assertEqual((log.start_dt, log.given_start), (at("14:05"), at("14:05")))

    def test_a_newer_time_replaces_an_older_one(self):
        self.t.said_earlier = [(at("07:00").isoformat(), "log the firing at 2:05"),
                               (at("07:40").isoformat(), "actually log the firing at 2:40")]
        self.put("kiln check on please")
        self.assertEqual(self.step("Log the firing").given_start, at("14:40"))

    def test_one_that_has_passed_no_longer_stands(self):
        set_debug_time(at("15:10"))
        self.earlier("log the firing at 2:05")
        self.put("kiln check now-ish", start="3:30 PM")
        log = self.step("Log the firing")
        self.assertIsNone(log.given_start)
        self.assertGreaterEqual(log.start_dt, at("15:30"))

    def test_a_time_for_tomorrow_doesnt_time_today(self):
        self.earlier("tomorrow I need to log the firing at 2:05")
        self.put("kiln check this afternoon")
        self.assertIsNone(self.step("Log the firing").given_start)

    def test_yesterdays_words_dont_count(self):
        self.earlier("log the firing at 2:05", day=DAY - timedelta(days=1))
        self.put("kiln check this afternoon")
        self.assertIsNone(self.step("Log the firing").given_start)

class TestAnotherDay(KilnCase):

    def test_both_times_given_for_tomorrow_are_applied(self):
        self.put("tomorrow: vent the kiln at 10:20 and log the firing at 2:05", start="10:00 AM", day="tomorrow")
        plan = self.t.plan_for("tomorrow")
        vent = next(s for s in plan.spans if s.title == "Vent the kiln")
        log = next(s for s in plan.spans if s.title == "Log the firing")
        self.assertEqual(vent.given_start, at("10:20", 1))
        self.assertEqual(log.given_start, at("14:05", 1))

    def test_an_earlier_message_naming_that_day_counts(self):
        self.earlier("tomorrow log the firing at 2:05")
        self.put("kiln check tomorrow morning", start="10:00 AM", day="tomorrow")
        log = next(s for s in self.t.plan_for("tomorrow").spans if s.title == "Log the firing")
        self.assertEqual(log.given_start, at("14:05", 1))

class TestALeftover(KilnCase):

    def test_a_leftover_placed_today_goes_at_the_time_the_user_gave(self):
        self.services.tasks.append(item("t_hull", "Wax the canoe hull"))
        self.services.tasks = self.catalog.apply(self.services.tasks)
        self.earlier("I'm waxing the canoe hull at 5:20 tonight")
        placed, overflow = self.t.assistant.before.place_today([
            {"title": "Wax the canoe hull", "task_id": "t_hull", "start": "", "end": DAY.isoformat()}])
        self.assertEqual((placed, overflow), (["Wax the canoe hull"], []))
        hull = self.step("Wax the canoe hull")
        self.assertEqual((hull.start_dt, hull.given_start), (at("17:20"), at("17:20")))

class TestTheReplySaysIt(KilnCase):
    """Through a conversation with a scripted model: the time given in the first message is applied by the
    routine placed in the second, and the reply saying it passes the checks."""

    def setUp(self):
        super().setUp()
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        from thirties_core import morning
        self.engine = MockInferenceEngine()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, self.services.tasks, pending, self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        morning.mark_done(self.sched.state_db, DAY, at("07:00"))

    def test_an_earlier_step_time_is_applied_and_may_be_said(self):
        self.engine.responses = [reply("Noted.")]
        self.m.send_user_message("I'll log the firing at 2:05 today")
        said = "The kiln check is on from 2:00 PM, and Log the firing is at 2:05 PM."
        self.engine.responses = [
            reply('CALL put_routine {"routine": "Kiln check", "start": "2:00 PM"}'),
            reply(said)]
        out = self.m.send_user_message("put the kiln check on this afternoon")
        log = next(s for s in self.m.day_plan.spans if s.title == "Log the firing")
        self.assertEqual((log.start_dt, log.given_start), (at("14:05"), at("14:05")))
        self.assertEqual(out, said)

if __name__ == "__main__":
    unittest.main()
