"""Days off, exclusive items, items that rely on others, items someone else times,
leave-by and follow-up items, and lighter fare on a low-energy day. Invented items throughout."""

import unittest
from datetime import datetime, timedelta

from tests.fixtures import DAY, at, event, make_scheduler
from tests.test_make_way import add, span, times, tools_for
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools


def works(plan) -> bool:
    return any(s.category == SpanCategory.WORK for s in plan.spans)


class Case(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_debug_time, None)


class DaysOff(Case):
    """A week off: no Work those days, leaving the whole day free."""

    def test_off_this_week(self):
        t = tools_for(now="09:00", work=True)  # Thursday
        self.assertTrue(works(t.day_plan))
        t.user_message = "I'm off the rest of this week"
        out = t.execute("set_days_off", {"through": "this week"})
        self.assertIn("no Work on those days", out)
        self.assertFalse(works(t.day_plan))  # off today's plan at once
        self.assertFalse(works(t.plan_for("Friday")))
        self.assertFalse(works(t.plan_for("Saturday")))
        self.assertTrue(works(t.plan_for("next Monday")))
        self.assertIn("Days off: today through Sunday", t.execute("set_days_off", {}))

    def test_back_on_a_day_and_back_early(self):
        t = tools_for(now="09:00", work=True)
        t.execute("set_days_off", {"start": "tomorrow", "back": "next Wednesday"})
        self.assertTrue(works(t.day_plan))  # today is a work day still
        self.assertFalse(works(t.plan_for("next Monday")))
        self.assertFalse(works(t.plan_for("next Tuesday")))
        self.assertTrue(works(t.plan_for("next Wednesday")))
        out = t.execute("set_days_off", {"start": "next Monday", "back_to_work": True})
        self.assertIn("Back at work from", out)
        self.assertTrue(works(t.plan_for("next Monday")))  # Work is on again
        self.assertFalse(works(t.plan_for("Friday")))  # still off before then

    def test_work_put_on_by_hand_stays(self):
        t = tools_for(now="09:00", work=True)
        t.execute("set_days_off", {"through": "Friday"})
        t.user_message = "I'm working 1 to 3 today after all"
        t.execute("add_event", {"title": "Work", "start": "1:00 PM", "end": "3:00 PM", "category": "work"})
        plan, _ = t.scheduler.build_day_plan(DAY)
        self.assertTrue(works(plan))

    def test_days_off_sync_and_work_days(self):
        from thirties_core.days_off import is_off, works_on
        t = tools_for(now="09:00", work=True)
        t.execute("set_days_off", {"through": "Friday", "why": "vacation"})
        db = t.scheduler.state_db
        other = make_scheduler()
        for record in db.get_shared_setting_records():
            other.state_db.merge_shared_setting(record)
        self.assertIsNotNone(is_off(other.state_db, DAY + timedelta(days=1)))
        self.assertFalse(works_on(t.scheduler.config.general, db, DAY))
        self.assertTrue(works_on(t.scheduler.config.general, db, DAY + timedelta(days=4)))  # Monday


class Exclusive(Case):
    """An exclusive item takes up its time: nothing overlaps it."""

    def test_nothing_goes_on_top_of_it(self):
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Concert", "exclusive": True})
        add(t, "Concert", "7:00 PM", 120)
        out = t.execute("add_event", {"title": "Dishes", "start": "8:00 PM", "duration": "30 min"})
        self.assertTrue(out.startswith(NOT_DONE) and "takes up its time" in out, out)
        add(t, "Dishes", "9:00 PM", 30)
        out = t.execute("update_event", {"event": "Dishes", "start": "8:30 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        add(t, "Podcast", "3:00 PM", 60)
        add(t, "Laundry", "3:00 PM", 30)  # ordinary items still share time
        self.assertEqual(times(t, "Laundry"), ("3:00 PM", "3:30 PM"))

    def test_it_doesnt_go_over_anything_either(self):
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Concert", "exclusive": True})
        add(t, "Dishes", "8:00 PM", 30)
        out = t.execute("add_event", {"title": "Concert", "start": "7:00 PM", "duration": "2 hours"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Dishes", out)

    def test_pushing_goes_round_it(self):
        t = tools_for(now="18:05")
        t.execute("set_item_traits", {"event": "Concert", "exclusive": True})
        add(t, "Concert", "7:00 PM", 60)
        add(t, "Dishes", "6:00 PM", 60)
        t.execute("push_back", {"by": "30 min"})
        concert, dishes = span(t, "Concert"), span(t, "Dishes")
        self.assertEqual(times(t, "Dishes"), ("6:30 PM", "7:30 PM"))
        self.assertFalse(concert.overlaps(dishes.start_dt, dishes.end_dt))  # it moved along rather than share
        self.assertEqual(times(t, "Concert"), ("7:30 PM", "8:30 PM"))


class ReliesOnOthers(Case):
    """The cats' dinner, fed by the sitter, never passes as done or unknown just because time passed."""

    def test_never_settled_by_the_clock(self):
        t = tools_for(now="09:00")
        add(t, "Feed the cats", "5:00 PM", 15, fixed=True)
        add(t, "Water the ferns", "5:30 PM", 15, fixed=True)
        t.execute("set_item_traits", {"event": "Feed the cats", "relies_on": "the sitter"})
        set_debug_time(at("20:00"))  # three hours on: a fixed item has gone moot
        t.assistant.settle_moot()
        self.assertEqual(span(t, "Feed the cats").status, SpanStatus.PLANNED)  # still open: someone has to say
        self.assertEqual(span(t, "Water the ferns").status, SpanStatus.UNKNOWN)  # the ordinary one goes quiet
        from thirties_core.item_facts import lasting_lines
        self.assertTrue(any("depends on the sitter" in line for line in lasting_lines(t._lasting())))

    def test_clearing_it(self):
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Feed the cats", "relies_on": "others"})
        t.execute("set_item_traits", {"event": "Feed the cats", "relies_on": "none"})
        self.assertEqual(t._lasting().for_item("Feed the cats").relies_on, "")


class SomeoneElseTimesIt(Case):
    """What someone else sets the day and time of isn't scheduled by Thirties."""

    def test_not_proposed_or_replanned(self):
        from thirties_core.replan import replan
        t = tools_for(now="09:00")
        out = t.execute("set_item_traits", {"event": "Guitar lesson", "time_set_by": "Sam"})
        self.assertIn("Thirties won't schedule it", out)
        out = t.execute("propose_day", {"items": [{"item": "Guitar lesson", "start": "4:30 PM"}]})
        self.assertTrue(out.startswith(NOT_DONE) and "Sam sets its day and time" in out, out)
        placed = replan(t.assistant, "Guitar lesson", "once", 60, DAY)
        self.assertIsNone(placed.day)
        self.assertIn("Sam sets", placed.why)
        add(t, "Guitar lesson", "3:00 PM", 60)  # the user gives the time Sam set: it goes on
        self.assertEqual(times(t, "Guitar lesson"), ("3:00 PM", "4:00 PM"))


class TraitsSync(Case):
    def test_traits_travel_with_the_other_lasting_facts(self):
        from thirties_core.item_facts import LastingFacts
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Concert", "exclusive": True, "travel": "40 minutes"})
        other = make_scheduler()
        theirs = LastingFacts(other.state_db)
        for record in LastingFacts(t.scheduler.state_db).all_records():
            theirs.merge_record(record)
        got = theirs.get("Concert")
        self.assertTrue(got.exclusive)
        self.assertEqual(got.travel, 40)
        old = {"name": "dinner", "title": "Dinner", "minutes": 60, "updated_at": "2026-10-01T00:00:00+00:00"}
        theirs.merge_record(old)  # a record from a device without the new facts
        self.assertEqual((theirs.get("Dinner").minutes, theirs.get("Dinner").exclusive), (60, False))


class LeaveByAndFollowUps(Case):
    """A time to leave for something away from home, and a follow-up item tied to the item before it."""

    def test_leave_by_a_time(self):
        t = tools_for([event("show", "14:30", "16:30", "Matinee show")], now="09:00")
        add(t, "Fold laundry", "2:00 PM", 30)
        out = t.execute("leave_by", {"event": "Matinee show", "at": "1:45 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(times(t, "Leave for Matinee show"), ("1:45 PM", "2:30 PM"))
        self.assertTrue(t._is_fixed(span(t, "Leave for Matinee show")))
        self.assertEqual(times(t, "Fold laundry"), ("4:30 PM", "5:00 PM"))  # not during the drive, or the show

    def test_a_travel_time_on_record_plans_it_by_itself_once(self):
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Pottery class", "travel": "30 minutes"})
        add(t, "Pottery class", "6:00 PM", 90, fixed=True)
        t.assistant.settle_moot()
        self.assertEqual(times(t, "Leave for Pottery class"), ("5:30 PM", "6:00 PM"))
        t.execute("remove_event", {"event": "Leave for Pottery class"})
        t.assistant.settle_moot()
        self.assertFalse(any(s.title == "Leave for Pottery class" for s in t.day_plan.spans))  # off is off

    def test_follow_up(self):
        t = tools_for(now="09:00")
        add(t, "Charge the watch", "10:00 AM", 60)
        out = t.execute("follow_up", {"event": "Charge the watch", "title": "Put the watch back on"})
        self.assertIn("Added Put the watch back on at 11:00 AM", out)
        follow = span(t, "Put the watch back on")
        self.assertEqual(follow.duration_minutes, 5)
        self.assertIn(span(t, "Charge the watch").id, t._facts().get(follow.id).after)  # it stays after it

    def test_a_follow_up_that_always_comes(self):
        t = tools_for(now="09:00")
        t.execute("set_item_traits", {"event": "Charge the watch", "follow_up": "Put the watch back on",
                                      "follow_after": "15 minutes"})
        add(t, "Charge the watch", "10:00 AM", 60)
        t.assistant.settle_moot()
        t.assistant.settle_moot()
        self.assertEqual(times(t, "Put the watch back on"), ("11:15 AM", "11:20 AM"))
        self.assertEqual(sum(s.title == "Put the watch back on" for s in t.day_plan.spans), 1)


class LowEnergy(Case):
    """On a day the user says their energy is low, the proposal is lighter."""

    def setUp(self):
        super().setUp()
        from tests.test_routines import item
        from thirties_core.lists import ListCatalog
        set_debug_time(at("09:00"))
        self.t = tools_for(now="09:00")
        catalog = ListCatalog(self.t.scheduler.state_db)
        self.t.assistant.s.tasks += [item("t_letter", "Post the letter"), item("t_attic", "Clear the attic"),
                                     item("t_call", "Call the vet"), item("t_bills", "Pay the bills")]
        for tid in ("t_letter", "t_attic", "t_call", "t_bills"):
            catalog.set_urgency(tid, "soon")
        self.t.assistant.s.tasks = catalog.apply(self.t.assistant.s.tasks)
        from thirties_core.item_facts import Lasting
        self.t._lasting().set(Lasting(name="clear the attic", title="Clear the attic", minutes=180))

    def test_a_low_day_is_lighter(self):
        normal = self.t.assistant.proposals.default_items()
        self.assertEqual(len(normal), 3)
        self.assertIn("Clear the attic", [i["title"] for i in normal])
        self.t.execute("log_energy", {"level": 2})
        light = self.t.assistant.proposals.default_items()
        self.assertEqual(len(light), 2)  # fewer
        self.assertNotIn("Clear the attic", [i["title"] for i in light])  # nothing heavy
        first, second = sorted(datetime.fromisoformat(i["start"]) for i in light)
        self.assertGreaterEqual(second - first, timedelta(minutes=90))  # more room between: half an hour, then an hour
        self.assertIn("Energy is low today (2/5): lighter fare", self.t.execute("day_options", {}))


if __name__ == "__main__":
    unittest.main()
