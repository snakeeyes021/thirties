"""Leftovers land on the right day: a holiday on a calendar the user marked "holidays" never gets an item Python or
the model places by itself, the morning model sees the coming holidays, a leftover placed at "today" lands on
today's plan, and the morning hello lists what the user put on the day.

Fixtures are invented: someone who runs a small bike repair bench, with an invented festival calendar ("Festivals of
Corvia", whose "Lantern Day" is a holiday). The fixed day is a Thursday: tomorrow is Friday, then Saturday and Sunday.
"""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.calendar_engine import CalendarEvent
from thirties_core.carry import OpenPool, settle
from thirties_core.day_lists import AheadLists
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import PlanTools

YESTERDAY, TOMORROW, SATURDAY, SUNDAY = (DAY + timedelta(days=n) for n in (-1, 1, 2, 3))
BENCH = "- [ ] True the rear wheel\n- [ ] Replace the brake cables\n"
TITLES = {"n_today": "Today To-Do", "n_bench": "Bench jobs"}
FESTIVALS = "Festivals of Corvia"


def bench_item(tid, title, due=""):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=TITLES["n_bench"], parent_note_id="n_bench", due=due)


def lantern_day(offset):
    """Lantern Day, all day, ``offset`` days from the fixed Thursday, on the festival calendar."""
    return CalendarEvent(id=f"lantern-{offset}", summary="Lantern Day", start_dt=at("00:00", offset),
                         end_dt=at("00:00", offset + 1), calendar_id="uid-corvia", calendar_name=FESTIVALS,
                         is_primary=False)


class HolidayBase(unittest.TestCase):
    HOLIDAY = 2  # Saturday; a test class may move it

    def setUp(self):
        set_debug_time(at("09:35"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([lantern_day(self.HOLIDAY)])
        self.sched.config.calendar.modes = {FESTIVALS.lower(): "holidays"}
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n_bench": {"id": "n_bench", "body": BENCH}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES.get(nid, ""), "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_bench", "list")):
            catalog.set_role(nid, role, TITLES[nid])
        self.services = Services.for_db(self.sched.state_db, catalog.apply(
            [bench_item("t_wheel", "True the rear wheel"), bench_item("t_cables", "Replace the brake cables")]), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)
        self.a = self.t.assistant

    def say(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def missed_yesterday(self, title, task_id):
        """A bench job booked at a set time the day before, never marked; settle() runs over it."""
        self.t.execute("add_event", {"title": title, "start": "4:40 PM", "duration": "40 min", "task_id": task_id,
                                     "day": YESTERDAY.isoformat()})
        return settle(self.a, DAY)

    def pool(self, title):
        return OpenPool(self.sched.state_db).all()[title.lower()]


class HolidayIsTheDayItWouldGo(HolidayBase):
    """1: a holiday never receives what's carried, skipped or put off by Python or the model."""

    def test_a_skip_never_comes_back_on_a_holiday(self):
        self.say("put the wheel on at 10:20", "add_event",
                 {"title": "True the rear wheel", "start": "10:20 AM", "duration": "40 min", "task_id": "t_wheel"})
        span = self.planned("True the rear wheel")[0]
        out = self.say("not truing the wheel today after all", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("Sunday", out)  # Saturday, the first later day, is Lantern Day
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(SATURDAY)], [])
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(SUNDAY)], ["True the rear wheel"])
        self.assertEqual(self.pool("True the rear wheel")["start"], SUNDAY.isoformat())

    def test_a_day_the_user_names_stands_even_on_a_holiday(self):
        self.say("put the wheel on at 10:20", "add_event",
                 {"title": "True the rear wheel", "start": "10:20 AM", "duration": "40 min", "task_id": "t_wheel"})
        span = self.planned("True the rear wheel")[0]
        out = self.say("skip the wheel, do it Saturday", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("Saturday", out)
        self.assertEqual(self.pool("True the rear wheel")["start"], SATURDAY.isoformat())

    def test_the_models_own_pick_of_a_holiday_for_a_leftover_moves_to_the_next_day_with_room(self):
        self.missed_yesterday("Replace the brake cables", "t_cables")
        self.t.execute("rollover_proposal", {})
        self.t.execute("propose_leftovers", {"keep": [{"item": "Replace the brake cables", "when": "saturday"}]})
        self.assertEqual(self.pool("Replace the brake cables")["start"], SUNDAY.isoformat())


class HolidayTomorrow(HolidayBase):
    HOLIDAY = 1  # Friday

    def test_the_morning_carry_never_keeps_a_leftover_for_a_holiday(self):
        out = self.missed_yesterday("Replace the brake cables", "t_cables")
        self.assertIn("Replace the brake cables", out["pooled"])
        rec = self.pool("Replace the brake cables")
        self.assertEqual(rec["start"], SATURDAY.isoformat())  # Friday is Lantern Day: the first day after it
        self.assertEqual(rec["when"], "saturday")


class HolidayToday(HolidayBase):
    HOLIDAY = 0  # today

    def test_the_days_proposal_on_a_holiday_draws_nothing_from_the_pool_by_itself(self):
        pool = OpenPool(self.sched.state_db)
        pool.add([{"title": "True the rear wheel", "task_id": "t_wheel", "horizon": "week",
                   "start": DAY.isoformat(), "end": SUNDAY.isoformat(), "when": "this week"}], YESTERDAY)
        pool.add([{"title": "Replace the brake cables", "task_id": "t_cables", "horizon": "week"}], YESTERDAY)
        pool.place("Replace the brake cables", "today",
                   {"when": "today", "start": DAY.isoformat(), "end": DAY.isoformat()})  # the user's own word
        titles = [c["title"] for c in self.a.proposals.candidates(self.t.day_plan)]
        self.assertNotIn("True the rear wheel", titles)
        self.assertIn("Replace the brake cables", titles)

    def test_what_cant_wait_isnt_put_on_a_holiday_by_itself(self):
        self.missed_yesterday("Patch the inner tube asap", "")
        self.t.execute("rollover_proposal", {})
        self.assertEqual(self.planned("Patch the inner tube asap"), [])
        self.assertEqual(self.pool("Patch the inner tube asap")["start"], TOMORROW.isoformat())

    def test_the_user_can_still_put_a_leftover_on_the_holiday(self):
        self.missed_yesterday("Replace the brake cables", "t_cables")
        self.say("the cables today please", "place_leftover", {"item": "Replace the brake cables", "horizon": "today"})
        self.assertEqual(self.pool("Replace the brake cables")["start"], DAY.isoformat())


class TheMorningModelSeesTheHolidays(HolidayBase):
    """2: the leftovers context names the coming week's holidays, by name and day; the judgement is the model's."""

    def test_the_leftover_brief_names_the_coming_holidays(self):
        self.missed_yesterday("Replace the brake cables", "t_cables")
        self.t.execute("rollover_proposal", {})
        brief = self.a.leftover_brief()
        self.assertIn("Lantern Day", brief)
        self.assertIn("Saturday", brief[brief.index("Lantern Day") - 60:brief.index("Lantern Day") + 60])


if __name__ == "__main__":
    unittest.main()
