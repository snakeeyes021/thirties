"""What the user reads about times is in the app's own format, and a long item reads as one item.

- The line added under a reply that gave wrong times named an item by a title that opened with a 24-hour time of
  its own, next to the plan's 12-hour time, and the user had to ask which was meant (plain_corrections.py).
- An item lasting several hours, with other things starting during it, was a row in every half hour it touched,
  and read as several items (clock.once_lines). DAY is Thu Oct 15 (Work 8:30 AM - 4 PM); invented items.
"""

import re
import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.plain_corrections import in_app_words, on_the_plan, plain_name, twelve_hour

TWENTY_FOUR_HOUR = re.compile(r"\b(?:1[3-9]|2[0-3]|0\d):[0-5]\d\b")


class TestTheAppsOwnWords(unittest.TestCase):

    def test_a_title_without_the_time_it_opens_with(self):
        self.assertEqual(plain_name("21:30 Lock the back door"), "Lock the back door")
        self.assertEqual(plain_name("6:30am - Water the ferns"), "Water the ferns")
        self.assertEqual(plain_name("Flight (boards 2:25)"), "Flight (boards 2:25)")
        self.assertEqual(plain_name("10:00"), "10:00")  # nothing but a time: left as it is

    def test_times_in_twelve_hour_form(self):
        self.assertEqual(twelve_hour("at 21:30"), "at 9:30 PM")
        self.assertEqual(twelve_hour("from 06:15"), "from 6:15 AM")
        self.assertEqual(twelve_hour("00:30"), "12:30 AM")
        self.assertEqual(twelve_hour("at 10:00 and 9:30 PM"), "at 10:00 and 9:30 PM")

    def test_one_plain_sentence(self):
        self.assertEqual(on_the_plan(["Water the ferns is at 6:00 PM"]), "(On the plan, Water the ferns is at 6:00 PM.)")
        self.assertEqual(on_the_plan(["A is at 3:00 PM", "B is at 9:30 PM", "C is at 10:00 PM"]),
                         "(On the plan, A is at 3:00 PM, B is at 9:30 PM and C is at 10:00 PM.)")

    def test_the_added_lines(self):
        lines = in_app_words(["(On the plan, 21:30 Lock the back door is at 9:30 PM.)"],
                             ["21:30 Lock the back door", "Water the ferns"])
        self.assertEqual(lines, ["(On the plan, Lock the back door is at 9:30 PM.)"])


class TestTheAddedLineInAReply(ConversationTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("17:25"))
        self.manager.execute_tool("add_event", {"title": "Water the ferns", "start": "6:00 PM", "duration": "30 min"})
        self.manager.execute_tool("add_event", {"title": "21:30 Lock the back door", "start": "9:30 PM",
                                                "duration": "30 min"})

    def test_no_24_hour_time_and_one_time_per_item(self):
        for _ in range(3):
            self.engine.set_next_response(reply("The ferns are at 6:15, and Lock the back door is at 9:00."))
        out = self.manager.send_user_message("what's later?")
        added = out.split("\n\n")[-1]
        self.assertTrue(added.startswith("(On the plan, "), out)
        self.assertIn("Lock the back door is at 9:30", added)
        self.assertNotIn("21:30", added)
        self.assertIsNone(TWENTY_FOUR_HOUR.search(added), added)


if __name__ == "__main__":
    unittest.main()
