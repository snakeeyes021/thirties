"""Where a missed item goes next (replan.py): the plan's own examples, with invented items."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, build_day
from thirties_core.astronomy import set_debug_time
from thirties_core.kinds import DAILY, ONCE, ONGOING
from thirties_core.plan_tools import PlanTools
from thirties_core.replan import place_once, replan, spread_ongoing


class ReplanTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("21:00"))
        self.addCleanup(set_debug_time, None)
        self.sched, plan, events = build_day()
        self.t = PlanTools(plan, events, self.sched)

    def fill(self, offset):
        day = (DAY + timedelta(days=offset)).isoformat()
        plan = self.t.plan_for(day)
        for s in [s for s in plan.spans if s.title == "Work"]:  # a day off, then filled to the brim
            self.t.execute("remove_event", {"event": s.id, "day": day})
        self.t.execute("add_event", {"title": f"Busy {offset}", "start": "7:00 AM", "end": "11:30 PM", "day": day,
                                     "fixed": True})

    def test_a_one_off_goes_to_tomorrow(self):
        p = place_once(self.t, "Mail the warranty card", 30, DAY)
        self.assertEqual((p.day, p.why), (DAY + timedelta(days=1), "tomorrow"))

    def test_with_the_next_two_days_full_it_goes_to_the_day_after_those(self):
        self.fill(1)
        self.fill(2)
        p = place_once(self.t, "Read for the pottery class", 60, DAY, due=DAY + timedelta(days=21))
        self.assertEqual(p.day, DAY + timedelta(days=3))
        self.assertIn("the days before are full", p.why)

    def test_never_past_its_deadline(self):
        self.fill(1)
        self.fill(2)
        p = place_once(self.t, "Send the form", 30, DAY, due=DAY + timedelta(days=2))
        self.assertIsNone(p.day)
        self.assertIn("before it's due", p.why)

    def test_missed_time_on_something_ongoing_goes_over_its_next_sessions(self):
        for offset in (1, 2):
            day = (DAY + timedelta(days=offset)).isoformat()
            self.t.execute("add_event", {"title": "Garden app dev", "start": "6:00 PM", "duration": "1 hour",
                                         "day": day})
        p = spread_ongoing(self.t, "Garden app dev", 60, DAY)
        self.assertEqual(len(p.extended), 2, p.why)
        for offset in (1, 2):
            plan = self.t.plan_for((DAY + timedelta(days=offset)).isoformat())
            dev = next(s for s in plan.spans if s.title == "Garden app dev")
            self.assertEqual(dev.end_dt - dev.start_dt, timedelta(minutes=90))  # an extra half hour each

    def test_spread_never_goes_past_the_deadline(self):
        self.t.execute("add_event", {"title": "Garden app dev", "start": "6:00 PM", "duration": "1 hour",
                                     "day": (DAY + timedelta(days=3)).isoformat()})
        self.fill(1)
        p = spread_ongoing(self.t, "Garden app dev", 60, DAY, until=DAY + timedelta(days=2))
        self.assertEqual(p.extended, [])  # its session after the deadline doesn't count
        self.assertEqual(p.day, DAY + timedelta(days=2))  # a session on the last day with room

    def test_a_daily_thing_simply_ends(self):
        class A:
            pt = self.t
        self.assertIsNone(replan(A, "Lunch", DAILY, 30, DAY).day)
        self.assertEqual(replan(A, "Mail the warranty card", ONCE, 30, DAY).day, DAY + timedelta(days=1))
        self.assertIsNotNone(replan(A, "Garden app dev", ONGOING, 60, DAY))


if __name__ == "__main__":
    unittest.main()


from tests.test_today import TodayTestCase  # noqa: E402


class SettleTest(TodayTestCase):
    """The morning's sorting of yesterday gives each missed item its next day."""

    def miss(self, title, start="2:00 PM", minutes=30, **extra):
        y = (DAY - timedelta(days=1)).isoformat()
        self.t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min", "day": y, **extra})

    def test_a_missed_one_off_waits_for_the_first_day_with_room(self):
        from thirties_core.carry import OpenPool, settle
        self.miss("Mail the warranty card")
        out = settle(self.t.assistant, DAY)
        rec = OpenPool(self.sched.state_db).all()["mail the warranty card"]
        tomorrow = (DAY + timedelta(days=1)).isoformat()  # not urgent: never today (rule 3), the first day with room
        self.assertEqual((rec["start"], rec["end"]), (tomorrow, tomorrow), out)

    def test_as_soon_as_possible_goes_on_today_if_it_fits(self):
        from thirties_core.carry import OpenPool
        self.miss("Mail the warranty card ASAP")
        said = self.t.execute("rollover_proposal", {})
        rec = OpenPool(self.sched.state_db).all()["mail the warranty card asap"]
        self.assertEqual(rec["start"], DAY.isoformat(), said)
        self.assertIn("Keep: Mail the warranty card ASAP, today.", said)

    def test_the_morning_names_each_placement(self):
        self.miss("Return the library books")
        said = self.t.execute("rollover_proposal", {})
        self.assertIn("Keep: Return the library books, tomorrow.", said)  # applied, and said so it can be corrected

    def test_a_week_with_no_room_leaves_it_at_its_horizon(self):
        from thirties_core.carry import OpenPool, settle
        self.miss("Return the library books")
        for offset in range(1, 4):  # through the end of the week (Thursday's week ends Sunday)
            day = (DAY + timedelta(days=offset)).isoformat()
            for s in [s for s in self.t.plan_for(day).spans if s.title == "Work"]:
                self.t.execute("remove_event", {"event": s.id, "day": day})
            self.t.execute("add_event", {"title": f"Busy {offset}", "start": "7:00 AM", "end": "11:30 PM",
                                         "day": day, "fixed": True})
        settle(self.t.assistant, DAY)
        rec = OpenPool(self.sched.state_db).all()["return the library books"]
        self.assertFalse(rec.get("start"), rec)  # not pushed into next week: it waits at this week's horizon

    def test_missed_ongoing_time_is_spread_forward(self):
        from thirties_core.carry import settle
        from thirties_core.models import TaskItem
        self.services.tasks.append(TaskItem(id="t_garden", source_notebook="1. Tasks", title="Garden app dev",
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands",
                                            due=(DAY + timedelta(days=14)).isoformat()))  # due in two weeks
        self.t.execute("set_item_facts", {"event": "Garden app dev", "kind": "ongoing"})
        self.miss("Garden app dev", start="3:00 PM", minutes=60)
        for offset in (0, 1):
            self.t.execute("add_event", {"title": "Garden app dev", "start": "7:00 PM", "duration": "1 hour",
                                         "day": (DAY + timedelta(days=offset)).isoformat()})
        out = settle(self.t.assistant, DAY)
        self.assertIn("Garden app dev", [r["title"] for r in out.get("respread", [])], out)

    def test_missed_project_time_with_no_deadline_simply_ends(self):
        from thirties_core.carry import settle
        self.t.execute("set_item_facts", {"event": "Pottery practice", "kind": "ongoing"})
        self.miss("Pottery practice", start="3:00 PM", minutes=60)
        out = settle(self.t.assistant, DAY)
        self.assertIn("Pottery practice", out["ended"])
        self.assertEqual(out.get("respread"), [])  # there's another session anyway

    def test_the_morning_keeps_the_day_replanning_found(self):
        from thirties_core.carry import OpenPool
        self.miss("Mail the warranty card")  # first: the day's sorting runs once, at the first change to today
        busy_day = (DAY + timedelta(days=1)).isoformat()
        for s in [s for s in self.t.plan_for(busy_day).spans if s.title == "Work"]:
            self.t.execute("remove_event", {"event": s.id, "day": busy_day})
        self.t.execute("add_event", {"title": "All-day workshop", "start": "7:00 AM", "end": "11:30 PM",
                                     "day": busy_day, "fixed": True})
        self.sched.state_db.set_sync_meta(f"leftovers:{DAY.isoformat()}", "")  # sorted again, tomorrow full
        self.t.execute("rollover_proposal", {})
        rec = OpenPool(self.sched.state_db).all()["mail the warranty card"]
        self.assertEqual(rec["start"], (DAY + timedelta(days=2)).isoformat())  # the day after the full one
        self.assertEqual(rec["when"], (DAY + timedelta(days=2)).strftime("%A").lower())
        self.assertFalse(any(s.title == "Mail the warranty card" for s in self.t.day_plan.spans))
