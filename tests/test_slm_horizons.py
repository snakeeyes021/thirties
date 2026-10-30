"""Gemma and horizons and the day's actual times (horizon_talk.py): what a Gemini user does with set_horizon,
pin_to_period, show_period, propose_period and log_day_times, a Gemma user can do too."""

import unittest

from tests.fixtures import DAY, at
from tests.test_slm_brain import SlmTestCase, actions
from thirties_core.brains.slm.horizon_talk import read_day_times, read_horizon
from thirties_core.horizons import HorizonCatalog, HorizonPlans


class TestReading(unittest.TestCase):

    def test_the_days_times_in_plain_words(self):
        self.assertEqual(read_day_times("I woke up at 7 and started work at 7:15"),
                         {"woke": "7", "work_started": "7:15"})
        self.assertEqual(read_day_times("done with work"), {"work_ended": "now"})
        self.assertEqual(read_day_times("finished work around 4:30"), {"work_ended": "4:30"})
        self.assertEqual(read_day_times("got to work at 9 and went to bed at 1 last night")["slept"], "1")
        self.assertEqual(read_day_times("slept in until 9:30"), {"woke": "9:30"})
        for not_a_report in ("I'll be done with work at 5", "when am I done with work?", "I'm starting work at 10",
                             "slept 8 hours", "I start work at 9 tomorrow"):
            self.assertEqual(read_day_times(not_a_report), {}, not_a_report)

    def test_horizon_talk_in_plain_words(self):
        def read(text):
            a = read_horizon(text)
            return a.fields if a else None
        self.assertEqual(read("that's a this-year thing"), {"target": "that", "when": "this year"})
        self.assertEqual(read("the gutters are a next month kind of thing"), {"target": "the gutters",
                                                                             "when": "next month"})
        self.assertEqual(read("honestly that's a someday thing"), {"target": "honestly that", "when": "someday"})
        self.assertEqual(read("I want to get to the gutters this month"), {"target": "the gutters",
                                                                           "when": "this month"})
        self.assertEqual(read("put the taxes on Thursday"), {"target": "the taxes", "when": "Thursday", "pin": True})
        self.assertEqual(read("plan my week"), {"topic": "plan", "when": "this week"})
        self.assertEqual(read("let's plan next month"), {"topic": "plan", "when": "next month"})
        self.assertEqual(read("what's on this week?"), {"topic": "show", "when": "this week"})
        self.assertEqual(read("how's the month looking"), {"topic": "show", "when": "this month"})
        for other in ("put the taxes on Thursday at 3", "what's on tomorrow?", "plan my day",
                      "what's the weather this week", "dinner at 6"):
            self.assertIsNone(read(other), other)


class TestGemmaHorizons(SlmTestCase):

    def tell(self, text):
        """A message Python handles by itself: no model call."""
        before = len(self.rt.parse.seen)
        self.m.send_user_message(text)
        self.assertEqual(len(self.rt.parse.seen), before, f"the model was asked about {text!r}")
        return self.m.last_raw_reply

    def test_when_things_belong_and_pinning_them_to_a_day(self):
        out = self.tell("the media server is a this-year thing")
        self.assertEqual(out, "Noted: 'Fix the media server' is for this year.")
        self.assertEqual(HorizonCatalog(self.sched.state_db).items()["t4"]["horizon"], "year")
        out = self.tell("I want to get to the wedding thank-you notes this month")
        self.assertEqual(out, "Noted: 'Write wedding thank-you notes' is for this month.")
        out = self.tell("put the costco customer service on Saturday")
        self.assertEqual(out, "'Costco customer service' is planned for Saturday.")
        rows = HorizonPlans(self.sched.state_db).rows()
        self.assertEqual([(r["item_id"], r["slot"]) for r in rows], [("t1", "2026-10-17")])
        out = self.tell("what's on this week?")
        self.assertTrue(out.startswith("This week (Oct 12 – 18):\nOn track, 0 of 1 done, 4 days left.\n"
                                       "- Saturday: Costco customer service"), out)

    def test_that_is_the_item_just_planned(self):
        self.say("fix the media server at 7", {"type": "add", "title": "Fix the media server", "start": "7",
                                               "duration": "1 hour"})
        self.assertEqual(self.tell("eh, that's a someday thing"), "Noted: 'Fix the media server' is someday.")

    def test_moving_whats_on_the_schedule_isnt_pinning(self):
        self.say("move the dentist to Thursday", {"type": "move", "target": "dentist", "to_day": "Thursday"})
        self.assertEqual(HorizonPlans(self.sched.state_db).rows(), [])

    def test_the_model_names_it_when_the_words_arent_plain(self):
        out = self.say("honestly the media server can sit until next month",
                       {"type": "horizon", "target": "media server", "when": "next month"})
        self.assertEqual(out, "'Fix the media server' is planned for November.")

    def test_plan_my_week_then_yes(self):
        self.tell("the media server is a this-week thing")
        out = self.tell("plan my week")
        self.assertTrue(out.startswith("For this week I'd plan:\n- "), out)
        self.assertTrue(out.endswith("Want this week like that?"))
        out = self.say("yes please", {"type": "confirm"})
        self.assertTrue(out.startswith("Planned for this week: "), out)
        self.assertTrue(HorizonPlans(self.sched.state_db).rows())


class TestGemmaDayTimes(SlmTestCase):

    def work(self):
        return next(s for s in self.m.day_plan.spans if s.title == "Work")

    def test_woke_and_started_work_then_done(self):
        before = len(self.rt.parse.seen)
        self.m.send_user_message("I woke up at 7 and started work at 7:15")
        self.assertEqual(self.m.last_raw_reply, "Recorded: woke 7:00 AM, started work 7:15 AM.")
        self.assertEqual(len(self.rt.parse.seen), before)  # Python read it: no model call
        self.assertEqual((self.work().actual_start, self.work().start_dt), (at("07:15"), at("07:15")))
        self.m.send_user_message("done with work")
        self.assertEqual(self.m.last_raw_reply, "Recorded: finished work 4:40 PM.")
        self.assertEqual(self.work().actual_end, at("16:40"))

    def test_in_a_message_with_other_requests_python_reads_its_part(self):
        self.rt.parse_replies += [actions({"type": "move", "target": "work", "start": "9:40"}),
                                  actions({"type": "add", "title": "Laundry", "start": "7", "duration": "1 hour"})]
        self.m.send_user_message("started work at 9:40, and add laundry at 7")
        out = self.m.last_raw_reply
        self.assertIn("Recorded: started work 9:40 AM.", out)
        self.assertEqual((self.work().actual_start, self.work().planned_start), (at("09:40"), at("08:30")))
        self.assertTrue(any(s.title == "Laundry" for s in self.m.day_plan.spans))

    def test_a_short_answer_to_the_morning_question(self):
        self.m.add_assistant_message("☀️ For the record: when did you wake up, and when did you start work?")
        self.m.send_user_message("6:45 and 8:40")
        self.assertEqual(self.m.last_raw_reply, "Recorded: woke 6:45 AM, started work 8:40 AM.")

    def test_yes_to_done_with_work(self):
        self.m.add_assistant_message("💼 Done with work? If so, when did you finish? (It was planned until 4:00 PM.)")
        self.m.send_user_message("yep, at 4:30")
        self.assertEqual(self.m.last_raw_reply, "Recorded: finished work 4:30 PM.")


if __name__ == "__main__":
    unittest.main()
