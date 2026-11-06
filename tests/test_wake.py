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

    def test_the_items_without_times(self):
        self.assertEqual(hello_text([]), "☀️ Good morning! Let me know when you're up and I'll put your schedule "
                                         "together.")
        self.assertTrue(hello_text(["Dentist", "dentist", "Call"]).endswith("On the plate today:\n- Dentist\n- Call"))
