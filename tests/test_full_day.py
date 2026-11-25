"""Nothing on today is without a time outside If there's time (full_day.py): every item gets a time; a day really full
moves its lowest-priority item on to the next day with room that isn't a holiday, in one plain line; the Today note
is the truth (a line on it with no plan item gets its time, with one line saying so, and is never taken off for
want of one); a later day's ask waits in the open pool and gets its time when that day is built.

Fixtures are invented: the small bike repair bench of test_holiday_leftovers, with Lantern Day on Friday this time,
so the next day with room after the fixed Thursday is Saturday.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import DAY, at, line_with
from tests.test_holiday_leftovers import SATURDAY, SUNDAY, HolidayBase
from thirties_core import full_day, morning
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import OpenPool
from thirties_core.day_lists import AheadLists
from thirties_core.plan_tools import PlanTools


class Base(HolidayBase):
    HOLIDAY = 1  # Friday

    def on_today(self, message, *titles):
        out = self.say(message, "add_to_today", {"items": list(titles)})
        for t in titles:
            self.assertTrue(self.planned(t), (t, out))
        return out

    def full_until_gone(self, title):
        """The day full even doubled up, until ``title`` leaves today's plan: then its time is free."""
        spot = self.planned(title)[0].start_dt

        def first(pt, minutes, after=None):
            return None if self.planned(title) else spot
        return (patch("thirties_core.if_time.first_slot", side_effect=first),
                patch("thirties_core.if_time.doubled_slot", return_value=None))

    def full(self):
        return (patch("thirties_core.if_time.first_slot", return_value=None),
                patch("thirties_core.if_time.doubled_slot", return_value=None))


class TheLowestItemMovesOn(Base):

    def test_the_lowest_item_goes_to_the_next_day_with_room_past_a_holiday_and_the_asked_one_takes_its_time(self):
        self.on_today("sort the spoke bins today", "Sort the spoke bins")
        first, doubled = self.full_until_gone("Sort the spoke bins")
        with first, doubled:
            out = self.say("patch the demo tubes today", "add_to_today", {"items": ["Patch the demo tubes"]})
        self.assertIn("No room today: Sort the spoke bins goes to Saturday.", out)
        self.assertTrue(self.planned("Patch the demo tubes"))
        self.assertFalse(self.planned("Sort the spoke bins"))
        saturday = self.t.plan_for(SATURDAY)
        self.assertTrue(any(s.title == "Sort the spoke bins" for s in saturday.spans))  # a normal move: on that plan
        self.assertNotIn("Sort the spoke bins", self.api.notes["n_today"]["body"])
        self.assertNotIn("no time", out)

    def test_a_later_deadline_stays_and_one_with_none_goes(self):
        cables = next(t for t in self.a.s.tasks if t.id == "t_cables")
        cables.due = SUNDAY.isoformat()
        self.on_today("cables and spoke bins today", "Replace the brake cables", "Sort the spoke bins")
        first, doubled = self.full_until_gone("Sort the spoke bins")
        with first, doubled:
            out = self.say("patch the demo tubes today", "add_to_today", {"items": ["Patch the demo tubes"]})
        line_with(out, "No room today", "Sort the spoke bins", "Saturday")
        self.assertTrue(self.planned("Replace the brake cables"))

    def test_one_named_in_the_same_message_is_never_the_one_moved(self):
        self.on_today("cables and spoke bins today", "Replace the brake cables", "Sort the spoke bins")
        first, doubled = self.full_until_gone("Replace the brake cables")
        with first, doubled:
            out = self.say("the spoke bins and the demo tubes both today", "add_to_today",
                           {"items": ["Sort the spoke bins", "Patch the demo tubes"]})
        line_with(out, "No room today", "Replace the brake cables", "Saturday")
        self.assertTrue(self.planned("Sort the spoke bins"))

    def test_due_today_never_moves_so_with_nothing_else_the_new_item_gets_the_next_day(self):
        cables = next(t for t in self.a.s.tasks if t.id == "t_cables")
        cables.due = DAY.isoformat()
        self.on_today("cables today", "Replace the brake cables")
        first, doubled = self.full()
        with first, doubled:
            out = self.say("patch the demo tubes today", "add_to_today", {"items": ["Patch the demo tubes"]})
        self.assertTrue(self.planned("Replace the brake cables"))
        self.assertIn("No room today: Patch the demo tubes goes to Saturday.", out)
        self.assertEqual(self.pool("Patch the demo tubes")["start"], SATURDAY.isoformat())
        self.assertNotIn("Patch the demo tubes", self.api.notes["n_today"]["body"])  # never in limbo on today


class JoplinIsTheTruth(Base):

    def setUp(self):
        super().setUp()
        morning.mark_done(self.sched.state_db, DAY, at("09:10"))  # the day is built

    def test_a_line_added_by_hand_gets_its_time_and_one_line_saying_so(self):
        self.api.notes["n_today"]["body"] += "- [ ] Lube the chains\n"
        said = full_day.mirror_today(self.a)
        self.assertTrue(self.planned("Lube the chains"))
        self.assertEqual(len(said), 1)
        line_with(said[0], "Lube the chains", "today's plan at")
        self.assertEqual(full_day.mirror_today(self.a), [])  # said once

    def test_nothing_is_placed_before_the_day_is_built(self):
        with self.sched.state_db._get_connection() as conn:
            conn.execute("DELETE FROM sync_meta WHERE key = ?", (morning.done_key(DAY),))
        self.api.notes["n_today"]["body"] += "- [ ] Lube the chains\n"
        self.assertEqual(full_day.mirror_today(self.a), [])
        self.assertFalse(self.planned("Lube the chains"))

    def test_a_line_whose_item_left_todays_plan_earlier_is_never_given_a_time_and_goes_quietly(self):
        self.on_today("spoke bins today", "Sort the spoke bins")
        span = self.planned("Sort the spoke bins")[0]
        self.t._drop(span)  # its time cleared earlier today, by a path that left its Today line behind
        self.t._save_plan_after_change()
        if "Sort the spoke bins" not in self.api.notes["n_today"]["body"]:
            self.api.notes["n_today"]["body"] += "- [ ] Sort the spoke bins\n"  # the line lingers
        bench = self.api.notes["n_bench"]["body"]
        said = full_day.mirror_today(self.a)
        self.assertEqual(said, [])  # a repair: never told
        self.assertFalse(self.planned("Sort the spoke bins"))  # never given a time again
        self.assertNotIn("Sort the spoke bins", self.api.notes["n_today"]["body"])  # the user took it off today
        self.assertEqual(self.api.notes["n_bench"]["body"], bench)  # a home list is never written to

    def test_an_unmarked_past_item_is_unknown_never_re_placed_or_moved(self):
        self.t.execute("add_event", {"title": "Rear wheel truing", "start": "9:40 AM", "duration": "40 min",
                                     "task_id": "t_wheel", "category": "task"})
        self.assertIn("True the rear wheel", self.api.notes["n_today"]["body"])  # its line, worded as its list item
        set_debug_time(at("15:10"))  # its block went by and nothing was said
        before = [(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope]
        said = full_day.mirror_today(self.a)
        self.assertEqual(said, [])
        self.assertEqual([(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope], before)
        for day in (DAY + timedelta(days=1), SATURDAY):  # never moved on to a later day either
            self.assertFalse(any(s.task_id == "t_wheel" for s in self.t.plan_for(day).spans), day)

    def test_an_id_only_line_for_an_item_already_planned_under_another_id_is_left_alone(self):
        from tests.test_holiday_leftovers import bench_item
        self.a.s.tasks.append(bench_item("a1b2c3d4e5f6", "Replace the brake cables"))  # one title, two ids
        self.t.execute("add_event", {"title": "Cable swap on the commuter", "start": "9:40 AM",  # planned in other
                                     "duration": "40 min", "task_id": "t_cables", "category": "task"})  # words
        self.assertTrue(any(s.task_id == "t_cables" for s in self.t.day_plan.spans))
        set_debug_time(at("15:10"))  # its block went by, nothing said: unknown
        self.api.notes["n_today"]["body"] += "- [ ] a1b2c3d4e5f6\n"  # an old write left the other id as a line
        body, writes = self.api.notes["n_today"]["body"], len(self.api.updates)
        before = [(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope]
        said = full_day.mirror_today(self.a)
        self.assertEqual(said, [])  # never shown
        self.assertEqual([(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope], before)
        self.assertEqual(self.api.notes["n_today"]["body"], body)  # never written to
        self.assertEqual(len(self.api.updates), writes)

    def test_an_id_only_line_that_cant_be_resolved_is_never_placed_removed_or_shown(self):
        self.api.notes["n_today"]["body"] += "- [ ] 0f0e0d0c0b0a\n"
        body = self.api.notes["n_today"]["body"]
        before = [(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope]
        self.assertEqual(full_day.mirror_today(self.a), [])
        self.assertEqual([(s.title, s.start_dt) for s in self.t.day_plan.spans if not s.is_envelope], before)
        self.assertEqual(self.api.notes["n_today"]["body"], body)

    def test_a_refused_write_with_free_time_left_is_never_read_as_a_full_day(self):
        self.on_today("spoke bins today", "Sort the spoke bins")
        self.api.notes["n_today"]["body"] += "- [ ] Lube the chains\n"
        with patch("thirties_core.if_time.place", return_value=(None, False)):  # the write refused, room left
            said = full_day.mirror_today(self.a)
        self.assertFalse(any("No room today" in line for line in said), said)
        self.assertTrue(self.planned("Sort the spoke bins"))  # nothing moved for it
        self.assertIn("- [ ] Lube the chains", self.api.notes["n_today"]["body"])

    def test_one_message_a_sync_however_many_lines_and_none_for_repairs(self):
        from types import SimpleNamespace
        from thirties_core.background import BackgroundService
        self.on_today("spoke bins today", "Sort the spoke bins")
        span = self.planned("Sort the spoke bins")[0]
        self.t._drop(span)
        self.t._save_plan_after_change()
        if "Sort the spoke bins" not in self.api.notes["n_today"]["body"]:
            self.api.notes["n_today"]["body"] += "- [ ] Sort the spoke bins\n"
        self.api.notes["n_today"]["body"] += "- [ ] Lube the chains\n- [ ] Patch the demo tubes\n- [ ] Oil the pedals\n"
        sent = []
        service = BackgroundService(self.sched.config, lambda title, text, kind: sent.append((kind, text)),
                                    scheduler=self.sched, joplin_engine=self.services.joplin,
                                    inference_engine=None, sync_engine=SimpleNamespace(sync=lambda: None))
        service.manager = lambda: SimpleNamespace(tools=self.t, day_plan=self.t.day_plan)
        service.send = lambda text, kind, **_: sent.append((kind, text))
        service.today_sync(at("10:20"))
        self.assertEqual(len(sent), 1, sent)
        for title in ("Lube the chains", "Patch the demo tubes", "Oil the pedals"):
            self.assertIn(title, sent[0][1])
        self.assertNotIn("Sort the spoke bins", sent[0][1])

    def test_a_full_day_moves_the_lowest_item_on_and_the_users_line_takes_its_time(self):
        self.on_today("spoke bins today", "Sort the spoke bins")
        self.api.notes["n_today"]["body"] += "- [ ] Lube the chains\n"
        first, doubled = self.full_until_gone("Sort the spoke bins")
        with first, doubled:
            said = full_day.settle_loose(self.a)
        self.assertIn("No room today: Sort the spoke bins goes to Saturday.", said)
        line_with("\n".join(said), "Lube the chains", "today's plan at")
        self.assertTrue(self.planned("Lube the chains"))
        self.assertIn("- [ ] Lube the chains", self.api.notes["n_today"]["body"])


class ALaterDaysAskWaitsInThePool(Base):

    def test_it_gets_its_time_when_that_day_is_built_and_is_never_shown_without_one(self):
        out = self.say("cable job: sunday is its day", "add_to_today",
                       {"items": ["Replace the brake cables"], "day": "Sunday"})
        self.assertNotIn("no time", out)
        line_with(out, "Replace the brake cables", "Sunday")
        rec = self.pool("Replace the brake cables")
        self.assertEqual((rec["start"], rec["end"], rec["named_day"]), (SUNDAY.isoformat(),) * 3)
        self.assertEqual(self.api.notes["n_today"]["body"], "")
        set_debug_time(at("09:10", 3))
        plan, pending = self.sched.build_day_plan(SUNDAY)
        sunday = PlanTools(plan, pending, self.sched, services=self.services)
        full_day.settle_loose(sunday.assistant)
        self.assertTrue(any(s.title == "Replace the brake cables" for s in sunday.day_plan.spans))
        self.assertEqual(AheadLists(self.sched.state_db).for_day(SUNDAY), [])  # placed, no longer waiting


class TheOldReportIsGone(unittest.TestCase):

    def test_no_tool_or_prompt_line_offers_the_old_no_time_yet_report(self):
        from pathlib import Path
        import thirties_core.brains.frontier as frontier
        from thirties_core.assistant_tools import ASSISTANT_TOOL_SPECS
        from thirties_core.list_tools import LIST_TOOL_SPECS
        names = {s["name"] for s in ASSISTANT_TOOL_SPECS + LIST_TOOL_SPECS}
        self.assertNotIn("pencilled_in", names)  # the old note-minus-plan report
        text = " ".join(str(s) for s in ASSISTANT_TOOL_SPECS + LIST_TOOL_SPECS) + Path(frontier.__file__).read_text()
        self.assertNotIn("no time yet", text)


if __name__ == "__main__":
    unittest.main()
