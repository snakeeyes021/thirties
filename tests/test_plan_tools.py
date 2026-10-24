"""Unit tests for thirties_core.plan_tools: the shared schedule tool API."""

import unittest

from tests.fixtures import DAY, at, event, make_scheduler, occupant
from thirties_core.astronomy import set_debug_time
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

    def test_replacing_never_wipes_out_an_item(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Throne", "start": "3:00 PM", "duration": "30 min"})
        out = t.execute("add_event", {"title": "Shower", "start": "3:00 PM", "duration": "30 min",
                                      "replace_overlaps": True})
        self.assertTrue(out.startswith(NOT_DONE) and "alongside" in out, out)  # 10-21: "removed Throne"
        self.assertEqual(occupant(t.day_plan, "15:00"), "Throne")

    def test_fixed_things_keep_their_minutes_and_flexible_ones_keep_to_blocks(self) -> None:
        t = tools_for()
        out = t.execute("add_event", {"title": "Uber to the airport", "start": "12:15 PM", "duration": "30 min"})
        self.assertTrue(out.startswith("Added Uber to the airport, 12:15 PM – 12:45 PM (30m) (blocks 12:00 PM – "
                                       "1:00 PM, fixed)"), out)
        out = t.execute("add_event", {"title": "Packing", "start": "3:45 PM", "duration": "1 hour"})
        self.assertTrue(out.startswith("Added Packing, 4:00 PM – 5:00 PM (1h)"), out)  # flexible: the next block
        out = t.execute("add_event", {"title": "Hold music", "start": "5:15 PM", "duration": "1 hour", "fixed": True})
        self.assertIn("5:15 PM – 6:15 PM", out)
        out = t.execute("add_event", {"title": "Laundry", "start": "6:00 PM", "duration": "30 min", "push_later": True})
        self.assertTrue(out.startswith(NOT_DONE) and "Hold music" in out, out)  # a fixed thing doesn't slide

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

    def test_new_start_alone_moves_an_activity(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Sort the mail", "start": "6:30 PM", "end": "7:30 PM"})
        t.execute("update_event", {"event": "sort the mail", "start": "5:00 PM"})
        self.assertEqual(occupant(t.day_plan, "17:30"), "Sort the mail")
        self.assertIsNone(occupant(t.day_plan, "18:00"))

    def test_moving_a_missed_item_to_another_day_records_the_skip(self) -> None:
        set_debug_time(at("17:00"))
        self.addCleanup(set_debug_time, None)
        t = tools_for()
        t.execute("add_event", {"title": "File taxes", "start": "2:00 PM", "end": "3:00 PM"})
        out = t.execute("update_event", {"event": "file taxes", "to_day": "tomorrow", "start": "8:00 AM"})
        self.assertIn("marked skipped", out)
        today = next(s for s in t.day_plan.spans if s.title == "File taxes")
        self.assertEqual(today.status.value, "skipped")
        tomorrow = t.plan_for("tomorrow")
        self.assertEqual([s.status.value for s in tomorrow.spans if s.title == "File taxes"], ["planned"])

    def test_short_items_stack_in_their_thirty(self) -> None:
        t = tools_for()
        for title in ("Feed the cats", "Message Brad", "Water the alligator"):
            self.assertFalse(t.execute("add_event", {"title": title, "start": "5:00 PM", "duration": "5 min"})
                             .startswith(NOT_DONE))
        spans = sorted((s for s in t.day_plan.spans if s.duration_minutes == 5), key=lambda s: s.start_dt)
        self.assertEqual([(s.title, s.start_dt.strftime("%H:%M")) for s in spans],
                         [("Feed the cats", "17:00"), ("Message Brad", "17:05"), ("Water the alligator", "17:10")])
        self.assertIn("- 5:00 PM – 5:30 PM: Feed the cats id=", t.list_schedule())
        self.assertIn(", then Message Brad id=", t.list_schedule())
        self.assertIn("5:05 PM – 5:10 PM: Message Brad", t.list_schedule(exact=True))
        block = t.day_plan.block_at(at("17:01"))
        self.assertEqual(t.day_plan.display_title(block), "Feed the cats\nMessage Brad\nWater the alligator")
        out = t.execute("add_event", {"title": "Laundry", "start": "5:00 PM", "duration": "20 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # no room left in that thirty: a conflict, not an overlap

    def test_the_user_only_ever_sees_thirties(self) -> None:
        t = tools_for()
        out = t.execute("add_event", {"title": "Glass", "start": "5:50 PM", "duration": "30 min"})
        self.assertIn("Glass, 6:00 PM – 6:30 PM", out)  # it wouldn't fit in the 5:30 thirty: next one
        t.execute("add_event", {"title": "Brad", "start": "5:00 PM", "duration": "15 min"})
        free = t.open_windows(after=at("17:10"))
        self.assertEqual(free[0][0], at("17:30"))  # the 5:00 thirty holds something: not free
        out = t.execute("add_event", {"title": "Food", "start": "5:30 PM", "duration": "1 hour", "push_later": True})
        self.assertIn("Glass now 6:30 PM – 7:00 PM", out)  # pushed along to the next thirty
        out = t.execute("update_event", {"event": "food", "shift": "+10 minutes", "push_later": True})
        self.assertIn("Food, 6:00 PM – 7:00 PM", out.split("→")[1])  # a 10-minute nudge still lands on a thirty
        self.assertNotRegex(t.list_schedule(), r":(?!00|30)\d\d")
        self.assertRegex(t.list_schedule(exact=True), r"5:15 PM")  # exact minutes only when asked

    def test_two_things_stay_two_items(self) -> None:
        t = tools_for()
        t.execute("add_event", {"title": "Thirties dev", "start": "5:00 PM", "duration": "1 hour"})
        t.execute("add_event", {"title": "Message Brad back", "start": "7:00 PM", "duration": "15 min"})
        out = t.execute("add_event", {"title": "Thirties dev / Message Brad", "start": "5:00 PM", "duration": "1 hour"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("alongside=true", out)
        out = t.execute("update_event", {"event": "thirties dev", "title": "Thirties dev & Message Brad back"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = t.execute("add_event", {"title": "Rice and beans", "start": "8:00 PM", "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)  # one thing that happens to say "and"

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


class TestInsertAndSwap(unittest.TestCase):

    def day(self) -> PlanTools:
        t = tools_for([event("dentist", "21:30", "22:00", "Late call")])
        t.execute("remove_event", {"event": "Work"})
        for title, start, end in [("Upgrades", "1:00 PM", "2:00 PM"), ("Shower", "2:00 PM", "2:30 PM"),
                                  ("Chores", "2:30 PM", "3:30 PM"), ("Reading", "5:00 PM", "6:00 PM")]:
            t.execute("add_event", {"title": title, "start": start, "end": end})
        return t

    def test_insert_slides_following_items_later(self) -> None:
        t = self.day()
        out = t.execute("add_event", {"title": "Lunch", "start": "1:00 PM", "duration": "30 min", "push_later": True})
        self.assertIn("Upgrades now 1:30 PM – 2:30 PM", out)
        self.assertEqual(occupant(t.day_plan, "13:00"), "Lunch")
        self.assertEqual(occupant(t.day_plan, "14:30"), "Shower")
        self.assertEqual(occupant(t.day_plan, "15:30"), "Chores")
        self.assertEqual(occupant(t.day_plan, "17:00"), "Reading")  # the gap absorbed the slide

    def test_slide_stops_before_fixed_commitments(self) -> None:
        t = tools_for([event("call", "15:00", "15:30", "Call")])
        t.execute("remove_event", {"event": "Work"})
        t.execute("add_event", {"title": "Upgrades", "start": "2:00 PM", "end": "3:00 PM"})
        out = t.execute("add_event", {"title": "Lunch", "start": "2:00 PM", "duration": "30 min", "push_later": True})
        self.assertTrue(out.startswith(NOT_DONE) and "Call" in out, out)
        self.assertEqual(occupant(t.day_plan, "14:00"), "Upgrades")

    def test_lengthening_an_item_can_push(self) -> None:
        t = self.day()
        t.execute("update_event", {"event": "Upgrades", "end": "2:30 PM", "push_later": True})
        self.assertEqual(occupant(t.day_plan, "14:00"), "Upgrades")
        self.assertEqual(occupant(t.day_plan, "14:30"), "Shower")

    def test_swap_back_to_back_items_trades_order(self) -> None:
        t = self.day()
        out = t.execute("swap_events", {"first": "Upgrades", "second": "Shower"})
        self.assertTrue(out.startswith("Swapped"), out)
        self.assertEqual(occupant(t.day_plan, "13:00"), "Shower")
        self.assertEqual(occupant(t.day_plan, "13:30"), "Upgrades")
        self.assertEqual(occupant(t.day_plan, "14:00"), "Upgrades")
        self.assertEqual(occupant(t.day_plan, "14:30"), "Chores")

    def test_swap_apart_items_trades_start_times(self) -> None:
        t = self.day()
        t.execute("swap_events", {"first": "Upgrades", "second": "Reading"})
        self.assertEqual(occupant(t.day_plan, "13:00"), "Reading")
        self.assertEqual(occupant(t.day_plan, "17:00"), "Upgrades")

    def test_swap_refuses_when_lengths_would_collide(self) -> None:
        t = self.day()
        out = t.execute("swap_events", {"first": "Shower", "second": "Reading"})  # Reading is 1h, Chores follows Shower
        self.assertTrue(out.startswith(NOT_DONE) and "Chores" in out, out)
        self.assertEqual(occupant(t.day_plan, "14:00"), "Shower")


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
        self.assertIn("8:30 AM – 4:00 PM: Work [work]", text)
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
