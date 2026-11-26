"""The morning's word on the leftovers from before agrees with itself and with the plan (leftover_facts.py).

The counts and lines Python writes, the facts the model is given, what propose_leftovers answers, and the model's
own morning words all come from (or are checked against) the plan as it stands once the day is built: a kept item
already on today's plan is counted there, one taken off it isn't, and a reply that miscounts or misplaces them
goes back for a rewrite, then gives way to Python's notice.

Invented: a potter on the late shift at a studio, with a canoe on days off."""

import re
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_unknown_stays_off_today import STUDIO, StudioCase, YESTERDAY, studio_item
from thirties_core import leftover_facts
from thirties_core.astronomy import set_debug_time


def on_plan_count(text):
    m = re.search(r"\((\d+) on today's plan\)", text)
    return int(m.group(1)) if m else None


# Python's first guess places only leftovers never planned on a day now over (carry.replan_leftovers); these
# items are planned yesterday just to make them leftovers, so they count as plain list items here.
class AsPlainListItems:
    def setUp(self):
        p = patch("thirties_core.past_one_offs.planned_over", new=lambda facts, day: False)
        p.start()
        self.addCleanup(p.stop)
        super().setUp()


class MorningCase(StudioCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:45"))

    def unmarked_yesterday(self, *titles):
        for i, (tid, title) in enumerate(t for t in STUDIO if t[1] in titles):
            self.t.execute("add_event", {"title": title, "start": f"{2 + i}:20 PM", "duration": "30 min",
                                         "day": YESTERDAY.isoformat(), "task_id": tid, "category": "task"})
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")

    def on_today(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.start_dt.date() == DAY]


class TestTheNoticeCountsThePlan(AsPlainListItems, MorningCase):

    def test_kept_for_the_week_but_already_on_today(self):
        self.unmarked_yesterday("Sand the spare paddle", "Order a bag of porcelain")
        self.t.execute("add_event", {"title": "Sand the spare paddle", "start": "3:20 PM", "duration": "30 min",
                                     "task_id": "t_paddle", "category": "task"})
        text = self.t.execute("rollover_proposal", {})
        self.assertEqual(on_plan_count(text), 1, text)
        self.assertIn("Sand the spare paddle, today at", text)
        self.assertIn("Order a bag of porcelain, tomorrow", text)

    def test_meant_for_today_but_taken_off_since(self):
        self.task("t_permit").due = DAY.isoformat()
        self.unmarked_yesterday("Renew the kiln permit", "Order a bag of porcelain")
        text = self.t.execute("rollover_proposal", {})
        self.assertEqual(on_plan_count(text), 1, text)
        self.t.execute("remove_event", {"event": self.on_today("Renew the kiln permit")[0].id})
        text = self.a.leftover_notice()
        self.assertEqual(on_plan_count(text), 0, text)
        self.assertNotIn("Renew the kiln permit, today at", text)

    def test_every_line_agrees_with_the_count(self):
        self.task("t_permit").due = DAY.isoformat()
        self.unmarked_yesterday("Renew the kiln permit", "Order a bag of porcelain", "Mix a bucket of slip")
        self.t.execute("add_event", {"title": "Mix a bucket of slip", "start": "4:40 PM", "duration": "30 min",
                                     "task_id": "t_slip", "category": "task"})
        text = self.t.execute("rollover_proposal", {})
        timed = [line for line in text.splitlines() if line.startswith("Keep:") and ", today at " in line]
        self.assertEqual(on_plan_count(text), len(timed), text)
        self.assertEqual(leftover_facts.wrong(self.a, text), [])

    def test_the_models_list_says_where_each_one_is_now(self):
        self.unmarked_yesterday("Sand the spare paddle", "Order a bag of porcelain")
        self.t.execute("add_event", {"title": "Sand the spare paddle", "start": "3:20 PM", "duration": "30 min",
                                     "task_id": "t_paddle", "category": "task"})
        self.t.execute("rollover_proposal", {})
        listing = self.a.leftover_brief()
        paddle = next(line for line in listing.splitlines() if "Sand the spare paddle" in line and line.startswith("- "))
        self.assertIn("today at", paddle)
        self.assertIn("1 of them on today's plan", listing)


class TestProposeLeftoversAnswersFromThePlan(MorningCase):

    def test_the_result_counts_what_is_on_the_plan(self):
        self.task("t_permit").due = DAY.isoformat()
        self.unmarked_yesterday("Renew the kiln permit", "Order a bag of porcelain")
        self.t.execute("rollover_proposal", {})
        self.t.assistant.ask_json = lambda system, user: {"answer": "STILL_NEEDED", "reason": "scripted"}
        self.t.user_message = "up, the canoe club meets at 7:15 tonight"
        try:
            out = self.t.execute("propose_leftovers", {
                "keep": [{"item": "Renew the kiln permit", "when": "today", "kind": "task"},
                         {"item": "Order a bag of porcelain", "when": "today", "kind": "task"}]})
        finally:
            self.t.user_message = None
        # the porcelain is unknown and the user didn't name it: it isn't put on today, so it isn't counted there
        self.assertIn("kept 2 from before; 1 of them on today's plan", out)
        self.assertIn("Order a bag of porcelain – ", out)
        self.assertNotIn("Order a bag of porcelain – tomorrow", out)  # no day named: a later day, never tomorrow by itself
        self.assertIn("Renew the kiln permit – today at", out)
        self.assertEqual(on_plan_count(self.a.leftover_notice()), 1)


class TestTheModelsMorningIsChecked(AsPlainListItems, unittest.TestCase):
    """A whole morning through the app's service with a mock model: the first message of the day builds it."""

    def setUp(self):
        super().setUp()  # as plain list items (the morning may be sorted while the service starts)
        set_debug_time(at("09:45"))
        self.addCleanup(set_debug_time, None)
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        self.tasks = [studio_item(tid, title) for tid, title in STUDIO]
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = self.tasks, []
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda *a: None, scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.m = self.svc.manager()
        for i, tid in enumerate(("t_paddle", "t_clay")):
            title = next(t for i_, t in STUDIO if i_ == tid)
            self.m.tools.execute("add_event", {"title": title, "start": f"{2 + i}:20 PM", "duration": "30 minutes",
                                               "day": (DAY - timedelta(days=1)).isoformat(), "task_id": tid,
                                               "category": "task"})
        # put on today's plan before the morning: kept for the week, but it's on today
        self.m.tools.execute("add_event", {"title": "Sand the spare paddle", "start": "3:20 PM",
                                           "duration": "30 minutes", "task_id": "t_paddle", "category": "task"})

    def checks_sent(self):
        return [str(m.get("content") or "") for call in self.engine.call_history for m in call
                if m.get("role") == "user" and "doesn't match the plan as it stands" in str(m.get("content") or "")]

    def test_the_facts_given_agree_with_the_plan(self):
        self.engine.set_next_response(reply("☀️ Good morning! The evening is open for the canoe."))
        self.m.send_user_message("awake, clay drying on the rack")
        instruction = next(m["content"] for m in self.engine.call_history[0]
                           if m["role"] == "user" and "first message of the day" in str(m["content"]))
        self.assertIn("Kept 2 from before (1 on today's plan)", instruction)
        self.assertIn("Sand the spare paddle, today at", instruction)
        self.assertNotIn("(0 on today's plan)", instruction)

    def test_a_wrong_count_is_sent_back_and_the_rewrite_stands(self):
        self.engine.set_next_response(reply("☀️ Good morning! Kept 2 from before (0 on today's plan), all in "
                                            "Before today."))
        self.engine.set_next_response(reply("☀️ Good morning! Kept 2 from before, 1 on today's plan: the paddle "
                                            "sanding. The rest is in Before today."))
        out = self.m.send_user_message("awake, clay drying on the rack")
        self.assertEqual(len(self.checks_sent()), 1)
        self.assertIn("1 on today's plan", out)
        self.assertNotIn("0 on today's plan", out)

    def test_a_kept_item_called_on_the_plan_that_isnt(self):
        self.engine.set_next_response(reply("☀️ Good morning! Order a bag of porcelain is on today's plan."))
        self.engine.set_next_response(reply("☀️ Good morning! Order a bag of porcelain waits for tomorrow."))
        out = self.m.send_user_message("awake, clay drying on the rack")
        self.assertEqual(len(self.checks_sent()), 1)
        self.assertIn("isn't on today's plan", self.checks_sent()[0])
        self.assertIn("waits for tomorrow", out)

    def test_still_wrong_after_the_rewrite_python_says_it(self):
        wrong = "☀️ Good morning! I kept 5 from before, all on today's plan. The canoe waits for the evening."
        self.engine.set_next_response(reply(wrong))
        self.engine.set_next_response(reply(wrong))
        out = self.m.send_user_message("awake, clay drying on the rack")
        self.assertNotIn("5 from before", out)
        self.assertIn("Kept 2 from before (1 on today's plan)", out)
        self.assertIn("The canoe waits for the evening.", out)

    def test_a_right_morning_goes_out_as_written(self):
        right = ("☀️ Good morning! Kept 2 from before, 1 on today's plan (sanding the spare paddle); the porcelain "
                 "order waits for tomorrow. It's all in Before today.")
        self.engine.set_next_response(reply(right))
        out = self.m.send_user_message("awake, clay drying on the rack")
        self.assertEqual(self.checks_sent(), [])
        self.assertIn("1 on today's plan", out)


class TestTheClaimChecker(AsPlainListItems, MorningCase):
    """The checker's reading of counts and places, on its own."""

    def setUp(self):
        super().setUp()
        self.unmarked_yesterday("Sand the spare paddle", "Order a bag of porcelain")
        self.t.execute("add_event", {"title": "Sand the spare paddle", "start": "3:20 PM", "duration": "30 min",
                                     "task_id": "t_paddle", "category": "task"})
        self.t.execute("rollover_proposal", {})

    def test_counts(self):
        self.assertEqual(leftover_facts.wrong(self.a, "Kept two from before, one on today's plan."), [])
        self.assertTrue(leftover_facts.wrong(self.a, "Kept 3 from before, one on today's plan."))
        self.assertTrue(leftover_facts.wrong(self.a, "Kept 2 from before, none on today's plan."))
        self.assertTrue(leftover_facts.wrong(self.a, "Kept both from before and both are on today's plan."))

    def test_a_count_about_something_else_isnt_read_as_the_leftovers(self):
        self.assertEqual(leftover_facts.wrong(self.a, "3 things on today's plan for the free time."), [])

    def test_places(self):
        self.assertTrue(leftover_facts.wrong(self.a, "Keep: Sand the spare paddle, tomorrow."))
        self.assertTrue(leftover_facts.wrong(self.a, "Keep: Order a bag of porcelain, today at 2:30 PM."))
        self.assertEqual(leftover_facts.wrong(self.a, "Keep: Order a bag of porcelain, tomorrow."), [])


if __name__ == "__main__":
    unittest.main()
