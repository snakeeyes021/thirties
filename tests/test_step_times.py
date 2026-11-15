"""A clock time typed at the start of a routine step ("7:45 Water the plants") is the step's time, not its name.

Kept in the title, it showed next to the plan's own time ("8:00 AM 7:45 Water the plants"), the user had to keep
asking to fix the times, and the reply checks read the title's time as a claim about the plan.
"""

import unittest
from datetime import time, timedelta

from tests.fixtures import DAY, at
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.item_ids import line_id
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.plan_tools import NOT_DONE
from thirties_core.routines import Anchor, Routine
from thirties_core.step_times import clean_title, split_leading_time, step_start

TIMED = ["7:45 Water the plants", "Make coffee", "11:30 Stretch break", "21:30 Lights out"]


class TestReadingALeadingTime(unittest.TestCase):
    def test_times_people_type_before_a_step(self):
        cases = {
            "7:45 Water the plants": ("Water the plants", time(7, 45), False),
            "07:45 - Water the plants": ("Water the plants", time(7, 45), True),
            "21:30 Lights out": ("Lights out", time(21, 30), True),
            "9:15pm Lay out clothes": ("Lay out clothes", time(21, 15), True),
            "7 am: Feed the cat": ("Feed the cat", time(7, 0), True),
            "12:15 Lunch walk": ("Lunch walk", time(12, 15), False),
        }
        for text, want in cases.items():
            self.assertEqual(split_leading_time(text), want, text)

    def test_numbers_that_are_no_time_stay_in_the_title(self):
        for text in ("15-min stretch", "3 sets of squats", "10.5 km bike ride", "2 loads of laundry", "10:30",
                     "Call the bank at 3:00"):
            title, t, _ = split_leading_time(text)
            self.assertIsNone(t, text)
            self.assertEqual(title, text.strip())

    def test_morning_or_evening_is_read_from_where_the_step_was_placed(self):
        self.assertEqual(step_start("9:30 Read a chapter", at("20:00")), at("21:30"))
        self.assertEqual(step_start("9:30 Read a chapter", at("08:30")), at("09:30"))
        self.assertEqual(step_start("21:30 Lights out", at("08:00")), at("21:30"))
        self.assertIsNone(step_start("Make coffee", at("08:00")))


class TimedRoutineCase(RoutineCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("07:10"))
        self.routine_body = "".join(f"- [ ] {s}\n" for s in TIMED)
        self.api.notes["n_routine"]["body"] = self.routine_body
        self.routines = self.t.assistant.routines

    def titles(self):
        return [s.title for s in self.t.day_plan.spans if not s.is_envelope]


class TestPlacingATimedRoutine(TimedRoutineCase):
    def test_the_time_leaves_the_title_and_becomes_the_steps_time(self):
        out = self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "8:00 AM", "items": ["7:45 Water the plants", "Make coffee"]},
            {"start": "11:30 AM", "items": ["11:30 Stretch break"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("7:45 Water", out)
        water = self.spans(self.t, "Water the plants")
        self.assertEqual(len(water), 1, self.titles())
        self.assertEqual(water[0].start_dt, at("07:45"))  # the step's own time, not the part's 8:00
        self.assertEqual(self.spans(self.t, "Make coffee")[0].start_dt, at("08:00"))  # right after it
        self.assertEqual(len(self.spans(self.t, "Stretch break")), 1)
        self.assertFalse(any(t[:1].isdigit() for t in self.titles()), self.titles())
        # still the routine's own step: linked to its line in the note, which is never written
        self.assertEqual(water[0].task_id, line_id(self.sched.state_db, "n_routine", "7:45 Water the plants"))
        self.assertEqual(self.body("n_routine"), self.routine_body)

    def test_named_without_its_time_its_still_the_step(self):
        out = self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "8:00 AM", "items": ["Water the plants", "Make coffee"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("isn't in the", out)  # not taken for an extra step
        water = self.spans(self.t, "Water the plants")
        self.assertEqual(water[0].task_id, line_id(self.sched.state_db, "n_routine", "7:45 Water the plants"))

    def test_the_schedule_and_todays_list_show_one_time(self):
        self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "8:00 AM", "items": ["7:45 Water the plants", "Make coffee"]}]})
        shown = self.t.execute("show_schedule", {})
        self.assertIn("Water the plants", shown)
        self.assertNotIn("7:45 Water", shown)
        self.assertIn("Water the plants", self.body("n_today"))
        self.assertNotIn("7:45 Water", self.body("n_today"))

    def test_the_learned_placement_keeps_names_without_times(self):
        self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "8:00 AM", "items": ["7:45 Water the plants", "Make coffee"]}]})
        parts = self.routines.book().get("n_routine").parts
        items = [i for p in parts for i in p["items"]]
        self.assertIn("Water the plants", items)
        self.assertFalse(any(i[:1].isdigit() for i in items), items)
        self.assertEqual(parts[0]["at"], "07:30")  # learned where it went: the 7:30 thirty

    def test_a_time_before_the_day_starts_never_moves_it_to_the_next_morning(self):
        self.api.notes["n_routine"]["body"] = "- [ ] 6:20 Water the plants\n- [ ] Make coffee\n"
        out = self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "7:00 AM", "items": ["6:20 Water the plants", "Make coffee"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        water = self.spans(self.t, "Water the plants")
        self.assertEqual(water[0].start_dt, at("07:00"))  # today's canvas starts at 7:00; tomorrow's 6:20 is wrong

    def test_a_daily_anchor_typed_with_its_time(self):
        out = self.t.execute("set_anchor", {"title": "18:15 Feed the cat"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        anchors = {a.title: a.at for a in self.routines.book().anchors()}
        self.assertEqual(anchors, {"Feed the cat": "18:15"})


class TestStoredTitlesAreCleaned(TimedRoutineCase):
    def test_old_copies_lose_their_leading_time_once_and_the_note_is_left_alone(self):
        book = self.routines.book()
        book.save(Routine("n_routine", "Morning Routine", parts=[
            {"items": ["7:45 Water the plants", "Make coffee"], "at": "07:30", "fallback": ""}]))
        book.save_anchor(Anchor("18:15 Feed the cat", "18:15"))
        old = EventSpan("old-water", "7:45 Water the plants", at("07:45"), at("08:00"), SpanCategory.TASK,
                        task_id=line_id(self.sched.state_db, "n_routine", "7:45 Water the plants"))
        other = EventSpan("old-call", "10:00 Call the plumber", at("10:00"), at("10:30"), SpanCategory.ACTIVITY)
        for s in (old, other):
            self.t.day_plan.spans.append(s)
            self.t._persist(s)
        changed = self.routines.clean_stored_titles(self.t.day_plan)
        self.assertEqual(changed, 3)
        self.assertEqual(self.spans(self.t, "Water the plants")[0].start_dt, at("07:45"))
        self.assertEqual(len(self.spans(self.t, "10:00 Call the plumber")), 1)  # not a step: left as the user named it
        stored = {s.id: s.title for s in self.sched.state_db.get_spans_between(at("00:00"), at("23:59"))}
        self.assertEqual(stored["old-water"], "Water the plants")
        self.assertEqual(book.get("n_routine").parts[0]["items"], ["Water the plants", "Make coffee"])
        self.assertEqual([(a.title, a.at) for a in book.anchors()], [("Feed the cat", "18:15")])
        self.assertEqual(self.body("n_routine"), self.routine_body)
        self.routines._titles_cleaned.clear()  # safe to run again: nothing left to clean
        self.assertEqual(self.routines.clean_stored_titles(self.t.day_plan), 0)

    def test_it_runs_before_a_turn(self):
        old = EventSpan("old-stretch", "11:30 Stretch break", at("11:30"), at("12:00"), SpanCategory.TASK)
        self.t.day_plan.spans.append(old)
        self.t._persist(old)
        self.routines.set_up_current()
        self.assertEqual(old.title, "Stretch break")


class TestTheReplyCheckReadsTitlesAsNames(TimedRoutineCase):
    def test_a_time_inside_a_title_is_no_claim_about_the_plan(self):
        from thirties_core.brains.frontier import FrontierBrain
        span = EventSpan("s-walk", "12:15 Midday stroll", at("12:00"), at("12:45"), SpanCategory.ACTIVITY)
        self.t.day_plan.spans.append(span)
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.t
        self.assertEqual(brain._wrong_times("12:00 PM – 12:15 Midday stroll (until 12:45 PM)"), [])
        self.assertEqual(brain._wrong_times("The 12:15 Midday stroll is at 12:00."), [])
        self.assertTrue(brain._wrong_times("12:15 Midday stroll is at 3:00 PM."))  # a real wrong time still counts


if __name__ == "__main__":
    unittest.main()
