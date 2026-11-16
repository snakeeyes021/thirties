"""Times the user states are shown as said; times Thirties picks are shown by their blocks.

A schedule is shown in blocks (AGENTS.md §7, "Natural units"). Under the hood items keep minute times, to pack a
thirty without overstuffing it. A time the user named for their own item as they placed it ("drop off the parcel
at 4:45") is theirs: the item keeps it (given_start), stores and syncs it, and the views show it to the minute.
A start Thirties chose (a routine's steps, a placing started by another tool, a change with nobody talking) never
counts as the user's.
"""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_routines import RoutineCase
from thirties_core.models import EventSpan
from thirties_core.plan_tools import NOT_DONE
from thirties_core.shown_times import end_exact, kept_times, span_when, start_exact

RESET_STEPS = ["Rinse the teapot", "Fill the bird feeder", "Set out tomorrow's socks", "Charge the radio",
               "Wind the clock", "Stack the mail", "Close the blinds", "Water the cactus"]


class GivenCase(RoutineCase):

    def add(self, args, message):
        self.t.user_message = message
        out = self.t.execute("add_event", args)
        self.assertFalse(out.startswith(NOT_DONE), out)
        return next(s for s in self.t.day_plan.spans if s.title == args["title"])

    def add_reset_routine(self):
        self.api.notes["n_reset"] = {"id": "n_reset", "title": "Evening Reset",
                                     "body": "".join(f"- [ ] {s}\n" for s in RESET_STEPS)}
        self.catalog.set_role("n_reset", "routine", "Evening Reset")


class TestATimeTheUserStates(GivenCase):

    def test_a_stated_start_is_kept_and_shown_to_the_minute(self):
        span = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"},
                        "drop off the parcel at 4:45 please")
        self.assertEqual(span.given_start, at("16:45"))
        self.assertTrue(start_exact(span))
        self.assertEqual(span_when(span), "4:45 PM – 5:00 PM")
        from thirties_core.messages import schedule_rows
        rows = "\n".join(schedule_rows(self.t.day_plan))
        self.assertIn("Drop off the parcel at 4:45 PM", rows)  # its block's row, with the time the user gave
        self.assertIn(at("16:45"), kept_times(span))  # a reply may say it as said (clock.real_times)

    def test_a_flexible_item_at_a_stated_time_keeps_it_too(self):
        span = self.add({"title": "Water the ferns", "start": "4:45 PM", "duration": "30 min"},
                        "water the ferns at 4:45")
        self.assertEqual((span.start_dt, span.given_start), (at("16:45"), at("16:45")))
        self.assertEqual(span_when(span), "4:45 PM – 5:30 PM")  # its length is Thirties' own: its end is a block

    def test_a_stated_start_and_end_are_both_shown(self):
        span = self.add({"title": "Repaint the gate", "start": "3:10 PM", "end": "3:50 PM"},
                        "repaint the gate from 3:10 to 3:50")
        self.assertTrue(start_exact(span) and end_exact(span))
        self.assertEqual(span_when(span), "3:10 PM – 3:50 PM")

    def test_it_is_stored_and_synced(self):
        span = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"},
                        "drop off the parcel at 4:45")
        stored = self.sched.state_db.get_span(span.id)
        self.assertEqual(stored.given_start, at("16:45"))
        self.assertEqual(EventSpan.from_dict(span.to_dict()).given_start, at("16:45"))
        other = make_scheduler()
        other.build_day_plan(DAY)
        for record in self.sched.state_db.get_span_records():
            other.state_db.merge_span_record(record)
        there = other.state_db.get_span(span.id)
        self.assertEqual((there.given_start, span_when(there)), (at("16:45"), "4:45 PM – 5:00 PM"))

    def test_a_move_the_user_states_is_kept_and_a_move_thirties_makes_is_not(self):
        span = self.add({"title": "Water the ferns", "start": "2:00 PM", "duration": "15 min"}, "water the ferns at 2")
        self.t.user_message = "move the ferns to 5:10"
        self.t.execute("update_event", {"event": span.id, "start": "5:10 PM"})
        self.assertEqual((span.start_dt, span.given_start), (at("17:10"), at("17:10")))
        self.assertEqual(span_when(span), "5:10 PM – 5:30 PM")
        self.t.user_message = None  # moved with nobody talking: the time is no longer one the user gave
        out = self.t.execute("update_event", {"event": span.id, "shift": "+40 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertEqual((span.start_dt, span.given_start), (at("17:50"), None))
        self.assertEqual(span_when(span), "5:30 PM – 6:30 PM")

    def test_a_reported_actual_is_shown_as_said(self):
        span = self.add({"title": "Water the ferns", "start": "7:30 AM", "duration": "30 min"}, "water the ferns first")
        self.t.user_message = "I watered the ferns 7:40 to 7:55"
        out = self.t.execute("log_outcome", {"event": span.id, "status": "done", "started": "7:40 AM",
                                             "finished": "7:55 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        done = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertEqual(done.time_source, "reported")
        self.assertIn("7:40 AM", span_when(done))



class TestPointersAndItemsSaidTogether(GivenCase):
    """"Put it at 4:45" is the user's time for the one item just named; "A and B at 4:45" is both items' time."""

    def test_a_pointer_to_the_one_item_just_named(self):
        self.t.prior_exchange = ("", "Want me to put Drop off the parcel on this afternoon?")
        span = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"},
                        "yes, put it at 4:45")
        self.assertEqual(span.given_start, at("16:45"))
        self.assertEqual(span_when(span), "4:45 PM – 5:00 PM")

    def test_a_pointer_to_what_the_user_named_last_time(self):
        span = self.add({"title": "Water the ferns", "start": "2:00 PM", "duration": "15 min"}, "water the ferns at 2")
        self.t.prior_exchange = ("the ferns look dry", "")
        self.t.user_message = "move that to 5:10"
        out = self.t.execute("update_event", {"event": span.id, "start": "5:10 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertEqual(span.given_start, at("17:10"))

    def test_a_pointer_is_no_ones_when_two_items_were_named(self):
        self.add({"title": "Water the ferns", "start": "2:00 PM", "duration": "15 min"}, None)
        self.t.prior_exchange = ("", "Drop off the parcel, or Water the ferns first?")
        span = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"}, "put it at 4:45")
        self.assertIsNone(span.given_start)
        self.assertEqual(span_when(span), "4:30 PM – 5:00 PM")

    def test_items_said_together_share_the_time(self):
        message = "drop off the parcel and water the ferns at 4:45"
        parcel = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"}, message)
        ferns = self.add({"title": "Water the ferns", "start": "4:45 PM", "duration": "15 min", "alongside": True},
                         message)
        self.assertEqual((parcel.given_start, ferns.given_start), (at("16:45"), at("16:45")))

    def test_items_each_with_their_own_time_keep_their_own(self):
        message = "water the ferns at 4 and drop off the parcel at 4:45"
        parcel = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"}, message)
        ferns = self.add({"title": "Water the ferns", "start": "4:45 PM", "duration": "15 min", "alongside": True},
                         message)  # put at the parcel's time: not a time the user gave the ferns
        self.assertEqual((parcel.given_start, ferns.given_start), (at("16:45"), None))


class TestAShiftTheUserAsksFor(GivenCase):
    """A stated time shifted at the user's word ("an hour later") is still theirs: they anchored it."""

    def shifted(self, message, shift, prior=None):
        span = self.add({"title": "Water the ferns", "start": "4:45 PM", "duration": "30 min"},
                        "water the ferns at 4:45")
        self.t.prior_exchange = prior
        self.t.user_message = message
        out = self.t.execute("update_event", {"event": span.id, "shift": shift})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return next(s for s in self.t.day_plan.spans if s.id == span.id)

    def test_an_hour_later(self):
        span = self.shifted("move the ferns an hour later", "+1 hour")
        self.assertEqual((span.start_dt, span.given_start), (at("17:45"), at("17:45")))
        self.assertEqual(span_when(span), "5:45 PM – 6:30 PM")

    def test_pushed_back_by_a_pointer(self):
        span = self.shifted("push it back 20 minutes", "+20 minutes", prior=("", "Water the ferns is at 4:45 PM."))
        self.assertEqual(span.given_start, at("17:05"))
        self.assertTrue(start_exact(span))

    def test_a_shift_the_user_did_not_ask_for_drops_it(self):
        span = self.shifted("sounds good", "+1 hour")
        self.assertIsNone(span.given_start)
        self.assertEqual(span_when(span), "5:30 PM – 6:30 PM")

    def test_a_shift_of_a_time_thirties_picked_stays_by_blocks(self):
        span = self.add({"title": "Water the ferns", "start": "2:00 PM", "duration": "15 min"}, "water the ferns")
        self.t.user_message = "push the ferns back 15 minutes"
        self.t.execute("update_event", {"event": span.id, "shift": "+15 minutes"})
        span = next(s for s in self.t.day_plan.spans if s.id == span.id)
        self.assertIsNone(span.given_start)


class TestATimeThirtiesPicks(GivenCase):

    def test_with_nobody_talking_nothing_is_kept(self):
        span = self.add({"title": "Drop off the parcel", "start": "4:45 PM", "duration": "15 min"}, None)
        self.assertIsNone(span.given_start)
        self.assertEqual(span_when(span), "4:30 PM – 5:00 PM")

    def test_a_time_said_for_another_item_is_not_this_ones(self):
        span = self.add({"title": "Water the ferns", "start": "4:45 PM", "duration": "15 min"},
                        "the parcel drop is at 4:45, fit the ferns in somewhere")
        self.assertIsNone(span.given_start)
        self.assertFalse(start_exact(span))

    def test_a_time_the_user_did_not_say_is_not_kept(self):
        span = self.add({"title": "Water the ferns", "start": "1:15 PM", "duration": "15 min"},
                        "water the ferns after lunch")
        self.assertIsNone(span.given_start)
        self.assertNotIn("1:15", span_when(span))

    def test_a_placing_started_by_another_tool_is_never_the_users(self):
        from thirties_core.plan_log import tool_call
        self.t.user_message = "drop off the parcel at 4:45"
        with tool_call("apply_day_proposal"):  # a proposal, a carry-over, a routine: Thirties chose the time
            self.t.add_event("Drop off the parcel", "4:45 PM", duration="15 min")
        span = next(s for s in self.t.day_plan.spans if s.title == "Drop off the parcel")
        self.assertIsNone(span.given_start)
        self.assertEqual(span_when(span), "4:30 PM – 5:00 PM")

    def test_a_routines_steps_are_never_given_times(self):
        self.add_reset_routine()
        self.t.user_message = "start the evening reset at 4:45"
        out = self.t.execute("put_routine", {"routine": "Evening Reset", "start": "4:45 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        steps = [s for s in self.t.day_plan.spans if s.title in RESET_STEPS]
        self.assertTrue(steps)
        self.assertTrue(all(s.given_start is None and not start_exact(s) for s in steps))


if __name__ == "__main__":
    unittest.main()
