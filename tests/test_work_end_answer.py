"""The answer to the background's "Done with work?": a short yes or a time is recorded by Python (log_day_times),
and the reply says what's left after work (after_work.offer), with no model turn. Invented day: work 8:30 AM - 4 PM,
bed at 11 PM."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.day_times import work_span
from thirties_core.inference import MockInferenceEngine
from thirties_core.work_end_answer import work_ended_from


class TestTheShortAnswer(unittest.TestCase):

    def test_what_counts_as_an_answer(self):
        for said, ended in (("yes", "now"), ("Yep!", "now"), ("just now", "now"), ("done", "now"), ("👍", "now"),
                            ("4:10", "4:10"), ("at 4", "4"), ("yes, at 3:45 pm", "3:45 pm"), ("finished at noon", "noon")):
            self.assertEqual(work_ended_from(said), ended, said)
        for said in ("not yet", "no", "still working", "almost", "will be at 5", "when did I finish?",
                     "yes, and can you move the bins to 8", "about 2 more hours", ""):
            self.assertIsNone(work_ended_from(said), said)


class TestDoneWithWork(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("16:05"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"  # the question is asked at this level
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.engine = MockInferenceEngine()
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append(b), scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.run_in_threads = False
        self.manager = self.svc.manager()
        self.svc.day_times(at("16:05"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Done with work?", self.sent[0])

    def work(self):
        return work_span(self.sched.build_day_plan(DAY)[0])

    def test_a_yes_records_now_and_offers_whats_left(self):
        set_debug_time(at("16:20"))
        out = self.manager.send_user_message("yes", via="telegram")
        self.assertEqual(self.engine.call_history, [])  # Python answered: no model turn
        self.assertTrue(out.startswith("Noted: finished work 4:20 PM.\n\nAfter work: "), out)
        self.assertIn("After work: ", out)
        self.assertIn("dark thirties free before bed", out)
        self.assertEqual(self.work().actual_end, at("16:20"))
        said = [m["content"] for m in self.manager.brain.messages if m.get("role") in ("user", "assistant")]
        self.assertEqual(said[-2:], ["yes", out])  # in the one conversation, like any exchange

    def test_a_time_is_works_end(self):
        set_debug_time(at("16:40"))
        out = self.manager.send_user_message("at 4:10")
        self.assertEqual(self.engine.call_history, [])
        self.assertIn("After work: ", out)
        self.assertEqual(self.work().actual_end, at("16:10"))

    def test_anything_else_goes_to_the_assistant(self):
        set_debug_time(at("16:20"))
        self.engine.set_next_response(reply("Still at it, then. I'll keep the evening as it is."))
        out = self.manager.send_user_message("not yet")
        self.assertEqual(len(self.engine.call_history), 1)
        self.assertIsNone(self.work().actual_end)
        self.assertNotIn("After work:", out)

    def test_only_right_after_the_question(self):
        set_debug_time(at("16:20"))
        self.engine.set_next_response(reply("Sure."))
        self.manager.send_user_message("can you move the bins to 8")
        self.engine.set_next_response(reply("Glad to hear it."))
        self.manager.send_user_message("yes")  # the question isn't the last thing said any more
        self.assertEqual(len(self.engine.call_history), 2)
        self.assertIsNone(self.work().actual_end)

    def test_not_once_the_question_has_gone_stale(self):
        set_debug_time(at("18:30"))
        self.engine.set_next_response(reply("Glad to hear it."))
        self.manager.send_user_message("yes")
        self.assertEqual(len(self.engine.call_history), 1)
        self.assertIsNone(self.work().actual_end)


if __name__ == "__main__":
    unittest.main()
