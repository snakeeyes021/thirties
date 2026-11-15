"""Energy and the time the user has choose what's offered, the thirties left when work ends and a few
things that fit, and a rolling week pencilled in that firms up on the day. Invented items throughout."""

import re
import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_make_way import tools_for
from tests.test_routines import item
from thirties_core.astronomy import set_debug_time
from thirties_core.energy import level_from
from thirties_core.item_facts import Lasting
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools


class Case(unittest.TestCase):
    TASKS = (("t_letter", "Post the letter"), ("t_attic", "Clear the attic"), ("t_vet", "Call the vet"),
             ("t_bills", "Pay the bills"), ("t_lawn", "Mow the lawn"))

    def setUp(self):
        self.addCleanup(set_debug_time, None)
        self.t = self.tools(now="09:00")

    def tools(self, now, work=False):
        t = tools_for(now=now, work=work)
        catalog = ListCatalog(t.scheduler.state_db)
        t.assistant.s.tasks += [item(tid, title) for tid, title in self.TASKS]
        for tid, _ in self.TASKS:
            catalog.set_urgency(tid, "soon")
        t.assistant.s.tasks = catalog.apply(t.assistant.s.tasks)
        t._lasting().set(Lasting(name="clear the attic", title="Clear the attic", minutes=120))
        t._lasting().set(Lasting(name="mow the lawn", title="Mow the lawn", minutes=60))
        return t


class EnergyChoosesWhatsOffered(Case):
    """The user gives the time they have and how they feel, and asks what to do: Python reads both."""

    def test_words_and_numbers(self):
        for said, level in (("exhausted", 1), ("kinda tired", 2), ("fine", 3), ("motivated", 4),
                            ("really focused today", 5), ("4/5", 4), (2, 2), ("purple", None)):
            self.assertEqual(level_from(said), level, said)

    def test_high_energy_and_three_hours(self):
        out = self.t.execute("day_options", {"energy": "really focused today", "hours": "3 hours"})
        self.assertIn("The time they have: until 12:00 PM.", out)
        self.assertIn("Energy is high today (5/5): a good time for the bigger things", out)
        listed = [line for line in out.splitlines() if line.startswith("- ") and "task_id" in line]
        self.assertTrue(listed[0].startswith("- Clear the attic"), listed)  # the bigger things first
        self.assertIn("bigger", listed[0])
        self.assertNotIn("PM – 11:00 PM", out)  # only the time they have
        self.assertEqual(self.t.assistant.s.energy.entries()[-1]["level"], 5)  # noted in the energy log

    def test_low_energy_puts_light_things_first(self):
        out = self.t.execute("day_options", {"energy": "drained"})
        listed = [line for line in out.splitlines() if line.startswith("- ") and "task_id" in line]
        sizes = ["light" if "; light)" in line else "bigger" for line in listed]
        self.assertEqual(sizes, sorted(sizes, key=lambda x: x != "light"), listed)  # every light one before the big

    def test_words_that_dont_say(self):
        self.assertTrue(self.t.execute("day_options", {"energy": "purple"}).startswith(NOT_DONE))


class WhenWorkEnds(Case):
    """"You have two daylight thirties and 8 dark thirties; here's like... 3 tasks"."""

    def test_the_thirties_left_and_a_few_that_fit(self):
        t = self.tools(now="12:00", work=True)
        set_debug_time(at("16:05"))
        t.user_message = "done with work"
        out = t.execute("log_day_times", {"work_ended": "4:00 PM"})
        m = re.search(r"After work: (\d+) daylight thirties and (\d+) dark thirties free before bed\. A few that "
                      r"fit:\n(.+)", out, re.S)
        self.assertIsNotNone(m, out)
        light, dark = int(m.group(1)), int(m.group(2))
        self.assertEqual(light, 4)  # 4:30 (half an hour clear after work) to sunset at 6:30
        self.assertEqual(light + dark, 13)  # until bed at 11:00
        offered = m.group(3).splitlines()
        self.assertEqual(len(offered), 3)
        self.assertFalse(any(s.title in m.group(3) for s in t.day_plan.spans if s.status == SpanStatus.PLANNED
                             and s.title != "Sleep"))
        self.assertFalse(any(s.title == "Post the letter" for s in t.day_plan.spans))  # offered, not placed
        t.user_message = "actually I finished at 4:10"
        self.assertNotIn("After work", t.execute("log_day_times", {"work_ended": "4:10 PM"}))  # once a day

    def test_outdoors_only_with_daylight_to_do_it_in(self):
        t = self.tools(now="12:00", work=True)
        set_debug_time(at("18:40"))
        t.user_message = "finished work at 6:30"
        out = t.execute("log_day_times", {"work_ended": "6:30 PM"})
        self.assertIn("After work: no daylight left, and", out)
        self.assertNotIn("Mow the lawn", out)


class RollingWeek(Case):
    """"Always have like... a week's worth of thirties planned out, but things would only fully solidify on
    the day"."""

    def test_the_days_ahead_are_pencilled_in_and_firm_up_on_the_day(self):
        from thirties_core.week_draft import draft_for
        out = self.t.execute("week_ahead", {})
        self.assertTrue(out.startswith("Pencilled in (not on the plan; each day firms up on its morning):"), out)
        self.assertIn("Friday Oct 16: ", out)
        self.assertIn("daylight and", out)
        db = self.t.scheduler.state_db
        drafted = {d: draft_for(db, DAY + timedelta(days=d)) for d in range(1, 7)}
        titles = [i["title"] for items in drafted.values() for i in items]
        self.assertEqual(len(titles), len(set(titles)))  # each item on one day only
        self.assertTrue(drafted[1])
        tomorrow = self.t.plan_for("tomorrow")
        self.assertFalse(any(s.title in titles for s in tomorrow.spans))  # pencilled in: not on any plan
        first = drafted[1][0]
        # the next morning: that day's own proposal firms it up at its pencilled time
        set_debug_time(at("09:00", 1))
        plan, pending = self.t.scheduler.build_day_plan(DAY + timedelta(days=1))
        nxt = PlanTools(plan, pending, self.t.scheduler, services=self.t.services)
        cands = nxt.assistant.proposals.candidates()
        self.assertEqual(cands[0]["title"], first["title"])
        self.assertIn("pencilled in for", cands[0]["why"])
        items = nxt.assistant.proposals.default_items()
        firmed = next(i for i in items if i["title"] == first["title"])
        self.assertEqual(firmed["start"], first["start"])

    def test_the_draft_is_shared_between_devices(self):
        from tests.fixtures import make_scheduler
        from thirties_core.week_draft import draft_for
        self.t.execute("week_ahead", {})
        other = make_scheduler()
        for record in self.t.scheduler.state_db.get_shared_setting_records():
            other.state_db.merge_shared_setting(record)
        self.assertEqual(draft_for(other.state_db, DAY + timedelta(days=1)),
                         draft_for(self.t.scheduler.state_db, DAY + timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()
