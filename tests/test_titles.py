"""Short titles, descriptions, and never a raw id as a title (titles.py; 11-06, Matt's Agenda screenshot)."""

import json
import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_today import TodayTestCase
from thirties_core.titles import ItemNotes, description_for, is_raw_id, repair_raw_titles, split_title

NAS = ("**NAS: change the `pm-agent` password** (it leaked into an agent log on 11-04; then give the PM the new "
       "one) · the two fixes below are done (full report: https://redmax.example.net:8411/nas-inventory.md)")


class TestSplit(unittest.TestCase):

    def test_a_long_line_is_a_headline_and_a_description(self):
        title, description = split_title(NAS)
        self.assertEqual(title, "NAS: change the pm-agent password")
        self.assertNotIn("**", description + title)
        self.assertNotIn("`", description + title)
        self.assertNotIn("https://", description)
        self.assertIn("it leaked into an agent log on 11-04", description)

    def test_a_short_line_is_all_title(self):
        self.assertEqual(split_title("Thank yous"), ("Thank yous", ""))
        self.assertEqual(split_title("Read [the doc](https://x.y/z)")[0], "Read the doc")

    def test_raw_ids(self):
        self.assertTrue(is_raw_id("31a1d3808b2c9b79"))
        self.assertFalse(is_raw_id("Thank yous"))


class TestItemsKeepTheirTitles(TodayTestCase):

    def test_propose_day_never_makes_an_id_the_title(self):
        # 11-06: the model named "Thank yous" by an old id; the item went on as 31a1d3808b2c9b79
        self.sched.state_db.set_sync_meta("day_proposal:2026-10-01", json.dumps(
            {"items": [{"title": "Call the bank", "task_id": "31a1d3808b2c9b79", "start": "x", "minutes": 30}]}))
        self.t.execute("propose_day", {"items": [{"item": "31a1d3808b2c9b79", "start": "4:30 PM"}]})
        titles = [s.title for s in self.t.day_plan.spans]
        self.assertIn("Call the bank", titles)
        self.assertFalse(any(is_raw_id(t) for t in titles))
        out = self.t.execute("propose_day", {"items": [{"item": "0123456789abcdef", "start": "5:30 PM"}]})
        self.assertIn("doesn't know", out)  # an id nobody knows: refused, never used as a title

    def test_add_event_never_makes_an_id_the_title(self):
        out = self.t.execute("add_event", {"title": "fedcba9876543210", "start": "6:00 PM", "duration": "30 min"})
        self.assertIn("id Thirties doesn't know", out)

    def test_records_already_affected_are_repaired(self):
        from thirties_core.models import EventSpan, SpanCategory
        db = self.sched.state_db
        db.set_sync_meta("day_proposal:2026-10-02", json.dumps(
            {"items": [{"title": "Thank yous", "task_id": "31a1d3808b2c9b79", "start": "x", "minutes": 30}]}))
        db.upsert_span(EventSpan("bad", "31a1d3808b2c9b79", at("19:00"), at("19:30"), SpanCategory.ACTIVITY))
        self.assertEqual(repair_raw_titles(db, self.services.tasks), 1)
        span = next(s for s in self.sched.build_day_plan(DAY)[0].spans if s.id == "bad")
        self.assertEqual((span.title, span.task_id), ("Thank yous", "31a1d3808b2c9b79"))

    def test_the_chat_schedule_says_the_short_title(self):
        self.t.execute("add_event", {"title": NAS, "start": "4:00 PM", "duration": "30 min"})
        text = self.t.schedule_text()
        self.assertIn("NAS: change the pm-agent password", text)
        self.assertNotIn("leaked", text)

    def test_a_note_the_user_sets_is_the_description(self):
        out = self.t.execute("set_item_note", {"item": "Call the bank", "note": "ask about the old account"})
        self.assertIn("Noted for Call the bank", out)
        task = next(t for t in self.services.tasks if t.title == "Call the bank")
        self.assertEqual(description_for(self.sched.state_db, "Call the bank", task.id), "ask about the old account")
        self.assertEqual(ItemNotes(self.sched.state_db).get(task.id), "ask about the old account")
