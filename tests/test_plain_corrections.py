"""What the user reads about times is in the app's own format, and a long item reads as one item.

- The plain sentence said for a reply that gave wrong times named an item by a title that opened with a 24-hour
  time of its own, next to the plan's 12-hour time, and the user had to ask which was meant (plain_corrections.py).
- An item lasting several hours, with other things starting during it, was a row in every half hour it touched,
  and read as several items (clock.once_lines). DAY is Thu Oct 15 (Work 8:30 AM - 4 PM); invented items.
"""

import re
import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.plain_corrections import in_app_words, plain_name, twelve_hour

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

    def test_the_sentences_said(self):
        lines = in_app_words(["21:30 Lock the back door is at 9:30 PM."], ["21:30 Lock the back door", "Water the ferns"])
        self.assertEqual(lines, ["Lock the back door is at 9:30 PM."])


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
        added = out  # said in place of the wrong sentence, never as a line under it (plain_admit.py)
        self.assertNotIn("(On the plan", out)
        self.assertIn("Lock the back door is at 9:30", added)
        self.assertNotIn("21:30", added)
        self.assertIsNone(TWENTY_FOUR_HOUR.search(added), added)


class TestALongItemIsOneLine(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        for sid, title, start, end in (("art", "Export the print files", "09:35", "12:45"),
                                       ("inv", "Send the invoices", "12:30", "13:00"),
                                       ("ico", "Icon sketches", "13:00", "15:00"),
                                       ("call", "Review call", "15:30", "16:00")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.TASK))
        self.plan, _ = self.sched.build_day_plan(DAY)

    def rows(self, start=None):
        from thirties_core.messages import schedule_rows
        return schedule_rows(self.plan, start)

    def test_one_line_with_its_start_and_end(self):
        rows = self.rows()
        art = [r for r in rows if "Export the print files" in r]
        self.assertEqual(art, ["- 9:30 AM – Export the print files (until 1:00 PM)"], rows)  # the end of its block
        self.assertEqual(sum("Work" in r for r in rows), 1, rows)  # Work too: once, with its end
        self.assertIn("- 8:30 AM – Work (until 4:00 PM)", rows)
        self.assertIn("- 12:30 PM – Send the invoices", rows)  # during the export: its own line
        self.assertIn("- 1:00 PM – Icon sketches (until 3:00 PM)", rows)  # a gap after it: its end

    def test_followed_straight_on_needs_no_end(self):
        self.sched.state_db.upsert_span(EventSpan("ico", "Icon sketches", at("13:00"), at("15:30"),
                                                  SpanCategory.TASK))
        self.plan, _ = self.sched.build_day_plan(DAY)
        self.assertIn("- 1:00 PM – Icon sketches", self.rows())

    def test_the_schedule_and_whats_next_show_it_once(self):
        from thirties_core.messages import render_schedule
        from thirties_core.whats_next import render
        shown = render_schedule(self.plan, at("09:10"))
        self.assertEqual(shown.count("Export the print files"), 1, shown)
        self.assertIn("Export the print files (until 1:00 PM)", shown)
        nxt = render(self.plan, at("09:10"), mode="rest")
        self.assertEqual(nxt.count("Export the print files"), 1, nxt)


if __name__ == "__main__":
    unittest.main()
