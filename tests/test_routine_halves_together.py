"""A routine's later part follows its earlier one; words about where a part sits ask for a move; put_routine says only
what moved.

- Set up late (past the routine's usual time), the day's set-up put the first part now and sent the later part to
  its fallback. A later part follows right after the part before it, in order; the fallback is the model's call
  for a day with no room, or the user's.
- Told that a routine's later part sat in the wrong place, the model moved its steps, and each one was refused as
  a move nobody asked for. Words about where something on the plan sits give the model's reading a basis; which
  items they cover is still checked (move_scope.py): the routine they name, or the part a named step is in.
- put_routine said every step of a part had moved while only the first had: the others went back to the time the
  user had only quoted. Each step now lands where it's asked to, and the result is written from where they are.

On invented items only: a workshop's closing routine.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import at
from tests.test_routines import RoutineCase, TITLES
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE, PlanTools, ToolError

EARLIER = ["Unlock the shed", "Oil the vice", "Sharpen the chisels", "Sweep the bench"]
LATER = ["Check the glue pots", "Sort the offcuts", "Label the jars", "Wind the radio"]


class ClosingCase(RoutineCase):

    def setUp(self):
        super().setUp()
        TITLES["n_closing"] = "Workshop Closing"
        self.addCleanup(TITLES.pop, "n_closing", None)
        self.api.notes["n_closing"] = {"id": "n_closing", "body": "".join(f"- [ ] {s}\n" for s in EARLIER + LATER)}
        self.catalog.set_role("n_closing", "routine", "Workshop Closing")
        self.api.notes["n_routine"]["body"] = ""  # the other routines have nothing due today
        self.api.notes["n_clean"]["body"] = ""
        self.t = self.tools_for(self.t.day_plan.target_date)

    def start(self, title):
        found = self.spans(self.t, title)
        self.assertEqual(len(found), 1, (title, [(s.title, s.start_dt) for s in self.t.day_plan.spans]))
        return found[0].start_dt

    def in_part(self, steps, hhmm):
        """Each step is in the part that starts at ``hhmm``: within its half hour (the steps share it)."""
        for step in steps:
            self.assertTrue(at(hhmm) <= self.start(step) <= at(hhmm) + timedelta(minutes=30),
                            (step, self.start(step), hhmm))

    def said(self, message, tool, args):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None


class TestBuiltLate(ClosingCase):

    def set_up_late(self, fallback="at 10:30 PM if it's late", up="21:20"):
        out = self.t.execute("set_routine", {"routine": "Workshop Closing", "confirm": True, "parts": [
            {"start": "8:10 PM", "items": EARLIER}, {"start": "8:40 PM", "items": LATER}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        if fallback:
            self.t.execute("set_routine", {"routine": "Workshop Closing", "fallback": ["", fallback]})
        set_debug_time(at(up))
        return self.t.assistant.routines.set_up_current()

    def test_the_later_part_comes_next(self):
        summary = self.set_up_late()
        self.in_part(EARLIER, "21:30")
        self.in_part(LATER, "22:00")
        self.assertNotIn("10:30 PM", summary)  # never sent to its fallback by itself

    def test_in_order_when_neither_had_a_fallback(self):
        self.set_up_late(fallback="")
        self.assertLess(self.start("Sweep the bench"), self.start("Check the glue pots"))
        self.assertEqual(self.start("Check the glue pots"), at("22:00"))

    def test_the_fallback_is_shown_to_the_model_with_todays_time(self):
        self.set_up_late()
        section = self.t.assistant.routines.prompt_section()
        self.assertIn(": at 10:30 PM if it's late, 10:30 PM today)", section)

    def test_a_fixed_event_right_after_puts_it_in_the_next_free_thirty(self):
        from thirties_core.models import EventSpan, SpanCategory
        meeting = EventSpan("cal_farrier", "Farrier visit", at("21:30"), at("22:10"), SpanCategory.CALENDAR,
                            source="calendar")
        meeting.is_firm = True
        self.t.day_plan.spans.append(meeting)
        self.set_up_late(up="20:50")
        self.assertEqual(self.start("Check the glue pots"), at("22:30"))  # the first free thirty after the visit


class PlacedCase(ClosingCase):
    """The closing routine on the plan: its first part at 8:10 PM, the later part at 11:20 AM, and an errand."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:20"))
        out = self.t.execute("put_routine", {"routine": "Workshop Closing", "parts": [
            {"start": "8:10 PM", "items": EARLIER}, {"start": "11:20 AM", "items": LATER}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        out = self.t.execute("add_event", {"title": "Oil the gate hinge", "start": "12:30 PM", "duration": "20 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_part(LATER, "11:20")


class TestAPartSaidToBeInTheWrongPlace(PlacedCase):

    def move_later_part(self, message):
        return [self.said(message, "update_event", {"event": s, "start": "8:40 PM"}) for s in LATER]

    def test_naming_the_routine_covers_its_steps(self):
        outs = self.move_later_part("Workshop Closing got split up, some of it landed before lunch.")
        for out in outs:
            self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_part(LATER, "20:40")

    def test_naming_one_step_covers_the_rest_of_its_part(self):
        outs = self.move_later_part("Why are the glue pots at 11:20? Wrong spot entirely.")
        for out in outs:
            self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_part(LATER, "20:40")

    def test_the_rest_of_the_routine(self):
        outs = self.move_later_part("the rest of the routine is off, it carries on once the chisels are done")
        for out in outs:
            self.assertFalse(out.startswith(NOT_DONE), out)

    def test_what_the_words_dont_cover_is_moved_on_the_models_call_and_said(self):
        out = self.said("Why are the glue pots at 11:20? Wrong spot entirely.", "update_event",
                        {"event": "Oil the gate hinge", "start": "1:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("which the user didn't name", out)
        self.assertEqual(self.start("Oil the gate hinge"), at("13:00"))

    def test_a_step_named_only_as_where_it_goes_is_moved_and_said_so(self):
        out = self.said("Glue pots go straight after Sweep the bench.", "update_event",
                        {"event": "Sharpen the chisels", "start": "9:50 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("which the user didn't name", out)

    def test_with_no_words_about_the_plan_nothing_moves(self):
        out = self.said("frosty again, the yard is white", "update_event",
                        {"event": "Check the glue pots", "start": "8:40 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("hasn't asked for anything to move", out)
        self.in_part(["Check the glue pots"], "11:20")


class TestPutRoutineSaysWhatMoved(PlacedCase):

    def put_later_part(self, message):
        return self.said(message, "put_routine", {"routine": "Workshop Closing", "parts": [
            {"start": "8:40 PM", "items": LATER}]})

    def test_every_step_moves_though_the_old_time_was_quoted(self):
        out = self.put_later_part("Label the jars and that lot sitting at 11:20 AM is wrong: they come after the "
                                  "chisels.")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_part(LATER, "20:40")
        self.assertIn("8:30 PM: Check the glue pots, Sort the offcuts, Label the jars, Wind the radio", out)
        self.assertNotIn("11:00 AM", out)
        self.assertNotIn("11:30 AM", out)

    def test_a_step_that_cant_move_stays_and_is_named(self):
        real = PlanTools.add_event

        def add_event(tools, title, *args, **kwargs):
            if title == "Sort the offcuts":
                raise ToolError("that time is taken")
            return real(tools, title, *args, **kwargs)
        with patch.object(PlanTools, "add_event", add_event):
            out = self.put_later_part("the rest of the workshop closing follows the chisels")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.in_part(["Sort the offcuts"], "11:20")  # back where it was, not lost
        self.assertEqual(self.start("Check the glue pots"), at("20:40"))
        self.assertIn("8:30 PM: Check the glue pots, Label the jars, Wind the radio.", out)
        self.assertIn("Not moved: Sort the offcuts stays at", out)
        self.assertIn("(that time is taken)", out)

    def test_nothing_placed_is_refused_and_nothing_is_lost(self):
        def add_event(tools, title, *args, **kwargs):
            raise ToolError("that time is taken")
        with patch.object(PlanTools, "add_event", add_event):
            out = self.put_later_part("the rest of the workshop closing follows the chisels")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.in_part(LATER, "11:20")
        stored = {s.title for s in self.sched.state_db.get_spans_between(at("07:00"), at("23:00"))}
        self.assertTrue(set(LATER) <= stored, stored)


if __name__ == "__main__":
    unittest.main()
