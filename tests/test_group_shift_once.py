"""Shifting each step of a routine part by the same amount, one call each, moves the part once.

A move of any step takes its whole part along (routine_parts.py). The model, asked to push a part later, sometimes
shifts each step by the same amount in its own call; each call took the whole part along again, so the part ended up
that amount times the number of steps late. Now a shift of a step its part already took along this turn counts from
where the step was before that, so it lands where the first shift put it, and the call says it's already there.

On invented items only: a bike shop's five-step opening routine.
"""

from datetime import timedelta

from tests.fixtures import at
from tests.test_routines import RoutineCase, TITLES
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

OPEN = ["Unlock the bike cage", "Fill the air tank", "Sort the parts bin", "Light the showroom", "Wipe the counter glass"]


class OpeningCase(RoutineCase):

    def setUp(self):
        super().setUp()
        TITLES["n_open"] = "Shop open"
        self.addCleanup(TITLES.pop, "n_open", None)
        self.api.notes["n_open"] = {"id": "n_open", "body": "".join(f"- [ ] {s}\n" for s in OPEN)}
        self.catalog.set_role("n_open", "routine", "Shop open")
        self.api.notes["n_routine"]["body"] = ""
        self.api.notes["n_clean"]["body"] = ""
        self.t = self.tools_for(self.t.day_plan.target_date)
        set_debug_time(at("07:50"))
        out = self.t.execute("put_routine", {"routine": "Shop open", "parts": [{"start": "9:00 AM", "items": OPEN}]})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        found = [s for s in self.t.day_plan.spans if s.title == title]
        self.assertEqual(len(found), 1, title)
        return found[0]

    def starts(self):
        return {s: self.span(s).start_dt for s in OPEN}

    def calls(self, message, *calls):
        """One turn: the user's message, then the model's calls in order."""
        self.t.user_message = message
        outs = []
        try:
            with model_call():
                for tool, args in calls:
                    outs.append(self.t.execute(tool, args))
        finally:
            self.t.user_message = None
        return outs


class TestAShiftOnEachStep(OpeningCase):

    def test_the_same_shift_on_every_step_moves_the_part_once(self):
        before = self.starts()
        outs = self.calls("delivery truck is stuck: the open-up jobs go two and a half hours on",
                          *[("update_event", {"event": self.span(s).id, "shift": "+150 minutes",
                                              "because": "the open-up jobs go two and a half hours on"}) for s in OPEN])
        for out in outs:
            self.assertFalse(out.startswith(NOT_DONE), out)
        after = self.starts()
        for s in OPEN:
            self.assertEqual(after[s] - before[s], timedelta(minutes=150), (s, outs))
        self.assertIn("already there", outs[1])

    def test_one_shift_on_one_step_still_takes_the_part(self):
        before = self.starts()
        self.calls("open-up jobs three hours later",
                   ("update_event", {"event": self.span(OPEN[2]).id, "shift": "+180 minutes",
                                     "because": "open-up jobs three hours later"}))
        after = self.starts()
        for s in OPEN:
            self.assertEqual(after[s] - before[s], timedelta(minutes=180), s)

    def test_a_second_message_shifts_again(self):
        before = self.starts()
        self.calls("slide the opening jobs to after the stock check", ("update_event", {
            "event": self.span(OPEN[0]).id, "shift": "+150 minutes", "because": "after the stock check"}))
        self.calls("and three more hours, the courier rang", ("update_event", {
            "event": self.span(OPEN[3]).id, "shift": "+180 minutes", "because": "three more hours"}))
        after = self.starts()
        for s in OPEN:
            self.assertEqual(after[s] - before[s], timedelta(minutes=330), s)

    def test_a_bigger_shift_on_a_carried_step_counts_from_where_it_was(self):
        before = self.starts()
        self.calls("opening jobs after the delivery",
                   ("update_event", {"event": self.span(OPEN[0]).id, "shift": "+150 minutes",
                                     "because": "after the delivery"}),
                   ("update_event", {"event": self.span(OPEN[4]).id, "shift": "+240 minutes",
                                     "because": "after the delivery"}))
        after = self.starts()
        self.assertEqual(after[OPEN[4]] - before[OPEN[4]], timedelta(minutes=240))
