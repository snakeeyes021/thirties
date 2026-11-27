"""A routine part's steps stay together, whatever kind each step is stored as.

A step the routine's note has no line for is stored as a plain activity, while the note's own steps are tasks linked
to its lines. The day list gave that step a row of its own beside the others, and moving the part left it behind.
Now a part is one row with all its steps in order, a move of any step of it (or of the part) takes the whole part,
a step asked to move on its own moves alone, and steps already split apart come back together when the day is built.

On invented items only: a pottery studio's kiln room routine.
"""

from datetime import timedelta

from tests.fixtures import at
from tests.test_routines import RoutineCase, TITLES
from thirties_core import routine_parts
from thirties_core.astronomy import set_debug_time
from thirties_core.day_lists import planned_ahead_text
from thirties_core.models import SpanCategory
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

KILN = ["Check the kiln", "Wedge clay", "Sweep the wheels"]
EXTRA = "Log the firing"
ALL = KILN + [EXTRA]


class KilnCase(RoutineCase):

    def setUp(self):
        super().setUp()
        TITLES["n_kiln"] = "Kiln room"
        self.addCleanup(TITLES.pop, "n_kiln", None)
        self.api.notes["n_kiln"] = {"id": "n_kiln", "body": "".join(f"- [ ] {s}\n" for s in KILN)}
        self.catalog.set_role("n_kiln", "routine", "Kiln room")
        self.api.notes["n_routine"]["body"] = ""  # the other routines have nothing due today
        self.api.notes["n_clean"]["body"] = ""
        self.t = self.tools_for(self.t.day_plan.target_date)
        set_debug_time(at("07:40"))

    def put(self, start="9:30 AM", items=None, **more):
        out = self.t.execute("put_routine", {"routine": "Kiln room", "parts": [
            {"start": start, "items": items or ALL}], **more})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out

    def span(self, title):
        found = [s for s in self.t.day_plan.spans if s.title == title]
        self.assertEqual(len(found), 1, (title, [(s.title, s.start_dt) for s in self.t.day_plan.spans]))
        return found[0]

    def in_block(self, steps, hhmm):
        for step in steps:
            s = self.span(step).start_dt
            self.assertTrue(at(hhmm) <= s < at(hhmm) + timedelta(minutes=30), (step, s, hhmm))

    def said(self, message, tool, args):
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def lines(self):
        text = planned_ahead_text(self.t.assistant)
        return [x for x in text.splitlines()[1:]]


class TestOneRow(KilnCase):

    def test_the_step_stored_as_an_activity_is_set_up_as_one_kind_of_thing_in_its_part(self):
        self.put()
        # the note's own steps are tasks; the one with no line in the note is a plain activity
        self.assertEqual(self.span(EXTRA).category, SpanCategory.ACTIVITY)
        self.assertEqual(self.span("Wedge clay").category, SpanCategory.TASK)
        self.assertEqual(self.lines(), ["9:30 AM Check the kiln, Wedge clay, Sweep the wheels, Log the firing"])

    def test_it_is_one_row_where_the_routines_usual_placement_has_it_but_nothing_recorded_the_day(self):
        self.t.execute("set_routine", {"routine": "Kiln room", "confirm": True,
                                       "parts": [{"start": "9:30 AM", "items": ALL}]})
        self.put()
        self.t.assistant.routines.db.set_sync_meta(f"routine_parts:{self.t.day_plan.target_date.isoformat()}", "{}")
        self.assertEqual(self.lines(), ["9:30 AM Check the kiln, Wedge clay, Sweep the wheels, Log the firing"])

    def test_the_morning_set_up_lists_one_row(self):
        self.t.execute("set_routine", {"routine": "Kiln room", "confirm": True,
                                       "parts": [{"start": "9:30 AM", "items": ALL}]})
        self.t.assistant.routines.set_up_current()
        self.assertEqual(self.lines(), ["9:30 AM Check the kiln, Wedge clay, Sweep the wheels, Log the firing"])


class TestAPartMovesWhole(KilnCase):

    def setUp(self):
        super().setUp()
        self.put()
        self.in_block(ALL, "09:30")

    def test_put_routine_naming_the_notes_steps_takes_the_other_step_too(self):
        out = self.t.execute("put_routine", {"routine": "Kiln room", "parts": [
            {"start": "11:30 AM", "items": KILN}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_block(ALL, "11:30")
        self.assertEqual(self.lines(), ["11:30 AM Check the kiln, Wedge clay, Sweep the wheels, Log the firing"])

    def test_put_routine_naming_only_the_stored_kind_takes_the_rest(self):
        self.t.execute("put_routine", {"routine": "Kiln room", "parts": [{"start": "11:30 AM", "items": [EXTRA]}]})
        self.in_block(ALL, "11:30")

    def test_put_routine_asked_for_one_step_on_its_own_moves_only_that_one(self):
        out = self.t.execute("put_routine", {"routine": "Kiln room", "alone": True, "parts": [
            {"start": "11:30 AM", "items": ["Wedge clay"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_block(["Wedge clay"], "11:30")
        self.in_block(["Check the kiln", "Sweep the wheels", EXTRA], "09:30")

    def test_the_stored_kind_asked_to_move_on_its_own_moves_alone(self):
        self.t.execute("put_routine", {"routine": "Kiln room", "alone": True, "parts": [
            {"start": "11:30 AM", "items": [EXTRA]}]})
        self.in_block([EXTRA], "11:30")
        self.in_block(KILN, "09:30")

    def test_moving_one_step_with_update_event_takes_its_part(self):
        out = self.said("The kiln check should be later, 11:30", "update_event",
                        {"event": "Check the kiln", "start": "11:30 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("moved with it", out)
        self.assertGreaterEqual(self.span("Check the kiln").start_dt, at("11:30"))
        self.in_block(["Wedge clay", "Sweep the wheels", EXTRA], "11:30")

    def test_moving_the_stored_kind_with_update_event_takes_its_part_too(self):
        self.said("Log the firing belongs later, 11:30", "update_event", {"event": EXTRA, "start": "11:30 AM"})
        self.in_block(KILN, "11:30")

    def test_every_step_moved_one_by_one_still_lands_as_one_part(self):
        outs = [self.said("Kiln room goes later", "update_event", {"event": s, "start": "11:30 AM"})
                for s in ALL]
        for out in outs:
            self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_block(ALL, "11:30")
        self.assertEqual(self.lines(), ["11:30 AM Check the kiln, Wedge clay, Sweep the wheels, Log the firing"])

    def test_one_step_asked_to_move_on_its_own_moves_only_that_one(self):
        self.said("only the wedging goes later, 11:30", "update_event",
                  {"event": "Wedge clay", "start": "11:30 AM", "alone": True})
        self.in_block(["Wedge clay"], "11:30")
        self.in_block(["Check the kiln", "Sweep the wheels", EXTRA], "09:30")
        # and it stays its own part: the day isn't pulled back together
        routine_parts.regroup(self.t.assistant, self.t.day_plan)
        self.in_block(["Wedge clay"], "11:30")
        self.in_block(["Check the kiln", "Sweep the wheels", EXTRA], "09:30")

    def test_a_step_moved_by_hand_moves_alone(self):
        self.t.by_hand = True
        try:
            self.t.execute("update_event", {"event": "Wedge clay", "start": "11:30 AM"})
        finally:
            self.t.by_hand = False
        self.in_block(["Wedge clay"], "11:30")
        self.in_block(["Check the kiln", "Sweep the wheels", EXTRA], "09:30")


class TestSplitPartsComeBackTogether(KilnCase):

    def split(self):
        lone = self.span(EXTRA)
        lone.start_dt, lone.end_dt = at("18:10"), at("18:20")
        self.t._persist(lone)
        self.in_block([EXTRA], "18:00")

    def test_the_build_puts_a_split_part_back_together(self):
        self.put()
        self.split()
        self.t.assistant.routines.set_up_current()
        self.in_block(ALL, "09:30")

    def test_a_split_part_with_no_record_of_the_day_comes_back_by_the_usual_placement(self):
        self.t.execute("set_routine", {"routine": "Kiln room", "confirm": True,
                                       "parts": [{"start": "9:30 AM", "items": ALL}]})
        self.put()
        self.t.assistant.routines.db.set_sync_meta(f"routine_parts:{self.t.day_plan.target_date.isoformat()}", "{}")
        self.split()
        self.assertEqual(routine_parts.regroup(self.t.assistant, self.t.day_plan), 1)
        self.in_block(ALL, "09:30")

    def test_a_part_that_isnt_split_is_left_alone(self):
        self.put()
        before = {s.title: s.start_dt for s in self.t.day_plan.spans}
        self.assertEqual(routine_parts.regroup(self.t.assistant, self.t.day_plan), 0)
        self.assertEqual(before, {s.title: s.start_dt for s in self.t.day_plan.spans})
