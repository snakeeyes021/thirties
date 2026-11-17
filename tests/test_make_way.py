"""The day making way: still doing it, auto-done after a stated length, smart pushing,
meals spaced from when they were really eaten, breathing room and the day replanned from work.
Invented items throughout."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.clock import format_clock
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools


def tools_for(events=None, now="08:00", work=False) -> PlanTools:
    set_debug_time(at(now))
    sched = make_scheduler(events)
    plan, pending = sched.build_day_plan(DAY)
    t = PlanTools(plan, pending, sched)
    if not work:
        t.execute("remove_event", {"event": "Work"})
    return t


def span(t, title):
    return next(s for s in t.day_plan.spans if s.title == title)


def times(t, title):
    s = span(t, title)
    return format_clock(s.start_dt), format_clock(s.end_dt)


def add(t, title, start, minutes, **kw):
    out = t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min", **kw})
    assert not out.startswith(NOT_DONE), out
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_debug_time, None)


class StillDoingIt(Case):
    """Still in the shower: in progress, the item stretches, and the rest moves along only as far as it has to."""

    def morning(self, now="08:25"):
        t = tools_for(now="07:30")
        add(t, "Shower", "8:00 AM", 30)
        add(t, "Journal", "8:30 AM", 30)
        add(t, "Errands", "9:30 AM", 30)
        add(t, "Breakfast", "10:00 AM", 30)
        add(t, "Laundry", "1:00 PM", 60)
        set_debug_time(at(now))
        return t

    def test_still_doing_stretches_it_and_moves_what_follows(self):
        t = self.morning()
        out = t.execute("still_doing", {"event": "Shower"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        shower = span(t, "Shower")
        self.assertEqual(times(t, "Shower"), ("8:00 AM", "9:00 AM"))  # five minutes left: into the next thirty
        self.assertEqual(shower.status, SpanStatus.PLANNED)  # in progress is not done
        self.assertEqual((shower.time_source, shower.actual_start, shower.planned_end), ("running", at("08:00"),
                                                                                         at("08:30")))
        self.assertEqual(times(t, "Journal"), ("9:00 AM", "9:30 AM"))  # right after it
        self.assertEqual(times(t, "Breakfast"), ("10:00 AM", "10:30 AM"))  # a meal stays where it is
        self.assertEqual(times(t, "Errands"), ("10:30 AM", "11:00 AM"))  # it kept its room, then went round breakfast
        self.assertEqual(times(t, "Laundry"), ("1:00 PM", "2:00 PM"))  # far enough off: nothing needed to move it
        self.assertIn("still going, until about 9:00 AM (in progress, not done)", out)
        self.assertIn("Breakfast stays where it was", out)
        self.assertIn("There was room before Laundry", out)

    def test_with_time_left_nothing_moves(self):
        t = self.morning(now="08:05")
        t.execute("still_doing", {"event": "Shower"})
        self.assertEqual(times(t, "Shower"), ("8:00 AM", "8:30 AM"))
        self.assertEqual(span(t, "Shower").time_source, "running")
        self.assertEqual(times(t, "Journal"), ("8:30 AM", "9:00 AM"))

    def test_until_a_time_and_undo(self):
        t = self.morning()
        t.execute("still_doing", {"event": "Shower", "until": "8:45 AM"})
        self.assertEqual(times(t, "Shower"), ("8:00 AM", "8:45 AM"))
        self.assertEqual(times(t, "Journal"), ("9:00 AM", "9:30 AM"))  # on a block
        t.execute("undo_redraw", {})
        self.assertEqual(times(t, "Shower"), ("8:00 AM", "8:30 AM"))
        self.assertEqual(times(t, "Journal"), ("8:30 AM", "9:00 AM"))

    def test_a_meal_it_covers_waits_for_it_inside_its_window(self):
        t = tools_for(now="07:30")
        add(t, "Garden", "8:00 AM", 60)
        add(t, "Breakfast", "9:00 AM", 30)
        set_debug_time(at("08:55"))
        t.execute("still_doing", {"event": "Garden", "until": "9:45 AM"})
        self.assertEqual(times(t, "Breakfast"), ("10:00 AM", "10:30 AM"))  # after it, and still inside its window

    def test_while_it_goes_on_the_clock_stretches_it_and_never_ends_it(self):
        t = self.morning(now="07:30")
        add(t, "Sketching", "11:00 AM", 60)
        set_debug_time(at("11:50"))
        t.execute("still_doing", {"event": "Sketching"})
        self.assertEqual(times(t, "Sketching"), ("11:00 AM", "12:30 PM"))
        set_debug_time(at("12:40"))
        t.assistant.settle_moot()  # each check (the background service, each turn)
        self.assertEqual(times(t, "Sketching"), ("11:00 AM", "1:00 PM"))
        self.assertEqual(times(t, "Laundry"), ("1:30 PM", "2:30 PM"))  # it keeps half an hour of the hour it had
        set_debug_time(at("13:10"))
        t.assistant.settle_moot()
        self.assertEqual(times(t, "Sketching"), ("11:00 AM", "1:00 PM"))  # twice its length: the user's to say now
        self.assertEqual(span(t, "Sketching").status, SpanStatus.PLANNED)  # never done, never unknown, by the clock
        out = t.execute("log_outcome", {"event": "Sketching", "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotEqual(span(t, "Sketching").time_source, "running")

    def test_doing_it_now_before_its_time(self):
        t = self.morning(now="07:30")
        set_debug_time(at("10:10"))
        out = t.execute("still_doing", {"event": "Laundry"})
        self.assertIn("Laundry is going now", out)
        self.assertEqual(times(t, "Laundry"), ("10:10 AM", "11:10 AM"))  # the time they said: now
        self.assertEqual(span(t, "Laundry").time_source, "running")

    def test_only_things_on_the_plan_that_are_still_open(self):
        t = self.morning()
        t.execute("log_outcome", {"event": "Shower", "status": "done"})
        self.assertTrue(t.execute("still_doing", {"event": "Shower"}).startswith(NOT_DONE))
        self.assertTrue(t.execute("still_doing", {"event": "Sleep"}).startswith(NOT_DONE))


class DoneByItself(Case):
    """"You can mark it done automatically in 3 hours"."""

    def test_marked_done_at_the_time_given(self):
        t = tools_for(now="09:00")
        add(t, "Bake bread", "9:00 AM", 60)
        out = t.execute("still_doing", {"event": "Bake bread", "length": "3 hours", "auto_done": True})
        self.assertIn("marked done by itself then", out)
        self.assertEqual(times(t, "Bake bread"), ("9:00 AM", "12:00 PM"))
        set_debug_time(at("11:30"))
        t.assistant.settle_moot()
        self.assertEqual(span(t, "Bake bread").status, SpanStatus.PLANNED)
        set_debug_time(at("12:05"))
        t.assistant.settle_moot()
        bread = span(t, "Bake bread")
        self.assertEqual(bread.status, SpanStatus.DONE)
        self.assertEqual((bread.actual_start, bread.actual_end), (at("09:00"), at("12:00")))
        self.assertEqual(bread.time_source, "reported")

    def test_through_log_outcome_once(self):
        """A list item done by itself is recorded the one way every done is (so its list line is ticked once), and
        isn't counted as an outcome the model logged."""
        t = tools_for(now="09:00")
        add(t, "Bake bread", "9:00 AM", 60)
        t.execute("still_doing", {"event": "Bake bread", "until": "10:00 AM", "auto_done": True})
        calls = []
        real = t.assistant.log_outcome

        def spy(*args, **kw):
            calls.append(args)
            return real(*args, **kw)
        t.assistant.log_outcome = spy
        logged = t.outcomes_logged
        set_debug_time(at("10:30"))
        t.assistant.settle_moot()
        t.assistant.settle_moot()
        self.assertEqual(len(calls), 1)
        self.assertEqual(t.outcomes_logged, logged)

    def test_needs_a_time(self):
        t = tools_for(now="09:00")
        add(t, "Bake bread", "9:00 AM", 60)
        self.assertTrue(t.execute("still_doing", {"event": "Bake bread", "auto_done": True}).startswith(NOT_DONE))


class SmartPush(Case):
    """Meals stay roughly in place, fixed events and what's inside them never move, only what has to moves."""

    def test_lunch_stays_and_the_rest_goes_round_it(self):
        t = tools_for(now="10:05")
        add(t, "Sort the mail", "10:00 AM", 60)
        add(t, "Repot the fern", "11:00 AM", 60)
        add(t, "Lunch", "12:00 PM", 30)
        out = t.execute("push_back", {"by": "1 hour"})
        self.assertEqual(times(t, "Lunch"), ("12:00 PM", "12:30 PM"))  # not slid the whole hour
        self.assertEqual(times(t, "Sort the mail"), ("11:00 AM", "12:00 PM"))
        self.assertEqual(times(t, "Repot the fern"), ("12:30 PM", "1:30 PM"))  # right after lunch
        self.assertIn("Lunch stays where it was; the rest goes around it.", out)

    def test_whats_inside_a_fixed_event_stays_with_it(self):
        t = tools_for([event("lesson", "14:00", "16:00", "Music lesson")], now="13:05")
        add(t, "Tune the guitar", "2:00 PM", 30, alongside=True)
        add(t, "Stretch", "1:00 PM", 30)
        add(t, "Groceries", "4:00 PM", 60)
        t.execute("push_back", {"by": "1 hour"})
        self.assertEqual(times(t, "Tune the guitar"), ("2:00 PM", "2:30 PM"))  # part of the lesson: it stays
        self.assertEqual(times(t, "Music lesson"), ("2:00 PM", "4:00 PM"))
        self.assertEqual(times(t, "Stretch"), ("4:00 PM", "4:30 PM"))  # landed on the lesson: right after it
        self.assertEqual(times(t, "Groceries"), ("5:00 PM", "6:00 PM"))  # keeps half an hour of its room

    def test_a_gap_absorbs_the_push_and_lengths_are_kept(self):
        t = tools_for(now="10:05")
        add(t, "Sort the mail", "10:00 AM", 60)
        add(t, "Call the plumber", "3:00 PM", 30)
        add(t, "Dinner", "6:00 PM", 60)
        t.execute("push_back", {"by": "2 hours"})
        self.assertEqual(times(t, "Sort the mail"), ("12:00 PM", "1:00 PM"))
        self.assertEqual(times(t, "Call the plumber"), ("3:00 PM", "3:30 PM"))  # nothing needed to move it
        self.assertEqual(times(t, "Dinner"), ("6:00 PM", "7:00 PM"))

    def test_someone_elses_time_never_moves(self):
        t = tools_for(now="10:05")
        add(t, "Sort the mail", "10:00 AM", 60)
        add(t, "Piano lesson", "11:00 AM", 60)
        t.execute("set_item_traits", {"event": "Piano lesson", "time_set_by": "the teacher"})
        t.execute("push_back", {"by": "1 hour"})
        self.assertEqual(times(t, "Piano lesson"), ("11:00 AM", "12:00 PM"))
        self.assertEqual(times(t, "Sort the mail"), ("12:00 PM", "1:00 PM"))  # jumps it


class MealSpacing(Case):
    """The next meal is spaced from when the last one was really eaten."""

    def test_a_late_breakfast_moves_lunch_later_once(self):
        t = tools_for(now="07:30")
        add(t, "Breakfast", "8:00 AM", 30)
        add(t, "Lunch", "12:00 PM", 30)
        set_debug_time(at("11:05"))
        out = t.execute("log_outcome", {"event": "Breakfast", "status": "done", "started": "10:30 AM",
                                        "finished": "11:00 AM"})
        self.assertEqual(times(t, "Lunch"), ("1:30 PM", "2:00 PM"))  # inside its window, not twelve minutes on
        self.assertIn("Lunch moved to 1:30 PM", out)
        t.user_message = "actually put lunch at 12:30"
        t.execute("update_event", {"event": "Lunch", "start": "12:30 PM"})
        t.assistant.settle_moot()
        self.assertEqual(times(t, "Lunch"), ("12:30 PM", "1:00 PM"))  # the user's time: not moved again

    def test_still_eating_breakfast(self):
        t = tools_for(now="07:30")
        add(t, "Breakfast", "10:00 AM", 30)
        add(t, "Lunch", "11:30 AM", 30)
        set_debug_time(at("10:25"))
        t.execute("still_doing", {"event": "Breakfast"})
        self.assertEqual(times(t, "Lunch"), ("1:30 PM", "2:00 PM"))


class BreathingRoom(Case):
    """Thirties doesn't pack the day: half an hour clear after work, and a free thirty after a long run."""

    def test_slot_options_leave_room_after_work(self):
        t = tools_for(now="12:00", work=True)
        out = t.execute("slot_options", {"duration": "30 min"})
        self.assertNotIn("4:00 PM – 4:30 PM", out)  # work ends at 4:00
        self.assertIn("4:30 PM", out)
        self.assertIn("4:00 PM", t.execute("find_open_time", {"duration": "30 min"}))  # free time is still told as is

    def test_a_redraw_leaves_a_free_thirty_after_a_long_run(self):
        t = tools_for(now="07:30")
        for title, start in (("Taxes", "8:00 AM"), ("Filing", "9:00 AM"), ("Emails", "10:00 AM"), ("Ironing", "11:00 AM")):
            add(t, title, start, 60)
        set_debug_time(at("12:10"))  # none of it marked: laid out again from now
        t.execute("redraw", {})
        starts = sorted(span(t, x).start_dt for x in ("Taxes", "Filing", "Emails", "Ironing"))
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertIn(timedelta(minutes=90), gaps)  # one hour on, then a free thirty

    def test_a_redraw_starts_after_work_with_room(self):
        t = tools_for(now="13:40", work=True)
        add(t, "Taxes", "2:00 PM", 60, alongside=True)
        set_debug_time(at("15:45"))
        t.execute("redraw", {})
        self.assertEqual(times(t, "Taxes"), ("4:30 PM", "5:30 PM"))


class WorkReplans(Case):
    """Saying when work ended replans the rest of the day."""

    def test_work_ran_long_and_the_evening_moves_along(self):
        t = tools_for(now="12:00", work=True)
        add(t, "Groceries", "4:00 PM", 60)
        add(t, "Dinner", "6:00 PM", 60)
        add(t, "Read", "8:00 PM", 60)
        set_debug_time(at("17:10"))
        t.user_message = "finished work at 5"
        out = t.execute("log_day_times", {"work_ended": "5:00 PM"})
        # half an hour clear after work, then dinner stays where it is and groceries goes after it
        self.assertEqual(times(t, "Dinner"), ("6:00 PM", "7:00 PM"))
        self.assertEqual(times(t, "Groceries"), ("7:00 PM", "8:00 PM"))
        self.assertEqual(times(t, "Read"), ("8:30 PM", "9:30 PM"))
        self.assertIn("Moved along after work", out)

    def test_work_ended_early_says_whats_free(self):
        t = tools_for(now="12:00", work=True)
        add(t, "Dinner", "6:00 PM", 60)
        set_debug_time(at("15:05"))
        t.user_message = "done with work at 3"
        out = t.execute("log_day_times", {"work_ended": "3:00 PM"})
        self.assertIn("Free from 3:00 PM", out)
        self.assertEqual(times(t, "Dinner"), ("6:00 PM", "7:00 PM"))


if __name__ == "__main__":
    unittest.main()


from tests import test_today  # noqa: E402


class DoneByItselfKeepsJoplinTrue(test_today.TodayTestCase):
    """Guard: something done by itself is written the way any done is: a list item is ticked once in its
    list, and a routine step only on Today, never in the routine note (a template)."""

    def setUp(self):
        super().setUp()
        from thirties_core.lists import ListCatalog
        self.routine = "".join(f"- [ ] {s}\n" for s in ("Feed the fish", "Make coffee", "Shower"))
        self.api.notes["n_routine"] = {"id": "n_routine", "body": self.routine}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Studio opening")
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "done")

    def test_auto_done_list_item_is_ticked_once(self):
        set_debug_time(at("10:00"))
        self.t.execute("add_event", {"title": "Call the bank", "start": "10:00 AM", "duration": "30 min",
                                     "task_id": "t_bank"})
        self.t.execute("still_doing", {"event": "Call the bank", "until": "11:00 AM", "auto_done": True})
        set_debug_time(at("11:05"))
        self.t.assistant.settle_moot()
        self.t.assistant.settle_moot()
        self.assertEqual(self.body("n_errands").count("[x] Call the bank"), 1)
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))  # nothing else touched

    def test_auto_done_routine_step_never_writes_the_routine(self):
        template = self.body("n_routine")
        self.t.execute("put_routine", {"routine": "Studio opening", "start": "10:00 AM"})
        coffee = next(s for s in self.t.day_plan.spans if s.title == "Make coffee")
        set_debug_time(at("10:05"))
        self.t.execute("still_doing", {"event": coffee.id, "length": "20 minutes", "auto_done": True})
        set_debug_time(at("10:40"))
        self.t.assistant.settle_moot()
        self.assertEqual(coffee.status, SpanStatus.DONE)
        self.assertEqual(self.body("n_routine"), template)
        self.assertIn("    - [x] Make coffee\n", self.body("n_today"))
