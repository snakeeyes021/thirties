"""Unit tests for thirties_core.plan_tools: the shared schedule tool API."""

import unittest

from tests.fixtures import DAY, at, event, make_scheduler, occupant
from thirties_core.models import SpanCategory
from thirties_core.plan_tools import NOT_DONE, PlanTools


def tools_for(events=None) -> PlanTools:
    sched = make_scheduler(events)
    plan, pending = sched.build_day_plan(DAY)
    return PlanTools(plan, pending, sched)


def span_id(tools: PlanTools, title: str) -> str:
    return next(s.id for s in tools.day_plan.spans if s.title == title)


class TestAddEvent(unittest.TestCase):

    def test_duration_is_computed_by_the_tool(self) -> None:
        t = tools_for()
        out = t.execute("add_event", {"title": "Composing", "start": "5:00 PM", "duration": "2 hours"})
        self.assertTrue(out.startswith("Added Composing, 5:00 PM – 7:00 PM (2h)"), out)
        self.assertEqual(occupant(t.day_plan, "18:30"), "Composing")
        self.assertIsNone(occupant(t.day_plan, "19:00"))

    def test_end_time_and_task_category(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Taxes", "start": "8:00 PM", "end": "9:00 PM", "task_id": "t_taxes"})
        span = t.day_plan.get_span(span_id(t, "Taxes"))
        self.assertEqual(span.category, SpanCategory.TASK)
        self.assertEqual(span.task_id, "t_taxes")

    def test_activity_may_sit_on_top_of_work(self) -> None:
        t = tools_for()
        out = t.execute("add_event", {"title": "Standup", "start": "9:00 AM", "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(occupant(t.day_plan, "09:00"), "Standup")
        self.assertEqual(occupant(t.day_plan, "09:30"), "Work")

    def test_calendar_commitment_is_never_overwritten(self) -> None:
        t = tools_for([event("dentist", "15:00", "16:00", "Dentist")])
        out = t.execute("add_event", {"title": "Gym", "start": "2:30 PM", "end": "4:00 PM", "replace_overlaps": True})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Dentist", out)
        self.assertEqual(occupant(t.day_plan, "15:00"), "Dentist")
        self.assertNotIn("Gym", [s.title for s in t.day_plan.spans])

    def test_overlapping_activity_needs_permission_then_is_trimmed(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Composing", "start": "5:00 PM", "end": "7:00 PM"})
        out = t.execute("add_event", {"title": "Dinner", "start": "6:30 PM", "end": "7:30 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = t.execute("add_event", {"title": "Dinner", "start": "6:30 PM", "end": "7:30 PM", "replace_overlaps": True})
        self.assertIn("trimmed Composing", out)
        self.assertEqual(occupant(t.day_plan, "18:00"), "Composing")
        self.assertEqual(occupant(t.day_plan, "18:30"), "Dinner")

    def test_bad_input_is_reported_not_raised(self) -> None:
        t = tools_for()
        for args in ({"title": "X", "start": "5:00"}, {"title": "X", "start": "5:00 PM"},
                     {"title": "X", "start": "5:00 PM", "duration": "a while"}, {"_parse_error": "bad json"}):
            self.assertTrue(t.execute("add_event", args).startswith(NOT_DONE), args)
        self.assertTrue(t.execute("teleport", {}).startswith(NOT_DONE))


class TestUpdateEvent(unittest.TestCase):

    def test_work_ran_long_moves_only_the_end(self) -> None:
        t = tools_for()
        sleep_before = t.day_plan.get_span(span_id(t, "Sleep"))
        out = t.execute("update_event", {"event": span_id(t, "Work"), "end": "4:30 PM"})
        self.assertIn("→ Work, 8:30 AM – 4:30 PM", out)
        self.assertEqual(occupant(t.day_plan, "16:00"), "Work")
        self.assertIsNone(occupant(t.day_plan, "16:30"))
        self.assertEqual(t.day_plan.get_span(sleep_before.id), sleep_before)

    def test_late_start_keeps_end(self) -> None:
        t = tools_for()
        t.execute("update_event", {"event": "Work", "start": "10:00 AM"})
        self.assertIsNone(occupant(t.day_plan, "09:30"))
        self.assertEqual(occupant(t.day_plan, "10:00"), "Work")
        self.assertEqual(occupant(t.day_plan, "15:30"), "Work")

    def test_shift_keeps_duration(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Composing", "start": "5:00 PM", "end": "7:00 PM"})
        t.execute("update_event", {"event": "composing", "shift": "+1 hour"})
        self.assertIsNone(occupant(t.day_plan, "17:00"))
        self.assertEqual(occupant(t.day_plan, "19:30"), "Composing")
        self.assertIsNone(occupant(t.day_plan, "20:00"))

    def test_sleep_tonight_crosses_midnight(self) -> None:
        t = tools_for()
        t.execute("update_event", {"event": "Sleep", "start": "10:30 PM", "end": "6:30 AM"})
        self.assertEqual(occupant(t.day_plan, "22:30"), "Sleep")
        self.assertEqual(occupant(t.day_plan, "06:00", 1), "Sleep")
        self.assertIsNone(occupant(t.day_plan, "06:30", 1))

    def test_calendar_items_cannot_be_moved_or_removed(self) -> None:
        t = tools_for([event("dentist", "15:00", "16:00", "Dentist")])
        self.assertTrue(t.execute("update_event", {"event": "Dentist", "shift": "+1 hour"}).startswith(NOT_DONE))
        self.assertTrue(t.execute("remove_event", {"event": "cal:dentist"}).startswith(NOT_DONE))

    def test_ambiguous_reference_asks_for_id(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Walk the dog", "start": "7:30 AM", "duration": "30 min"})
        t.execute("add_event", {"title": "Walk to store", "start": "5:00 PM", "duration": "30 min"})
        out = t.execute("update_event", {"event": "walk", "shift": "+30 min"})
        self.assertTrue(out.startswith(NOT_DONE) and "several" in out, out)

    def test_changes_persist(self) -> None:
        t = tools_for()
        t.execute("update_event", {"event": "Work", "end": "4:30 PM"})
        plan, _ = t.scheduler.build_day_plan(DAY)
        self.assertEqual(occupant(plan, "16:00"), "Work")


class TestRemoveAndClear(unittest.TestCase):

    def test_day_off_removes_work_only(self) -> None:
        t = tools_for()
        t.execute("remove_event", {"event": "Work"})
        self.assertIsNone(occupant(t.day_plan, "10:00"))
        self.assertEqual(occupant(t.day_plan, "23:00"), "Sleep")
        self.assertIsNone(occupant(t.scheduler.build_day_plan(DAY)[0], "10:00"))

    def test_clear_time_splits_and_keeps_calendar(self) -> None:
        t = tools_for([event("call", "18:00", "18:30", "Call")])
        t.execute("add_event", {"title": "Reading", "start": "5:00 PM", "end": "6:00 PM"})
        t.execute("add_event", {"title": "Chores", "start": "6:30 PM", "end": "9:00 PM"})
        out = t.execute("clear_time", {"start": "5:30 PM", "end": "7:00 PM"})
        self.assertIn("trimmed Reading", out)
        self.assertEqual(occupant(t.day_plan, "17:00"), "Reading")
        self.assertIsNone(occupant(t.day_plan, "17:30"))
        self.assertEqual(occupant(t.day_plan, "18:00"), "Call")
        self.assertEqual(occupant(t.day_plan, "19:00"), "Chores")

        out = t.execute("clear_time", {"start": "7:30 PM", "end": "8:00 PM"})
        self.assertIn("split Chores", out)
        self.assertEqual(occupant(t.day_plan, "19:00"), "Chores")
        self.assertIsNone(occupant(t.day_plan, "19:30"))
        self.assertEqual(occupant(t.day_plan, "20:00"), "Chores")

    def test_clear_time_leaves_work_unless_asked(self) -> None:
        t = tools_for()
        t.execute("clear_time", {"start": "9:00 AM", "end": "10:00 AM"})
        self.assertEqual(occupant(t.day_plan, "09:00"), "Work")
        t.execute("clear_time", {"start": "9:00 AM", "end": "10:00 AM", "include_work": True})
        self.assertIsNone(occupant(t.day_plan, "09:00"))
        self.assertEqual(occupant(t.day_plan, "10:00"), "Work")


class TestReadOnlyAndCalendar(unittest.TestCase):

    def test_find_open_time(self) -> None:
        t = tools_for()
        out = t.execute("find_open_time", {"duration": "2 hours"})
        self.assertIn("4:00 PM – 11:00 PM (7h", out)
        self.assertNotIn("7:00 AM – 8:30 AM", out)
        out = t.execute("find_open_time", {"daylight": True})
        self.assertIn("4:00 PM – 6:31 PM", out)

    def test_schedule_text_lists_ids_and_free_time(self) -> None:
        t = tools_for([event("dinner", "19:00", "21:00", "Team dinner", invite=True)])
        text = t.execute("list_schedule", {})
        self.assertIn("8:30 AM – 4:00 PM: Work (7h 30m) [work]", text)
        self.assertIn("UNCONFIRMED invite", text)
        self.assertIn(f"id={span_id(t, 'Work')}", text)
        self.assertIn("Free time: 7:00 AM – 8:30 AM", text)

    def test_resolve_invite_attend_and_decline(self) -> None:
        t = tools_for([event("dinner", "19:00", "21:00", "Team dinner", invite=True),
                       event("party", "21:00", "22:00", "Party", invite=True)])
        self.assertEqual(len(t.ambiguous_events), 2)
        t.execute("resolve_calendar_event", {"event": "Team dinner", "attend": True})
        t.execute("resolve_calendar_event", {"event": "cal:party", "attend": "false"})
        self.assertEqual(t.ambiguous_events, [])
        self.assertEqual(occupant(t.day_plan, "19:00"), "Team dinner")
        self.assertNotIn("Party", [s.title for s in t.day_plan.spans])
        plan, pending = t.scheduler.build_day_plan(DAY)
        self.assertEqual((pending, occupant(plan, "19:00"), occupant(plan, "21:00")), ([], "Team dinner", None))

    def test_finalize_and_unfinalize_on_change(self) -> None:
        t = tools_for()
        t.execute("finalize_day", {"notes": "set"})
        self.assertTrue(t.day_plan.is_finalized)
        t.execute("add_event", {"title": "Walk", "start": "5:00 PM", "duration": "30 min"})
        self.assertFalse(t.day_plan.is_finalized)
        self.assertFalse(t.scheduler.state_db.get_day_meta(DAY).is_finalized)

    def test_snapshot_delta(self) -> None:
        t = tools_for()
        before = t.capture_snapshot()
        t.execute("add_event", {"title": "Walk", "start": "5:00 PM", "duration": "30 min"})
        t.execute("update_event", {"event": "Work", "end": "4:30 PM"})
        t.execute("list_schedule", {})
        delta = t.compute_delta(before, t.capture_snapshot())
        self.assertEqual((len(delta.added), len(delta.changed), len(delta.removed)), (1, 1, 0))
        self.assertEqual(delta.total_changes, 2)


if __name__ == "__main__":
    unittest.main()
