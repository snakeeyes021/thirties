"""What the day pages offer with a tap (thirties_core/day_actions.py): work started or finished,
and the actions on a later day's pencilled-in items, with the guard on their one Joplin write."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.day_actions import (PENCIL_DONE, PENCIL_DROP, PENCIL_NEXT_DAY, PENCIL_SCHEDULE, WORK_END,
                                       WORK_START, first_free_start, pencil_act, work_call,
                                       work_line, work_step)
from thirties_core.day_lists import AheadLists, pencilled_in
from thirties_core.models import SpanCategory, SpanStatus
from thirties_core.plan_tools import PlanTools
from thirties_core.timeline_layout import day_layout


class TestWorkButton(unittest.TestCase):
    """DAY: default Work 8:30 AM – 4:00 PM."""

    def setUp(self):
        set_debug_time(at("09:40"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched)

    def test_started_then_finished_then_nothing(self):
        plan = self.t.day_plan
        self.assertIsNone(work_step(plan, at("07:00")))  # too early to have started
        self.assertEqual(work_step(plan, at("07:40"))[0], WORK_START)  # from an hour before
        self.assertEqual(work_step(plan, at("09:40"))[0], WORK_START)
        self.assertIsNone(work_step(plan, None))  # not today
        call = work_call(WORK_START, at("09:40"))
        self.assertEqual(call, ("log_day_times", {"work_started": "9:40 AM"}))
        out = self.t.execute(*call)  # the same tool the assistant uses for "started work at 9:40"
        self.assertEqual(out, "Recorded: started work 9:40 AM.")
        work = next(s for s in self.t.day_plan.spans if s.category == SpanCategory.WORK)
        self.assertEqual(work.actual_start, at("09:40"))
        step = work_step(self.t.day_plan, at("15:50"))
        self.assertEqual(step[0], WORK_END)
        self.assertEqual(work_line(step[1]), "Started 9:40 AM")
        set_debug_time(at("16:35"))
        out = self.t.execute(*work_call(WORK_END, at("16:35")))
        self.assertTrue(out.startswith("Recorded: finished work 4:35 PM. After work: "), out)  # and what's left
        self.assertIsNone(work_step(self.t.day_plan, at("16:40")))  # both said: no button

    def test_finished_work_even_when_the_start_was_never_said(self):
        self.assertEqual(work_step(self.t.day_plan, at("16:20"))[0], WORK_END)  # Work was until 4 PM
        self.assertIsNone(work_step(self.t.day_plan, at("19:30")))  # long after: nothing to ask

    def test_no_work_no_button(self):
        work = next(s for s in self.t.day_plan.spans if s.category == SpanCategory.WORK)
        work.status = SpanStatus.SKIPPED
        self.assertIsNone(work_step(self.t.day_plan, at("09:40")))

    def test_a_guessed_start_still_asks(self):
        work = next(s for s in self.t.day_plan.spans if s.category == SpanCategory.WORK)
        work.actual_start, work.time_source = at("08:30"), "estimated"
        self.assertEqual(work_step(self.t.day_plan, at("09:40"))[0], WORK_START)


class TestEnergy(unittest.TestCase):

    def test_it_is_stored_where_the_statistics_read_it(self):
        set_debug_time(at("14:00"))
        self.addCleanup(set_debug_time, None)
        from thirties_core.assistant_tools import Services
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        services = Services.for_db(sched.state_db, [], None)
        t = PlanTools(plan, pending, sched, services=services)
        self.assertEqual(t.execute("log_energy", {"level": 4}), "Noted energy 4/5.")
        self.assertEqual([e["level"] for e in services.energy.entries()], [4])


class TestPencilledIn(ListsTestCase):
    """A later day's list, no time yet: done early, schedule it, the day after, or drop it."""

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Buy stamps", "Water plants"], "day": "tomorrow"})
        self.tomorrow = DAY + timedelta(days=1)
        plan, pending = self.sched.build_day_plan(self.tomorrow)
        self.tt = PlanTools(plan, pending, self.sched, services=self.services)

    def listed(self, day=None):
        return [r["title"] for r in AheadLists(self.sched.state_db).for_day(day or self.tomorrow)]

    def test_schedule_it_at_the_days_first_free_thirty(self):
        layout = day_layout(self.tt.day_plan, at("14:00"))
        start = first_free_start(layout)
        self.assertEqual(start.minute % 30, 0)  # a block time, never odd minutes
        out = pencil_act(self.tt.assistant, self.tomorrow, "buy stamps", PENCIL_SCHEDULE, layout)
        self.assertTrue(out.startswith("Buy stamps is on Friday's plan at "), out)
        span = next(s for s in self.tt.day_plan.spans if s.title == "Buy stamps")
        self.assertEqual(span.start_dt, start)
        stamps = next(t for t in self.services.tasks if t.title == "Buy stamps")
        self.assertEqual(span.task_id, stamps.id)  # linked to its home item
        self.assertEqual(self.listed(), ["Water plants"])  # no longer pencilled in
        self.assertEqual(pencilled_in(self.tt.assistant), ["Water plants"])

    def test_the_day_after_or_drop(self):
        self.assertEqual(pencil_act(self.tt.assistant, self.tomorrow, "Water plants", PENCIL_NEXT_DAY),
                         "Water plants is on Saturday's list instead.")
        self.assertEqual(self.listed(), ["Buy stamps"])
        self.assertEqual(self.listed(self.tomorrow + timedelta(days=1)), ["Water plants"])
        self.assertEqual(pencil_act(self.tt.assistant, self.tomorrow, "Buy stamps", PENCIL_DROP),
                         "Buy stamps is off Friday's list.")
        self.assertEqual(self.listed(), [])
        self.assertIn("Call the bank\n- [ ] Buy stamps", self.body("n_errands"))  # its own list untouched

    def test_done_early_ticks_its_home_item_by_its_exact_link(self):
        before = self.body("n_errands")
        self.assertEqual(pencil_act(self.tt.assistant, self.tomorrow, "Buy stamps", PENCIL_DONE), "Done: Buy stamps.")
        self.assertEqual(self.body("n_errands"), before.replace("- [ ] Buy stamps", "- [x] Buy stamps"))
        self.assertEqual(self.listed(), ["Water plants"])
        self.assertEqual(self.body("n_today"), "")  # never today's note

    def test_done_early_never_writes_what_has_no_home_or_is_a_routine(self):
        notes = {nid: self.body(nid) for nid in self.api.notes}
        self.assertEqual(pencil_act(self.tt.assistant, self.tomorrow, "Water plants", PENCIL_DONE),
                         "Done: Water plants.")  # nothing in the lists by that name: Thirties' own record only
        AheadLists(self.sched.state_db).add(self.tomorrow, "Feed the fish",
                                            next(t for t in self.services.tasks if t.title == "Feed the fish"))
        pencil_act(self.tt.assistant, self.tomorrow, "Feed the fish", PENCIL_DONE)
        self.assertEqual({nid: self.body(nid) for nid in self.api.notes}, notes)  # a routine is never written

    def test_something_no_longer_listed_is_said_so(self):
        out = pencil_act(self.tt.assistant, self.tomorrow, "Sell the boat", PENCIL_DROP)
        self.assertEqual(out, "Sell the boat isn't on Friday's list.")


if __name__ == "__main__":
    unittest.main()


class TestEffort(unittest.TestCase):
    """The user's own estimate per item, in the unit they pick: the calls into estimate tools."""

    def test_the_estimate_in_their_words_and_unit(self):
        from thirties_core.day_actions import estimate_text
        self.assertEqual(estimate_text(3, "thirties"), "3 thirties")
        self.assertEqual(estimate_text(1.5, "hours"), "1.5 hours")
        self.assertEqual(estimate_text(5, "points"), "5 points")
        self.assertEqual(estimate_text(2, "fortnights"), "2 hours")  # never a unit the lists can't read

    def test_a_new_unit_becomes_the_usual_one_first(self):
        from thirties_core.day_actions import estimate_calls
        self.assertEqual(estimate_calls("t_shed", 3, "hours", "hours"),
                         [("set_estimate", {"task": "t_shed", "estimate": "3 hours"})])
        self.assertEqual(estimate_calls("t_shed", 5, "points", "hours"),
                         [("set_effort_unit", {"unit": "points"}),
                          ("set_estimate", {"task": "t_shed", "estimate": "5 points"})])

    def test_what_the_editor_shows(self):
        from thirties_core.day_actions import shown_estimate
        self.assertEqual(shown_estimate({"said": "about 3 hours", "amount": 3, "unit": "hours", "minutes": 180},
                                        "points"), (3.0, "hours"))
        self.assertEqual(shown_estimate(None, "points"), (0.0, "points"))  # none yet: their usual unit
        self.assertEqual(shown_estimate(None, ""), (0.0, "hours"))
