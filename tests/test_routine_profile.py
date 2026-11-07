"""Profile → Routines: the rows the page shows and the changes it makes, through the tools' own functions."""

import unittest

from tests.fixtures import DAY
from tests.test_routines import FIRST, SECOND, RoutineCase
from thirties_core import routine_profile as rp
from thirties_core.routines import RoutineBook


class TestRoutineProfile(RoutineCase):

    def r(self):
        return self.t.assistant.routines

    def row(self, title="Morning Routine"):
        return next(x for x in rp.routine_rows(self.r()) if x["title"] == title)

    def test_rows_show_schedule_placement_and_skip(self):
        self.r().set_routine(routine="Morning Routine", parts=[{"start": "9:00 AM", "items": FIRST},
                                                               {"start": "3:00 PM", "items": SECOND}],
                             fallback="after work")
        row = self.row()
        self.assertEqual(row["repeat"]["text"], "every day")
        self.assertEqual([p["time"] for p in row["parts"]], ["9:00 AM", "3:00 PM"])
        self.assertEqual(row["parts"][1]["items"], SECOND)
        self.assertEqual(row["parts"][0]["fallback"], "after work")
        self.assertEqual(row["placement_text"], "Set by you")
        self.assertEqual(rp.summary(row), "Every day · 9:00 AM and 3:00 PM")
        self.r().skip_routine(routine="Morning Routine", how="until", until="Oct 20")
        self.assertEqual(self.row()["skip"], "Skipped until Tue Oct 20")
        self.assertIn("Skipped until", rp.summary(self.row()))
        rp.resume(self.r(), "Morning Routine")
        self.assertEqual(self.row()["skip"], "")
        self.assertEqual(self.row()["placement_source"], "user")

    def test_repeat_choices_go_through_set_routine(self):
        rp.save_repeat(self.r(), "Deep Clean", "days", 12)
        self.assertEqual(self.row("Deep Clean")["repeat"]["text"], "every 12 days")
        rp.save_repeat(self.r(), "Deep Clean", "weekdays", days=["sat", "mon"])
        self.assertEqual(self.row("Deep Clean")["repeat"]["days"], ["mon", "sat"])
        rp.save_repeat(self.r(), "Deep Clean", "asked")
        self.assertEqual(self.row("Deep Clean")["repeat"]["kind"], "asked")
        with self.assertRaises(ValueError):
            rp.repeat_text("weekdays", days=[])

    def test_placement_edits_are_the_users(self):
        rp.save_placement(self.r(), "Morning Routine", [
            {"time": "8:30 AM", "items": FIRST, "fallback": ""},
            {"time": "5:00 PM", "items": ["Throne", "Shower", " "], "fallback": "after work"},
            {"time": "", "items": ["PT"]}])  # an empty part row is dropped
        r = RoutineBook(self.sched.state_db).get("n_routine")
        self.assertEqual([(p["at"], p["fallback"]) for p in r.parts], [("08:30", ""), ("17:00", "after work")])
        self.assertEqual(r.parts[1]["items"], ["Throne", "Shower"])
        self.assertEqual(r.placement_source, "user")

    def test_anchors_add_edit_remove_and_the_defaults_offered_again(self):
        self.assertEqual([o["title"] for o in rp.default_offers(self.r())], ["Lunch", "Dinner"])
        rp.save_anchor(self.r(), "Cats' dinner", "5:00 PM", 15, True)
        rp.save_anchor(self.r(), "Lunch", "12:30 PM", 30, False, days="weekdays")
        rows = {a["title"]: a for a in rp.anchor_rows(self.r())}
        self.assertEqual(rows["Cats' dinner"]["summary"], "5:00 PM · 15 min · every day · fixed")
        self.assertEqual(rows["Lunch"]["days_text"], "weekdays")
        self.assertEqual([o["title"] for o in rp.default_offers(self.r())], ["Dinner"])
        rp.remove_anchor(self.r(), "Lunch")
        self.assertNotIn("Lunch", [a["title"] for a in rp.anchor_rows(self.r())])
        self.assertEqual([o["title"] for o in rp.default_offers(self.r())], ["Lunch", "Dinner"])


if __name__ == "__main__":
    unittest.main()
