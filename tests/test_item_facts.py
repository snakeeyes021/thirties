"""Lasting facts about items, by name (R6): what the user said holds every day, applied by Python."""

import tempfile
import unittest
from pathlib import Path

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.item_facts import OUTSIDE, Lasting, LastingFacts, item_name, lasting_lines, mentions
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.scheduler import StateDatabase


class TestNamesAndMentions(unittest.TestCase):

    def test_one_item_however_its_written(self):
        self.assertEqual(item_name("Cats' dinner"), item_name("cats dinner."))
        self.assertEqual(item_name("Dinner (with an audiobook)"), "dinner")
        self.assertEqual(item_name("anything outside"), OUTSIDE)

    def test_the_users_message_names_an_item(self):
        others = ["Dinner", "Cats' dinner"]
        self.assertTrue(mentions("trim dinner to 30 minutes", "Dinner", others))
        self.assertFalse(mentions("move dinner to 7", "Cats' dinner", others))  # "dinner" is Dinner's too
        self.assertTrue(mentions("feed the cats at 6 today", "Cats' dinner", others))
        self.assertFalse(mentions("plan my evening", "Dinner", others))
        self.assertTrue(mentions(None, "Dinner", others))  # nobody talking: a drag in the app is the user's own


class FactsTestCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched)
        self.t.user_message = "plan my evening"

    def say(self, message):
        self.t.user_message = message

    def run_tool(self, name, args):
        out = self.t.execute(name, args)
        return out

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)


class TestUsualLength(FactsTestCase):

    def setUp(self):
        super().setUp()
        out = self.t.execute("set_item_facts", {"event": "Dinner", "every_day": "true", "length": "1 hour"})
        self.assertEqual(out, "Noted for Dinner, every day: takes 1h.")

    def test_a_new_dinner_gets_its_hour(self):
        out = self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM"})
        self.assertNotIn(NOT_DONE, out)
        self.assertEqual(self.span("Dinner").duration_minutes, 60)

    def test_a_shorter_one_nobody_asked_for_gets_its_hour_too(self):
        # 10-26 and 10-28: dinner was planned for 30 minutes
        out = self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM", "duration": "30 min"})
        self.assertIn("for 1h, its usual length", out)
        self.assertEqual(self.span("Dinner").duration_minutes, 60)

    def test_unless_the_user_asked(self):
        self.say("quick dinner tonight, 30 minutes at 6")
        self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM", "duration": "30 min"})
        self.assertEqual(self.span("Dinner").duration_minutes, 30)

    def test_it_isnt_trimmed_behind_the_users_back(self):
        self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM"})
        out = self.t.execute("update_event", {"event": "Dinner", "duration": "30 min"})
        self.assertTrue(out.startswith(NOT_DONE) and "takes 1h" in out, out)
        out = self.t.execute("add_event", {"title": "Walk", "start": "6:30 PM", "duration": "30 min",
                                           "replace_overlaps": "true"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # carving it would cut it short too
        self.assertEqual(self.span("Dinner").duration_minutes, 60)
        self.say("trim dinner to 30 minutes")
        self.assertNotIn(NOT_DONE, self.t.execute("update_event", {"event": "Dinner", "duration": "30 min"}))
        self.assertEqual(self.span("Dinner").duration_minutes, 30)

    def test_the_hour_is_shown_to_the_model(self):
        self.assertEqual(lasting_lines(LastingFacts(self.sched.state_db)), ["Dinner: takes 1h"])


class TestFixedTime(FactsTestCase):

    def setUp(self):
        super().setUp()
        self.say("the cats' dinner is at 5, it's not allowed to move")
        out = self.t.execute("set_item_facts", {"event": "Cats' dinner", "every_day": "true", "at": "5:00 PM"})
        self.assertEqual(out, "Noted for Cats' dinner, every day: always at 5:00 PM.")
        self.say("plan my evening")

    def test_it_goes_on_at_its_time(self):
        # 10-30: put at 6 with dinner
        out = self.t.execute("add_event", {"title": "Cats' dinner", "start": "6:00 PM", "duration": "15 min"})
        self.assertIn("at 5:00 PM, its fixed time every day", out)
        self.assertEqual(self.span("Cats' dinner").start_dt, at("17:00"))

    def test_it_doesnt_move_unless_the_user_names_it(self):
        self.t.execute("add_event", {"title": "Cats' dinner", "start": "5:00 PM", "duration": "15 min"})
        self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM", "duration": "1 hour"})
        self.say("move dinner to 7")
        out = self.t.execute("update_event", {"event": "Cats' dinner", "start": "7:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE) and "at 5:00 PM every day" in out, out)
        self.assertNotIn(NOT_DONE, self.t.execute("update_event", {"event": "Dinner", "start": "7:00 PM"}))
        self.say("push everything back an hour")
        self.t.execute("add_event", {"title": "Laundry", "start": "4:30 PM", "duration": "1 hour",
                                     "push_later": "true"})
        self.assertEqual(self.span("Cats' dinner").start_dt, at("17:00"))
        self.say("feed the cats at 6 today")
        self.assertNotIn(NOT_DONE, self.t.execute("update_event", {"event": "Cats' dinner", "start": "6:00 PM"}))

    def test_the_redraw_leaves_it(self):
        self.t.execute("add_event", {"title": "Cats' dinner", "start": "5:00 PM", "duration": "15 min"})
        self.t.execute("add_event", {"title": "Reading", "start": "3:00 PM", "duration": "1 hour"})  # not reported
        set_debug_time(at("16:40"))
        self.t.execute("redraw", {})
        self.assertEqual(self.span("Cats' dinner").start_dt, at("17:00"))
        self.assertGreaterEqual(self.span("Reading").start_dt, at("17:15"))


class TestOrderEveryDay(FactsTestCase):

    def test_nothing_outside_until_after_the_shower(self):
        out = self.t.execute("set_item_facts", {"event": "outside", "every_day": "true", "after": "Shower"})
        self.assertEqual(out, "Noted for outside, every day: after shower.")
        self.t.execute("add_event", {"title": "Walk in the park", "start": "4:00 PM", "duration": "1 hour"})
        self.t.execute("add_event", {"title": "Shower", "start": "6:00 PM", "duration": "30 min"})
        set_debug_time(at("15:40"))
        self.t.execute("redraw", {})
        self.assertGreaterEqual(self.span("Walk in the park").start_dt, self.span("Shower").end_dt)

    def test_pt_and_teeth_go_with_the_shower(self):
        self.t.execute("set_item_facts", {"event": "PT", "every_day": "true", "goes_with": "Shower"})
        self.t.execute("set_item_facts", {"event": "Brush teeth", "goes_with": "Shower"})  # goes_with is every day
        for title, start in (("Shower", "4:00 PM"), ("Reading", "4:30 PM"), ("PT", "6:00 PM"),
                             ("Brush teeth", "8:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
        set_debug_time(at("15:40"))
        self.t.execute("redraw", {})
        order = [s.title for s in sorted(self.t.day_plan.spans, key=lambda s: s.start_dt) if not s.is_envelope]
        self.assertEqual(order, ["Shower", "PT", "Brush teeth", "Reading"])  # 10-29: all in one stretch

    def test_a_fact_for_something_not_planned_today(self):
        out = self.t.execute("set_item_facts", {"event": "Grocery run", "every_day": "true", "daylight": "true"})
        self.assertEqual(out, "Noted for Grocery run, every day: needs daylight.")


class TestThePrompt(unittest.TestCase):

    def test_the_frontier_prompt_shows_what_holds_every_day_and_nothing_moot_is_waiting(self):
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.models import EventSpan, SpanCategory
        set_debug_time(at("21:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        LastingFacts(sched.state_db).set(Lasting(name="Cats' dinner", title="Cats' dinner", fixed_at="17:00"))
        sched.state_db.upsert_span(EventSpan("l", "Lunch", at("12:00"), at("12:30"), SpanCategory.ACTIVITY))
        sched.state_db.upsert_span(EventSpan("d", "Dinner", at("18:00"), at("19:00"), SpanCategory.ACTIVITY))
        plan, pending = sched.build_day_plan(DAY)
        manager = ConversationManager(plan, [], pending, sched, inference_engine=MockInferenceEngine(), active_notes=[])
        prompt = manager.brain.build_system_prompt()
        self.assertIn("EVERY DAY (", prompt)
        self.assertIn("- Cats' dinner: always at 5:00 PM", prompt)
        waiting = prompt.split("WAITING FOR AN OUTCOME", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Dinner", waiting)
        self.assertNotIn("Lunch", waiting)  # moot once dinner began (10-23)


class TestSync(unittest.TestCase):

    def test_facts_and_their_clearing_sync_last_write_wins(self):
        a = LastingFacts(StateDatabase(Path(tempfile.mkdtemp()) / "a.sqlite"))
        b = LastingFacts(StateDatabase(Path(tempfile.mkdtemp()) / "b.sqlite"))
        a.set(Lasting(name="Dinner", title="Dinner", minutes=60))
        for record in a.all_records():
            self.assertTrue(b.merge_record(record))
        self.assertEqual(b.get("dinner").minutes, 60)
        self.assertFalse(b.merge_record(a.all_records()[0]))  # nothing newer
        cleared = a.get("dinner")
        cleared.minutes = 0
        a.set(cleared)
        for record in a.all_records():
            b.merge_record(record)
        self.assertIsNone(b.get("dinner"))

    def test_registered_with_sync(self):
        from unittest.mock import MagicMock
        from thirties_core.sync_engine import SyncEngine
        sched = make_scheduler()
        engine = SyncEngine(config=sched.config, state_db=sched.state_db, joplin_engine=MagicMock())
        self.assertIn("item_facts", engine._stores())


if __name__ == "__main__":
    unittest.main()
