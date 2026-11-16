"""Steps with their own time inside one part of a routine (step_times.py, put_routine): a timed step goes at its own
time; the part's other steps stay where the part was placed, in order, and never before the step listed ahead of
them. The learned placement keeps each step's own time (it comes from the note, which is never rewritten).
Invented routines, on a fake Joplin."""

from tests.fixtures import at
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE


class StepTimesCase(RoutineCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("07:10"))
        self.routines = self.t.assistant.routines

    def use(self, *steps):
        self.api.notes["n_routine"]["body"] = "".join(f"- [ ] {s}\n" for s in steps)

    def start_of(self, title):
        spans = self.spans(self.t, title)
        self.assertEqual(len(spans), 1, [s.title for s in self.t.day_plan.spans])
        return spans[0].start_dt

    def put(self, parts):
        out = self.t.execute("put_routine", {"routine": "Morning Routine", "parts": parts})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out


class TestATimedFirstStep(StepTimesCase):

    def test_it_doesnt_pull_the_rest_of_the_part_away_from_where_it_was_placed(self):
        self.use("7:15 Water the plants", "Make coffee")
        self.put([{"start": "8:00 AM", "items": ["7:15 Water the plants", "Make coffee"]}])
        self.assertEqual(self.start_of("Water the plants"), at("07:15"))  # its own time
        self.assertEqual(self.start_of("Make coffee"), at("08:00"))  # where the part was placed

    def test_right_before_the_part_the_next_step_still_starts_the_part(self):
        self.use("7:45 Water the plants", "Make coffee")
        self.put([{"start": "8:00 AM", "items": ["7:45 Water the plants", "Make coffee"]}])
        self.assertEqual(self.start_of("Water the plants"), at("07:45"))
        self.assertEqual(self.start_of("Make coffee"), at("08:00"))


class TestALaterTimedStep(StepTimesCase):

    def test_an_untimed_step_after_it_never_lands_before_it(self):
        self.use("Make coffee", "8:30 Stretch", "Journal")
        self.put([{"start": "8:00 AM", "items": ["Make coffee", "8:30 Stretch", "Journal"]}])
        coffee, stretch, journal = (self.start_of(t) for t in ("Make coffee", "Stretch", "Journal"))
        self.assertEqual(coffee, at("08:00"))
        self.assertEqual(stretch, at("08:30"))
        self.assertGreaterEqual(journal, stretch)  # listed after it: never ahead of it
        stretch_span = self.spans(self.t, "Stretch")[0]
        self.assertGreaterEqual(journal, stretch_span.end_dt)

    def test_the_learned_placement_keeps_its_own_time(self):
        self.use("Make coffee", "8:30 Stretch", "Journal")
        self.put([{"start": "8:00 AM", "items": ["Make coffee", "8:30 Stretch", "Journal"]}])
        learned = [{"start": p["at"], "items": p["items"]} for p in self.routines.book().get("n_routine").parts]
        for s in list(self.t.day_plan.spans):  # a fresh day: placed again from what was learned
            if not s.is_envelope:
                self.t.day_plan.spans.remove(s)
        from datetime import datetime
        self.put([{"start": datetime.strptime(p["start"], "%H:%M").strftime("%-I:%M %p"), "items": p["items"]}
                  for p in learned])
        self.assertEqual(self.start_of("Stretch"), at("08:30"))


if __name__ == "__main__":
    import unittest
    unittest.main()
