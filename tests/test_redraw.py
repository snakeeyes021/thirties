"""The plan changes only when asked: redrawing the rest of the day, with the facts about items kept."""

import unittest

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.clock import format_clock
from thirties_core.plan_tools import NOT_DONE, PlanTools


class TestRedraw(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("17:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([event("dinner-out", "19:00", "20:00", "Dinner with Sam")])
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched)
        for title, start, minutes in (("Shower", "4:00 PM", 30), ("Walk", "5:30 PM", 60), ("Glass", "6:30 PM", 30),
                                      ("Snack", "8:00 PM", 30), ("Litter", "8:30 PM", 30)):
            self.t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min"})

    def times(self):
        return {s.title: (format_clock(s.start_dt), format_clock(s.end_dt)) for s in self.t.day_plan.spans}

    def test_not_reported_is_not_assumed_done(self):
        self.assertIn("Not reported yet (not assumed done): Shower", self.t.list_schedule())

    def test_redraw_keeps_order_facts_and_fixed_things(self):
        self.t.execute("set_item_facts", {"event": "glass", "after": "Shower"})
        out = self.t.execute("set_item_facts", {"event": "walk", "after": "Shower", "daylight": True})
        self.assertEqual(out, "Noted for Walk: after Shower, in daylight.")
        self.t.execute("set_item_facts", {"event": "snack", "stays_put": True})
        out = self.t.execute("redraw", {})
        times = self.times()
        self.assertTrue(at("19:00") > self.t.day_plan.sunset)       # (the walk can't fit in daylight after the shower)
        self.assertEqual(times["Shower"], ("5:30 PM", "6:00 PM"))   # behind: the next free thirty from now
        self.assertNotIn("Walk", times)                              # after the shower is after sunset: off the plan
        self.assertIn("Doesn't fit before bedtime, so it's off today's plan: Walk. Plan it tomorrow, or let it go?", out)
        self.assertEqual(times["Glass"], ("6:30 PM", "7:00 PM"))    # never earlier than planned; before the dinner
        self.assertEqual(times["Dinner with Sam"], ("7:00 PM", "8:00 PM"))
        self.assertEqual(times["Snack"], ("8:00 PM", "8:30 PM"))    # stays put
        self.assertEqual(times["Litter"], ("8:30 PM", "9:00 PM"))
        self.assertIn("Not reported, so kept as still to do: Shower.", out)
        out = self.t.execute("undo_redraw", {})
        self.assertTrue(out.startswith("Put back as it was before the redraw"))
        self.assertEqual(self.times()["Shower"], ("4:00 PM", "4:30 PM"))
        self.assertEqual(self.times()["Walk"], ("5:30 PM", "6:30 PM"))  # back, too
        self.assertTrue(self.t.execute("undo_redraw", {}).startswith(NOT_DONE))

    def test_short_things_share_a_thirty_and_order_holds(self):
        self.t.execute("add_event", {"title": "Feed cats", "start": "4:30 PM", "duration": "5 min"})
        self.t.execute("add_event", {"title": "Message Brad", "start": "4:35 PM", "duration": "15 min"})
        self.t.execute("redraw", {})
        times = self.times()
        self.assertEqual(times["Shower"], ("5:30 PM", "6:00 PM"))
        self.assertEqual(times["Feed cats"][0], "6:00 PM")
        self.assertEqual(times["Message Brad"][0], "6:05 PM")  # shares the 6:00 thirty, in order

    def test_after_holds_even_when_the_other_was_planned_later(self):
        self.t.execute("add_event", {"title": "Bike", "start": "9:00 PM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Helmet", "start": "9:30 PM", "duration": "30 min"})
        self.t.execute("set_item_facts", {"event": "bike", "after": "Helmet"})
        self.t.execute("redraw", {})
        times = {s.title: s.start_dt for s in self.t.day_plan.spans}
        self.assertGreater(times["Bike"], times["Helmet"])

    def test_things_happening_alongside_move_together(self):
        self.t.execute("add_event", {"title": "Audiobook", "start": "4:00 PM", "duration": "30 min", "alongside": True})
        self.t.execute("redraw", {})
        times = self.times()
        self.assertEqual(times["Audiobook"], times["Shower"])

    def test_facts_need_real_items(self):
        self.assertTrue(self.t.execute("set_item_facts", {"event": "walk", "after": "Walk"}).startswith(NOT_DONE))
        self.assertTrue(self.t.execute("set_item_facts", {"event": "walk", "after": "Nap"}).startswith(NOT_DONE))


class TestSlmRedraw(unittest.TestCase):

    def test_replan_words_redraw_without_asking_gemma(self):
        from thirties_core.brains.slm import SlmBrain
        set_debug_time(at("17:10"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        tools = PlanTools(plan, pending, sched)
        tools.execute("add_event", {"title": "Shower", "start": "4:00 PM", "duration": "30 min"})
        brain = SlmBrain(tools)
        reply = brain.respond("ugh, still not showered. my god, just figure it out")
        self.assertTrue(reply.startswith("Redrew the rest of today:\n- 5:30 PM – Shower"), reply)
        self.assertTrue(brain.respond("undo the replan").startswith("Put back as it was"))
        from thirties_core.brains.slm.brain import NOT_RE, REDRAW_RE
        for text in ("please don't replan, I'm fine", "no need to push everything back", "I can't figure it out"):
            self.assertFalse(REDRAW_RE.search(text) and not NOT_RE.search(text), text)


if __name__ == "__main__":
    unittest.main()
