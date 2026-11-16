"""Clear asks to take something off work, and taking off is never finishing.

Seen in simulated days: an old, unmarked item the user asked three times to clear stayed on (its name shared a word
with a later item, and the time the user gave for it wasn't read); an item taken off the schedule stayed on today's
list after two asks, because nothing could take it off the list; and "clear X from the list" ticked X off as done.
On invented items only.
"""

from tests.fixtures import at
from tests.test_today import TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE


class ActionsCase(TodayTestCase):
    def say(self, tool, args, said, shown="", before=""):
        self.t.user_message = said
        self.t.prior_exchange = (before, shown)
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None
            self.t.prior_exchange = None

    def add(self, title, start, minutes=30):
        out = self.t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title]


class TestClearRequestsWork(ActionsCase):
    def setUp(self):
        super().setUp()
        self.add("Repot the cactus", "11:00 AM")
        self.add("Sand the bench", "1:00 PM")
        self.add("Wash the van", "5:00 PM")

    def test_each_plain_way_of_asking(self):
        for said in ("take the cactus off", "drop the cactus", "remove repot the cactus pls",
                     "the cactus isn't happening today", "clear the cactus"):
            with self.subTest(said=said):
                if not self.planned("Repot the cactus"):
                    self.add("Repot the cactus", "11:00 AM")
                out = self.say("remove_event", {"event": "Repot the cactus"}, said)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.planned("Repot the cactus"), [])

    def test_by_pointer_after_a_reply_that_named_it(self):
        out = self.say("remove_event", {"event": "Sand the bench"}, "take that off",
                       shown="Sand the bench is at 1:00 PM.")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_by_pointer_after_a_whole_schedule_when_the_user_had_named_it(self):
        shown = "11:00 AM Repot the cactus\n1:00 PM Sand the bench\n5:00 PM Wash the van"
        out = self.say("remove_event", {"event": "Sand the bench"}, "ugh just take that off",
                       shown=shown, before="the bench thing is still there?")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned("Sand the bench"), [])
        out = self.say("remove_event", {"event": "Wash the van"}, "ugh just take that off",
                       shown=shown, before="the bench thing is still there?")
        self.assertTrue(out.startswith(NOT_DONE), out)  # the pointer was to the bench, not the van

    def test_a_short_form_of_its_name(self):
        self.add("Hardware Store: hinges and glue", "3:00 PM")
        out = self.say("remove_event", {"event": "Hardware Store: hinges and glue"}, "take HS off today")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_another_item_is_still_never_taken(self):
        out = self.say("remove_event", {"event": "Wash the van"}, "drop the cactus")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.planned("Wash the van")), 1)


class TestAStaleItemNamedByItsTime(ActionsCase):
    """Two items share a word; the old unmarked one is named by its time, the later one is about to begin."""

    def setUp(self):
        super().setUp()
        self.add("Atlas report: draft three chart options", "11:00 AM", 120)
        self.add("Atlas report - review call", "4:00 PM", 45)
        set_debug_time(at("15:45"))

    def test_its_time_picks_it(self):
        out = self.say("remove_event", {"event": "Atlas report: draft three chart options"},
                       "the 11am atlas thing is STILL there?? i said clear it. pls remove")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned("Atlas report: draft three chart options"), [])
        self.assertEqual(len(self.planned("Atlas report - review call")), 1)

    def test_the_time_never_takes_the_other_one(self):
        out = self.say("remove_event", {"event": "Atlas report - review call"},
                       "the 11am atlas thing is STILL there?? i said clear it. pls remove")
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestOffTodaysList(ActionsCase):
    def test_an_item_with_no_time_left_comes_off_the_list(self):
        """Taken off the schedule earlier, it stayed on today's list with no time; asked to take it off the list,
        it goes (filed as dropped, never ticked as done)."""
        span = self.add("Fold the towels", "2:00 PM")
        self.assertIn("- [ ] Fold the towels", self.body("n_today"))
        self.say("remove_event", {"event": span.id}, "no towels before the guests leave")
        self.assertIn("- [ ] Fold the towels", self.body("n_today"))  # off the schedule, still on the list
        out = self.say("remove_event", {"event": "Fold the towels"}, "take the towels off the list completely")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Fold the towels", self.body("n_today"))
        self.assertNotIn("Done", " ".join(n.get("title", "") for n in self.api.notes.values()))

    def test_asked_to_take_it_off_it_leaves_the_schedule_and_the_list(self):
        self.add("Fold the towels", "2:00 PM")
        out = self.say("remove_event", {"event": "Fold the towels"}, "remove the towels")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned("Fold the towels"), [])
        self.assertNotIn("Fold the towels", self.body("n_today"))
        from thirties_core.day_lists import pencilled_in
        self.assertNotIn("Fold the towels", pencilled_in(self.t.assistant))

    def test_a_list_item_leaves_today_but_stays_on_its_own_list(self):
        self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": "t_bank"})
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        span = next(s for s in self.t.day_plan.spans if s.task_id == "t_bank")
        out = self.say("remove_event", {"event": span.id}, "drop the bank call today")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))  # its own list is never touched


class TestClearingIsNotFinishing(ActionsCase):
    """Data guard: a clear, remove or drop never ticks anything off as done in Joplin."""

    def setUp(self):
        super().setUp()
        self.add("Tea break", "10:00 AM")
        set_debug_time(at("15:45"))

    def test_clear_from_the_list_is_no_done(self):
        for said in ("pls clear out the old tea break from the list, its confusing",
                     "remove the tea break", "drop tea break", "take the tea break off"):
            with self.subTest(said=said):
                out = self.say("log_outcome", {"event": "Tea break", "status": "done"}, said)
                self.assertTrue(out.startswith(NOT_DONE), out)
                self.assertIn("remove_event", out)
                self.assertNotIn("[x]", self.body("n_today"))
                self.assertEqual(self.planned("Tea break")[0].status.value, "planned")

    def test_a_done_said_outright_still_goes(self):
        out = self.say("log_outcome", {"event": "Tea break", "status": "done"}, "tea break done, clear it off")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_clear_itself_files_nothing_as_done(self):
        out = self.say("remove_event", {"event": "Tea break"}, "pls clear out the old tea break from the list")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("[x] Tea break", self.body("n_today"))
        self.assertFalse(any("Done" in (n.get("title") or "") and "Tea break" in (n.get("body") or "")
                             for n in self.api.notes.values()))
        complete = [u for u in self.api.updates if "[x] Tea break" in str(u)]
        dropped = [n for n in self.api.notes.values() if (n.get("title") or "").startswith("Dropped ")]
        self.assertTrue(not complete or dropped, complete)  # a tick only ever in the Dropped note


if __name__ == "__main__":
    import unittest
    unittest.main()
