"""When the user's day has started (wake.py): the one trigger for the morning."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core.wake import hello_text, user_is_up


class TestUp(unittest.TestCase):

    def setUp(self):
        self.sched = make_scheduler()
        self.plan = self.sched.build_day_plan(DAY)[0]
        self.db = self.sched.state_db

    def test_up_once_they_write_on_the_day(self):
        self.assertFalse(user_is_up(self.db, self.plan, at("09:00")))
        mark_up(self.db, at("09:10"))
        self.assertTrue(user_is_up(self.db, self.plan, at("09:15")))
        self.assertTrue(user_is_up(self.db, self.plan, at("22:00")))

    def test_last_nights_message_doesnt_count_once_theyve_slept(self):
        # 11-04 replay: a message at 10 PM, then at 6:30 (sleep over, sunrise not yet) it was still "yesterday",
        # where the user had been up, and the week's planning message went out
        mark_up(self.db, at("22:00"))
        sleep = next(s for s in self.plan.spans if s.category.value == "sleep" and s.start_dt > at("12:00"))
        self.assertTrue(user_is_up(self.db, self.plan, sleep.start_dt - timedelta(minutes=5)))
        self.assertFalse(user_is_up(self.db, self.plan, sleep.end_dt + timedelta(minutes=1)))


class TestHello(unittest.TestCase):
    """11-05: the model writes the first message fresh each morning; Python checks it has what it must."""

    def test_the_items_one_per_line_without_bullets(self):
        self.assertEqual(hello_text("Morning!", []), "Morning!")
        self.assertEqual(hello_text("Morning!", ["Dentist", "dentist", "Call"]),
                         "Morning!\n\nOn the plate today:\nDentist\nCall")

    def test_the_model_writes_it_and_python_checks_it(self):
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.wake import write_hello
        engine = MockInferenceEngine()
        engine.responses = [{"role": "assistant", "content": "6:47 and the sun is up. Morning! Ready to plan when "
                                                             "you are.", "tool_calls": []}]
        line = write_hello(engine, "sunrise", at("06:47"), [])
        self.assertTrue(line.startswith("☀️ "), line)  # sunrise mode always has the sun
        self.assertIn("6:47", line)
        prompt = engine.call_history[0][0]["content"]
        self.assertIn("6:47 AM", prompt)
        self.assertIn("sunrise", prompt)

    def test_without_the_time_or_a_model_its_the_plain_fallback(self):
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.wake import FALLBACK, write_hello
        engine = MockInferenceEngine()
        engine.responses = [{"role": "assistant", "content": "Good morning! Ready when you are.", "tool_calls": []}]
        self.assertEqual(write_hello(engine, "wake", at("07:00"), []), FALLBACK["wake"].format(clock="7:00 AM"))
        self.assertEqual(write_hello(None, "sunrise", at("06:52"), []), FALLBACK["sunrise"].format(clock="6:52 AM"))

    def test_sent_late_it_never_claims_the_time_is_now(self):
        """The computer was asleep at 7:00 and the greeting goes out at 8:30: never "it's 7:00"."""
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.wake import FALLBACK_LATE, check_line, write_hello
        engine = MockInferenceEngine()
        engine.responses = [{"role": "assistant", "content": "Good morning, it's 7:00! Ready when you are.",
                             "tool_calls": []}]
        line = write_hello(engine, "wake", at("07:00"), [], now=at("08:30"))
        self.assertEqual(line, FALLBACK_LATE["wake"].format(clock="7:00 AM"))  # the model's "it's 7:00" refused
        self.assertNotRegex(line, r"(?i)\bit'?s 7|\bit is 7")
        self.assertIn("has passed", engine.call_history[0][0]["content"])  # the prompt says it's past
        self.assertTrue(check_line("Morning! Your 7:00 wake-up time has passed; ready when you are.", "wake",
                                   at("07:00"), late=True))
        self.assertIsNone(check_line("It is 7:00, your wake-up time!", "wake", at("07:00"), late=True))
        self.assertTrue(check_line("It is 7:00, your wake-up time!", "wake", at("07:00"), late=False))  # on time

    def test_a_few_minutes_late_is_still_on_time(self):
        from thirties_core.wake import FALLBACK, write_hello
        self.assertEqual(write_hello(None, "wake", at("07:00"), [], now=at("07:08")), FALLBACK["wake"].format(clock="7:00 AM"))
        from thirties_core.wake import FALLBACK_LATE
        self.assertEqual(write_hello(None, "sunrise", at("06:52"), [], now=at("07:30")),
                         FALLBACK_LATE["sunrise"].format(clock="6:52 AM"))

    def test_seven_oclock_can_be_said_as_7am(self):
        from thirties_core.wake import check_line
        self.assertTrue(check_line("7am means the alarm is ringing! Want me to plan the day?", "wake", at("07:00")))
        self.assertIsNone(check_line("Rise and shine! Want me to plan the day?", "wake", at("07:00")))
