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

    def test_every_item_however_many(self):
        titles = [f"Errand {n}" for n in range(20)]
        text = hello_text("Morning!", titles)
        self.assertEqual(text.splitlines()[3:], titles)  # every one, never a count in their place
        self.assertNotIn("more", text)

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
        self.assertIsNone(check_line("It is 7:00, your wake-up time! Tell me when you're up.", "wake", at("07:00"),
                                     late=True))
        self.assertTrue(check_line("It is 7:00, your wake-up time! Tell me when you're up.", "wake", at("07:00"),
                                   late=False))  # on time

    def test_a_few_minutes_late_is_still_on_time(self):
        from thirties_core.wake import FALLBACK, write_hello
        self.assertEqual(write_hello(None, "wake", at("07:00"), [], now=at("07:08")), FALLBACK["wake"].format(clock="7:00 AM"))
        from thirties_core.wake import FALLBACK_LATE
        self.assertEqual(write_hello(None, "sunrise", at("06:52"), [], now=at("07:30")),
                         FALLBACK_LATE["sunrise"].format(clock="6:52 AM"))

    def test_seven_oclock_can_be_said_as_7am(self):
        from thirties_core.wake import check_line
        self.assertTrue(check_line("7am, the wake-up time you set. Let me know when you're up.", "wake", at("07:00")))
        self.assertIsNone(check_line("Rise and shine! Want me to plan the day?", "wake", at("07:00")))


class TestNotAnAlarmClock(unittest.TestCase):
    """The one message at the wake time never says or suggests the user is up: Thirties can't know that. It asks to
    hear when they are, and nothing else goes out until they reply."""

    def test_wording_that_says_theyre_up_is_refused(self):
        from thirties_core.wake import check_line
        for line in ("Good morning! It's 7:00, just as you planned for your wake-up time. Let me know when you're "
                     "ready.",
                     "It's 7:00 and you're up! Tell me when you're ready to plan.",
                     "Rise and shine, it's 7:00! Tell me when you're up.",
                     "Morning, it's 7:00. Hope you slept well! Let me know when you're up.",
                     "It's 7:00, time to get up! Let me know when you're up.",
                     "It's 7:00, right on schedule. Let me know when you're ready.",
                     "7am: the alarm went off! Let me know when you're up.",
                     "It’s 7:00 and you’re awake, let’s get started once you’re ready."):
            self.assertIsNone(check_line(line, "wake", at("07:00")), line)

    def test_asking_to_hear_when_theyre_up_is_fine(self):
        from thirties_core.wake import check_line
        for line in ("Hello! 7:00 is the wake-up time you chose. Tell me when you're up and I'll lay out the hours.",
                     "Hi, 7:00, your wake up time. Once you are awake, drop me a line and the schedule follows.",
                     "Hey, 7:00 is your set wake-up time. As soon as you are out and about, send a word."):
            self.assertTrue(check_line(line, "wake", at("07:00")), line)

    def test_it_must_ask_to_hear_when_theyre_up(self):
        from thirties_core.wake import check_line
        self.assertIsNone(check_line("Good morning, it's 7:00, your wake-up time.", "wake", at("07:00")))

    def test_the_prompt_says_so_and_a_refused_line_gives_the_plain_one(self):
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.wake import FALLBACK, claims_awake, write_hello
        engine = MockInferenceEngine()
        engine.responses = [{"role": "assistant", "content": "Good morning! It's 7:00, just as you planned. Let me "
                                                             "know when you're ready.", "tool_calls": []}]
        line = write_hello(engine, "wake", at("07:00"), [])
        self.assertEqual(line, FALLBACK["wake"].format(clock="7:00 AM"))
        self.assertIn("when you're up", line)
        self.assertFalse(claims_awake(line))
        prompt = engine.call_history[0][0]["content"]
        self.assertIn("not an alarm clock", prompt)
        self.assertIn("never say or suggest they're up", prompt)

    def test_every_plain_line_passes_its_own_check(self):
        from thirties_core.wake import FALLBACK, FALLBACK_LATE, check_line
        for late, lines in ((False, FALLBACK), (True, FALLBACK_LATE)):
            for mode, text in lines.items():
                self.assertTrue(check_line(text.format(clock="7:00 AM"), mode, at("07:00"), late), (mode, late))

    def test_one_message_then_nothing_until_they_reply(self):
        from unittest.mock import MagicMock
        from thirties_core.astronomy import set_debug_time
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        sched = make_scheduler()
        sent = []
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        svc = BackgroundService(sched.config, lambda t, b, k: sent.append((k, b)), scheduler=sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: DAY
        plan = svc.manager().day_plan
        from thirties_core.wake import hello_time
        when = hello_time(sched.config, plan, sched.state_db.get_spans_between(plan.day_start - timedelta(hours=18),
                                                                                plan.day_start))
        self.addCleanup(set_debug_time, None)
        for minutes in (0, 5, 30, 90):
            set_debug_time(when + timedelta(minutes=minutes))
            svc.morning_hello(when + timedelta(minutes=minutes))
        self.assertEqual([k for k, _ in sent], ["morning"])
        self.assertIn("when you're up", sent[0][1])
