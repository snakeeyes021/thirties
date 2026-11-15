"""The schedule the user is given is right: each way it came out wrong in real use, reproduced with invented data.

About one schedule reply in five was followed straight away by a correction. Each class below is one kind of wrong
schedule: meals or routines missing, list items with no time left out, someone else's events shown as the user's,
done items as rows and ids as titles, duplicates, the morning routine placed after noon, items dropped that nobody asked
to drop, and a reply saying something moved when it didn't.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import DAY, at, make_scheduler
from tests.test_calendar_choices import partners
from tests.test_conversation import ConversationTestCase, reply
from tests.test_routines import FIRST, SECOND, TOMORROW, RoutineCase
from tests.test_today import TodayTestCase
from thirties_core.assistant_tools import AssistantTools
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools


def starts(tools, titles):
    return {s.title: s.start_dt for s in tools.day_plan.spans if s.title in titles}


class TestOneFailureNeverKeepsTheDayOff(RoutineCase):
    """Meals and routines missing: the morning set-up ran as one step, so anything failing in it (the Today note's
    reset, one routine) left every routine and meal after it off the plan, and the day was marked set up."""

    def setUp(self):
        super().setUp()
        self.t.execute("set_anchor", {"title": "Lunch", "time": "12:00 PM"})
        self.t.execute("set_routine", {"routine": "Morning Routine", "parts": [{"start": "9:00 AM", "items": FIRST}]})
        self.t.execute("set_routine", {"routine": "Deep Clean", "parts": [{"start": "6:00 PM", "items": ["Vacuum"]}]})

    def test_one_routine_that_fails_leaves_the_others_and_the_meals(self):
        real = AssistantTools.put_routine

        def put(assistant, routine="", **kw):
            if routine == "Morning Routine":
                raise ValueError("a stored part that can't be read")
            return real(assistant, routine=routine, **kw)
        with patch.object(AssistantTools, "put_routine", put):
            self.t.assistant.routines.set_up_current()
        self.assertEqual(starts(self.t, {"Lunch", "Vacuum"}), {"Lunch": at("12:00"), "Vacuum": at("18:00")})
        self.assertIn("Morning Routine: couldn't be placed", self.t.assistant.routines.prompt_section())

    def test_the_today_reset_failing_leaves_the_routines_and_the_meals(self):
        calls = []

        def reset(assistant, day=None):
            calls.append(day)
            if len(calls) == 1:
                raise RuntimeError("the Today note can't be read")
            return ""
        with patch.object(AssistantTools, "start_the_day", reset):
            self.t.assistant.routines.set_up_current()
        self.assertEqual(set(starts(self.t, {"Lunch", "Feed the fish", "Vacuum"})), {"Lunch", "Feed the fish", "Vacuum"})


class TestListItemsWithNoTimeAreShown(TodayTestCase):
    """A line on today's list with no time on the plan (added to the list in Joplin) was in the model's schedule but
    missing from the schedule sent to the user."""

    def test_the_schedule_sent_lists_them_after_the_timed_items(self):
        self.t.execute("add_event", {"title": "Return the drill", "start": "5:00 PM", "duration": "30 minutes"})
        self.api.notes["n_today"]["body"] += "- [ ] Pick up the dry cleaning\n"  # added in Joplin, later that day
        out = self.t.execute("show_schedule", {})
        self.assertIn("On today's list, no time yet:\nPick up the dry cleaning", out)
        self.assertEqual(out.count("Return the drill"), 1)  # it has its time: a row, not a list line


class TestSomeoneElsesEventsArentTheUsers(unittest.TestCase):
    """Someone else's own events (on a calendar that isn't the user's, with no RSVP asked of them) were treated as
    invites to the user, and listed among the user's own items until answered. Now they're left off the user's day,
    listed as theirs with whose calendar they're on, and asked about once for the series (others_events.py)."""

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([partners("choir", "19:00", "21:00", title="Choir practice")])
        plan, self.pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, self.pending, self.sched)

    def test_not_in_the_users_schedule_and_asked_about_once(self):
        self.assertEqual([e.summary for e in self.pending], [])  # not an invite of the user's
        span = next(s for s in self.t.day_plan.spans if s.title == "Choir practice")
        self.assertTrue(span.shown_only and span.ask)  # theirs, and the question about the series is still open
        shown = self.t.execute("show_schedule", {})
        self.assertNotIn("Choir practice", shown)
        text = self.t.schedule_text()
        self.assertNotIn("Choir practice", text.split("Not yours today:")[0])
        self.assertTrue(text.endswith("Not yours today:\nChoir practice, 7:00 PM – 9:00 PM, Jamie's calendar"), text)

    def test_going_makes_it_the_users(self):
        self.t.execute("resolve_calendar_event", {"event": "Choir practice", "attend": True})
        span = next(s for s in self.t.day_plan.spans if s.title == "Choir practice")
        self.assertTrue(span.is_firm)
        self.assertFalse(span.shown_only or span.tentative)
        self.assertIn("Choir practice", self.t.execute("show_schedule", {}))

    def test_an_invite_to_the_user_on_their_calendar_is_still_an_invite(self):
        theirs = partners("swap", "19:00", "20:00", title="Book swap")
        theirs.response_status = "needsAction"  # the user is asked to come: an invite, theirs to answer
        sched = make_scheduler([theirs])
        plan, pending = sched.build_day_plan(DAY)
        span = next(s for s in plan.spans if s.title == "Book swap")
        self.assertTrue(span.tentative)
        self.assertEqual(len(pending), 1)


class TestNoDoneRowsAndNoIds(RoutineCase):
    """Done items read as rows of what's planned (joined to a planned item on one row), and an item renamed to its
    id showed the id as its title."""

    def test_a_rename_to_an_items_id_takes_the_items_words(self):
        self.services.tasks.append(TaskItem(id="5e7a1c9b2d4f8a63", source_notebook="1. Tasks",
                                            title="Return the library books", is_checkbox_item=True,
                                            parent_note_title="Errands/Admin", parent_note_id="n_errands"))
        self.t.execute("add_event", {"title": "Books", "start": "1:00 PM", "duration": "30 minutes"})
        out = self.t.execute("update_event", {"event": "Books", "title": "5e7a1c9b2d4f8a63"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Return the library books", [s.title for s in self.t.day_plan.spans])
        self.assertNotIn("5e7a1c9b2d4f8a63", self.t.schedule_text())

    def test_an_unknown_id_is_never_a_title(self):
        self.t.execute("add_event", {"title": "Books", "start": "1:00 PM", "duration": "30 minutes"})
        out = self.t.execute("update_event", {"event": "Books", "title": "0123456789abcdef"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Books", [s.title for s in self.t.day_plan.spans])

    def test_whats_marked_is_its_own_line_never_a_row(self):
        set_debug_time(at("13:40"))
        self.t.execute("add_event", {"title": "Water the plants", "start": "1:00 PM", "duration": "10 minutes"})
        self.t.execute("add_event", {"title": "Make tea", "start": "1:10 PM", "duration": "10 minutes"})
        self.t.execute("add_event", {"title": "Sort the mail", "start": "1:00 PM", "duration": "30 minutes",
                                     "alongside": True})
        self.t.execute("log_outcome", {"event": "Water the plants", "status": "done"})
        self.t.execute("log_outcome", {"event": "Sort the mail", "status": "skipped"})
        rows = self.t.schedule_text().split("Free time")[0]
        scheduled, marked = rows.split("Marked already (not still to do): ")
        self.assertIn("Make tea", scheduled)
        self.assertNotIn("Water the plants", scheduled)
        self.assertNotIn("Sort the mail", scheduled)
        self.assertIn("Water the plants (done, ", marked)
        self.assertIn("Sort the mail (skipped, 1:00 PM)", marked)

    def test_whats_left_of_the_day_never_lists_what_was_done_early(self):
        self.t.execute("add_event", {"title": "Fold the laundry", "start": "5:00 PM", "duration": "30 minutes"})
        self.t.execute("log_outcome", {"event": "Fold the laundry", "status": "done"})
        self.assertNotIn("Fold the laundry", self.t.execute("show_schedule", {}))


class TestNoDuplicates(RoutineCase):
    """The same item twice on one day: a long list line on the plan and its short title added again, and an item
    moved from another day onto a day that already had it."""

    LONG = "**Call the bank** about the card (details: https://example.org/card)"

    def test_the_short_title_of_a_planned_long_line_is_the_same_item(self):
        self.t.execute("add_event", {"title": self.LONG, "start": "1:00 PM", "duration": "30 minutes"})
        out = self.t.execute("add_event", {"title": "Call the bank", "start": "3:00 PM", "duration": "30 minutes"})
        self.assertTrue(out.startswith(NOT_DONE) and "already planned" in out, out)
        self.assertEqual(sum(1 for s in self.t.day_plan.spans if "Call the bank" in s.title), 1)

    def test_two_long_lines_sharing_a_headline_are_two_items(self):
        self.t.execute("add_event", {"title": self.LONG, "start": "1:00 PM", "duration": "30 minutes"})
        out = self.t.execute("add_event", {"title": "**Call the bank** about the loan (rates first)", "start": "3:00 PM",
                                           "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_moving_onto_a_day_that_has_it_never_makes_a_second(self):
        tomorrow = self.tools_for(TOMORROW)
        tomorrow.execute("add_event", {"title": "Buy stamps", "start": "1:00 PM", "duration": "30 minutes"})
        self.t.execute("add_event", {"title": "Buy stamps", "start": "2:00 PM", "duration": "30 minutes"})
        out = tomorrow.execute("update_event", {"event": "Buy stamps", "to_day": DAY.isoformat(), "start": "4:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE) and "already planned" in out, out)
        self.assertEqual([s.start_dt for s in self.tools_for(DAY).day_plan.spans if s.title == "Buy stamps"],
                         [at("14:00")])
        self.assertEqual(len([s for s in self.tools_for(TOMORROW).day_plan.spans if s.title == "Buy stamps"]), 1)


class TestTheMorningRoutineStaysInTheMorning(RoutineCase):
    """The morning routine at 3:30 PM: a step done late (moved to the afternoon after its morning time had gone by)
    was learned as where it goes, and the next mornings put it there."""

    def setUp(self):
        super().setUp()
        self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "9:00 AM", "items": FIRST}, {"start": "9:30 AM", "items": SECOND}]})  # learned, not confirmed

    def next_morning(self):
        set_debug_time(at("08:00", 1))
        tomorrow = self.tools_for(TOMORROW)
        tomorrow.assistant.routines.set_up_current()
        return starts(tomorrow, {"Feed the fish", "Make bed"})

    def test_a_late_move_today_isnt_where_it_goes(self):
        set_debug_time(at("15:10"))
        self.t.user_message = "only getting to the fish now, put it at 3:30"
        out = self.t.execute("update_event", {"event": "Feed the fish", "start": "3:30 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.next_morning()["Feed the fish"], at("09:00", 1))

    def test_a_late_put_routine_today_isnt_where_it_goes(self):
        set_debug_time(at("15:10"))
        for s in [s for s in self.t.day_plan.spans if s.title in FIRST + SECOND]:
            self.t._drop(s)
        self.t.user_message = "I skipped the morning routine, do it at 3:30"
        self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "3:30 PM", "items": FIRST}, {"start": "4:00 PM", "items": SECOND}]})
        self.assertEqual(self.next_morning(), {"Feed the fish": at("09:00", 1), "Make bed": at("09:30", 1)})

    def test_a_move_before_its_time_is_still_learned(self):
        set_debug_time(at("08:00"))
        self.t.user_message = "feed the fish at 10 from now on"
        self.t.execute("update_event", {"event": "Feed the fish", "start": "10:00 AM"})
        self.assertEqual(self.next_morning()["Feed the fish"], at("10:00", 1))


class TestNothingDroppedUnasked(RoutineCase):
    """An item vanished from the plan in a turn about something else."""

    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": "Renew the parking permit", "start": "1:00 PM", "duration": "30 minutes"})

    def remove(self, message):
        self.t.user_message = message
        return self.t.execute("remove_event", {"event": "Renew the parking permit"})

    def test_a_turn_about_something_else_never_takes_it_off(self):
        out = self.remove("lunch done")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Renew the parking permit", [s.title for s in self.t.day_plan.spans])

    def test_naming_it_or_asking_for_it_takes_it_off(self):
        self.assertFalse(self.remove("no need to renew the parking permit today").startswith(NOT_DONE))
        self.t.execute("add_event", {"title": "Renew the parking permit", "start": "1:00 PM", "duration": "30 minutes"})
        self.assertFalse(self.remove("actually take that off").startswith(NOT_DONE))

    def test_the_apps_own_controls_take_it_off(self):
        self.t.by_hand = True
        self.assertFalse(self.remove("").startswith(NOT_DONE))


class TestAClaimedMoveIsChecked(ConversationTestCase):
    """The reply said an item had been moved; nothing moved. The check for claimed changes only
    looked at whether anything changed in the turn, and only at "I moved ..." wording."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:00"))
        self.manager.execute_tool("add_event", {"title": "Fold the laundry", "start": "5:00 PM", "duration": "30 min"})
        self.manager.execute_tool("add_event", {"title": "Stretch", "start": "8:00 AM", "duration": "30 min"})

    def laundry(self):
        return next(s.start_dt for s in self.manager.day_plan.spans if s.title == "Fold the laundry")

    def test_a_move_claimed_beside_another_change_is_sent_back(self):
        self.engine.set_next_response(reply('Stretch is marked done. Shifted the laundry to right now.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply('CALL update_event {"event": "Fold the laundry", "start": "9:00 AM"}'))
        self.engine.set_next_response(reply("Stretch is done, and the laundry is at 9:00 now."))
        out = self.manager.send_user_message("stretched. do the laundry now instead")
        self.assertIn("Fold the laundry is still at 5:00 PM", self.engine.call_history[1][-1]["content"])
        self.assertEqual(self.laundry(), at("09:00"))
        self.assertEqual(out, "Stretch is done, and the laundry is at 9:00 now.")

    def test_a_claim_that_stays_false_gets_a_plain_line(self):
        self.engine.set_next_response(reply('Shifted the laundry to right now.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply("Shifted the laundry to right now."))
        self.engine.set_next_response(reply("Shifted the laundry to right now."))
        out = self.manager.send_user_message("stretched; laundry now")
        self.assertEqual(self.laundry(), at("17:00"))
        self.assertIn("That move didn't happen: Fold the laundry is still at 5:00 PM.", out)

    def test_moved_it_with_nothing_moved(self):
        self.engine.set_next_response(reply('Done, and shifted it to right now.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply("Shifted it to right now."))
        self.engine.set_next_response(reply("Shifted it to right now."))
        out = self.manager.send_user_message("stretched, and move the laundry to now")
        self.assertIn("nothing on the plan moved", self.engine.call_history[1][-1]["content"])
        self.assertIn("Nothing has moved yet", out)

    def test_a_real_move_passes(self):
        self.engine.set_next_response(reply('Moved the laundry to 10.\n'
                                            'CALL update_event {"event": "Fold the laundry", "start": "10:00 AM"}'))
        out = self.manager.send_user_message("laundry at 10")
        self.assertEqual(out, "Moved the laundry to 10.")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_an_offer_is_no_claim(self):
        self.engine.set_next_response(reply("The laundry is at 5. Want it shifted earlier?"))
        out = self.manager.send_user_message("what's later?")
        self.assertEqual(out, "The laundry is at 5. Want it shifted earlier?")

    def test_with_the_turn_bookkeeping_on_too(self):
        self.manager.scheduler.config.assistant.turn_bookkeeping = True
        self.engine.set_next_response(reply('Shifted the laundry to right now.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply("Shifted the laundry to right now."))
        self.engine.set_next_response(reply("Shifted the laundry to right now."))
        out = self.manager.send_user_message("stretched; laundry now")
        self.assertIn("That move didn't happen: Fold the laundry is still at 5:00 PM.", out)


class TestStartedIsNotDone(RoutineCase):
    """Told something had started ("the dishwasher's going", "I'm in the shower now"), the assistant marked it done."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("17:05"))
        self.t.execute("add_event", {"title": "Run the dishwasher", "start": "5:00 PM", "duration": "30 minutes"})
        self.t.execute("add_event", {"title": "Take a shower", "start": "5:00 PM", "duration": "30 minutes",
                                     "alongside": True})

    def done(self, message, item):
        self.t.user_message = message
        return self.t.execute("log_outcome", {"event": item, "status": "done"})

    def test_the_words_about_one_item(self):
        from thirties_core.started import only_started
        others = ["Run the dishwasher", "Take a shower", "Walk the dog"]
        self.assertTrue(only_started("the dishwasher's going", "Run the dishwasher", others))
        self.assertTrue(only_started("I'm in the shower now", "Take a shower", others))
        self.assertTrue(only_started("heading out with the dog", "Walk the dog", others))
        self.assertFalse(only_started("dishwasher's done", "Run the dishwasher", others))
        self.assertFalse(only_started("showered", "Take a shower", others))
        self.assertFalse(only_started("out of the shower, starting the dishwasher", "Take a shower", others))
        self.assertTrue(only_started("out of the shower, starting the dishwasher", "Run the dishwasher", others))
        self.assertFalse(only_started("the dishwasher's going", "Take a shower", others))  # not named
        self.assertFalse(only_started(None, "Take a shower", others))

    def test_begun_is_never_marked_done(self):
        for message, item in (("the dishwasher's going", "Run the dishwasher"),
                              ("I'm in the shower now", "Take a shower")):
            out = self.done(message, item)
            self.assertTrue(out.startswith(NOT_DONE) and "doing_now" in out, out)
            span = next(s for s in self.t.day_plan.spans if s.title == item)
            self.assertEqual(span.status, SpanStatus.PLANNED)

    def test_finished_is_marked_done(self):
        self.assertFalse(self.done("dishwasher's finished", "Run the dishwasher").startswith(NOT_DONE))
        self.assertFalse(self.done("just got back from walking the dog", "Walk the dog").startswith(NOT_DONE))

    def test_something_that_runs_by_itself_is_recorded_as_started_and_moves_nothing(self):
        self.t.user_message = "the dishwasher's going"
        out = self.t.execute("doing_now", {"item": "Run the dishwasher", "by_itself": True})
        self.assertIn("not done yet", out)
        washer = next(s for s in self.t.day_plan.spans if s.title == "Run the dishwasher")
        shower = next(s for s in self.t.day_plan.spans if s.title == "Take a shower")
        self.assertEqual((washer.status, washer.actual_start), (SpanStatus.PLANNED, at("17:05")))
        self.assertEqual(shower.start_dt, at("17:00"))


class TestWhatTheyreDoingNowIsThePlan(RoutineCase):
    """Heading out on errands, the plan still had a shower in that half hour, so "Now" listed what they weren't doing;
    and a task was placed inside the hour of rest they'd asked for after a delivery."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("17:05"))
        self.t.execute("add_event", {"title": "Take a shower", "start": "5:00 PM", "duration": "30 minutes"})
        self.t.execute("add_event", {"title": "Water the garden", "start": "5:30 PM", "duration": "30 minutes"})

    def start_of(self, title):
        return next(s.start_dt for s in self.t.day_plan.spans if s.title == title)

    def test_heading_out_moves_what_was_planned_then(self):
        self.t.user_message = "heading out on errands till 6"
        out = self.t.execute("doing_now", {"item": "Errands", "until": "6:00 PM"})
        self.assertIn("moved Take a shower to 6:00 PM", out)
        self.assertEqual(self.start_of("Errands"), at("17:00"))
        self.assertEqual(self.start_of("Take a shower"), at("18:00"))
        self.assertGreaterEqual(self.start_of("Water the garden"), at("18:00"))
        errands = next(s for s in self.t.day_plan.spans if s.title == "Errands")
        self.assertEqual((errands.status, errands.actual_start), (SpanStatus.PLANNED, at("17:05")))
        from thirties_core.messages import status_line
        now = status_line(self.t.day_plan, at("17:05"))
        self.assertIn("Errands", now)
        self.assertNotIn("shower", now.lower())

    def test_an_item_on_the_plan_comes_to_now(self):
        self.t.user_message = "actually watering the garden now"
        self.t.execute("doing_now", {"item": "Water the garden"})
        self.assertEqual(self.start_of("Water the garden"), at("17:00"))
        self.assertGreaterEqual(self.start_of("Take a shower"), at("17:30"))

    def test_a_rest_they_ask_for_keeps_its_time(self):
        set_debug_time(at("16:10"))
        self.t.user_message = "the delivery comes at 4:35, then I need an hour of rest"
        self.t.execute("add_event", {"title": "Rest", "start": "4:35 PM", "duration": "1 hour"})
        rest = next(s for s in self.t.day_plan.spans if s.title == "Rest")
        self.assertEqual((rest.start_dt, rest.end_dt), (at("16:35"), at("17:35")))
        self.assertGreaterEqual(self.start_of("Take a shower"), at("17:35"))  # what was planned there moved out
        self.t.user_message = "add a call to the plumber at 5:30"
        out = self.t.execute("add_event", {"title": "Call the plumber", "start": "5:30 PM", "duration": "30 minutes"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # not inside the rest without saying so
        self.t.user_message = "replan"
        self.t.execute("redraw", {})
        rest = next(s for s in self.t.day_plan.spans if s.title == "Rest")
        self.assertEqual((rest.start_dt, rest.end_dt), (at("16:35"), at("17:35")))
        inside = [s.title for s in self.t.day_plan.spans if s.start_dt < at("17:35") and s.end_dt > at("16:35")
                  and not s.is_envelope and s.title != "Rest" and s.status == SpanStatus.PLANNED]
        self.assertEqual(inside, [])

    def test_python_never_doubles_up_inside_a_rest(self):
        from thirties_core.if_time import doubled_slot
        self.t.user_message = "rest from 6 to 11"
        self.t.execute("add_event", {"title": "Rest", "start": "6:00 PM", "end": "11:00 PM"})
        spot = doubled_slot(self.t, 30, at("18:05"))  # from inside the rest: never in it
        self.assertTrue(spot is None or spot >= at("23:00"), spot)

    def test_a_break_down_task_isnt_a_rest(self):
        from thirties_core.started import is_rest
        self.assertTrue(is_rest("Rest after the delivery"))
        self.assertTrue(is_rest("a short nap"))
        self.assertFalse(is_rest("Break down the boxes"))
        self.assertFalse(is_rest("Restring the guitar"))


class TestAClaimedDoneIsChecked(ConversationTestCase):
    """A reply said things were done that the tools never marked."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:05"))
        self.manager.execute_tool("add_event", {"title": "Stretch", "start": "9:00 AM", "duration": "30 min"})
        self.manager.execute_tool("add_event", {"title": "Refill the bird feeder", "start": "9:00 AM", "duration": "10 min",
                                                "alongside": True})

    def status(self, title):
        return next(s.status for s in self.manager.day_plan.spans if s.title == title)

    def test_a_done_said_of_an_item_never_marked_is_sent_back(self):
        self.engine.set_next_response(reply('Stretch is marked done. Refill the bird feeder is done too.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply('CALL log_outcome {"event": "Refill the bird feeder", "status": "done"}'))
        self.engine.set_next_response(reply("Both are marked done."))
        out = self.manager.send_user_message("stretched and refilled the bird feeder")
        self.assertIn("Refill the bird feeder isn't recorded as done", self.engine.call_history[1][-1]["content"])
        self.assertEqual(self.status("Refill the bird feeder"), SpanStatus.DONE)
        self.assertEqual(out, "Both are marked done.")

    def test_a_false_done_that_stands_gets_a_plain_line(self):
        self.engine.set_next_response(reply('Stretch is marked done. Refill the bird feeder is done too.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply("Stretch is marked done. Refill the bird feeder is done too."))
        self.engine.set_next_response(reply("Stretch is marked done. Refill the bird feeder is done too."))
        out = self.manager.send_user_message("stretched")
        self.assertIn("(Refill the bird feeder isn't recorded as done.)", out)

    def test_started_and_replied_as_done(self):
        self.engine.set_next_response(reply('Stretch is marked done.\n'
                                            'CALL log_outcome {"event": "Stretch", "status": "done"}'))
        self.engine.set_next_response(reply('CALL doing_now {"item": "Stretch"}'))
        self.engine.set_next_response(reply("Enjoy the stretch; tell me when you're finished."))
        out = self.manager.send_user_message("I'm stretching now")
        self.assertEqual(self.status("Stretch"), SpanStatus.PLANNED)
        self.assertEqual(out, "Enjoy the stretch; tell me when you're finished.")


class TestTheReleaseCheckDays(RoutineCase):
    """Simulated days from the release check, rebuilt with invented items: what the user said, and what the plan
    must hold afterwards."""

    def add(self, title, start, minutes, **kw):
        out = self.t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} minutes", **kw})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def test_running_and_elsewhere_are_not_done(self):
        set_debug_time(at("08:50"))
        self.add("Take a shower", "8:30 AM", 20)
        self.add("Run the dishwasher", "8:30 AM", 15, alongside=True)
        self.t.user_message = "dishwasher's going. off up to the attic to sort boxes now"
        self.assertIn("doing_now", self.t.execute("log_outcome", {"event": "Run the dishwasher", "status": "done"}))
        self.t.user_message = "the dishwasher just started, it is NOT done. I'm up in the attic, no shower yet"
        for item in ("Run the dishwasher", "Take a shower"):
            out = self.t.execute("log_outcome", {"event": item, "status": "done", "finished": "8:50 AM"})
            self.assertTrue(out.startswith(NOT_DONE), out)
            self.assertEqual(self.span(item).status, SpanStatus.PLANNED)

    def test_a_started_machine_is_not_the_job_done(self):
        set_debug_time(at("12:03"))
        self.add("Wash the duvet cover", "2:30 PM", 30)
        self.t.user_message = "already pulled the duvet cover off and started the washer, so the 2:30 slot is useless"
        out = self.t.execute("log_outcome", {"event": "Wash the duvet cover", "status": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_out_on_errands_moves_whats_planned_now(self):
        set_debug_time(at("17:05"))
        self.add("Take a shower", "5:00 PM", 30)
        self.add("Recycling run & hardware store", "5:30 PM", 40)
        self.t.user_message = "taking the boxes down now, heading to the hardware store after"
        self.t.execute("update_event", {"event": "Recycling run & hardware store", "start": "5:05 PM"})
        errands = self.span("Recycling run & hardware store")
        self.assertLessEqual(errands.start_dt, at("17:05"))
        self.assertGreaterEqual(self.span("Take a shower").start_dt, errands.end_dt)

    def test_a_time_only_this_morning_isnt_made_up(self):
        set_debug_time(at("16:01"))
        self.add("Toast and tea", "9:00 AM", 30)
        self.t.user_message = "the sheets went in the dryer around 1:15. toast and tea got done this morning too"
        self.t.execute("log_outcome", {"event": "Toast and tea", "status": "done", "started": "11:15 AM",
                                       "finished": "11:30 AM", "duration": "15 minutes"})
        toast = self.span("Toast and tea")
        self.assertEqual(toast.status, SpanStatus.DONE)
        self.assertNotEqual(toast.time_source, "reported")
        self.assertNotEqual(toast.actual_start, at("11:15"))


class TestTheReplyMatchesThePlan(ConversationTestCase):
    """The reply gave times the plan didn't have ("set for 3:30" over a plan at 4:00; "at 5:05" beside its own Next
    line saying 5:30), and said things were off the plan that were still on it."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("17:25"))
        self.manager.execute_tool("add_event", {"title": "Batch-cook the lentils", "start": "5:30 PM",
                                                "duration": "1 hour"})
        self.manager.execute_tool("add_event", {"title": "Practice the ukulele", "start": "6:30 PM",
                                                "duration": "30 min"})

    def start_of(self, title):
        return next(s.start_dt for s in self.manager.day_plan.spans if s.title == title)

    def test_a_time_the_plan_doesnt_have_is_sent_back(self):
        self.engine.set_next_response(reply('The lentils are cued up for 5:35.\n'
                                            'CALL update_event {"event": "Batch-cook the lentils", "start": "5:35 PM"}'))
        self.engine.set_next_response(reply("The lentils are set for 6:00."))
        out = self.manager.send_user_message("home, putting groceries away, lentils in like 10 min")
        self.assertEqual(self.start_of("Batch-cook the lentils"), at("18:00"))
        self.assertIn("Batch-cook the lentils is at 6:00 PM", self.engine.call_history[1][-1]["content"])
        self.assertEqual(out, "The lentils are set for 6:00.")

    def test_a_wrong_time_that_stands_gets_the_plans(self):
        for _ in range(3):
            self.engine.set_next_response(reply("Lentils wrap at 6:30, and the ukulele is at 6:35."))
        out = self.manager.send_user_message("what's later?")
        self.assertIn("(On the plan: Practice the ukulele is at 6:30.)", out)

    def test_times_as_planned_and_other_times_pass(self):
        self.engine.set_next_response(reply("Lentils at 5:30 (until 6:30), then the ukulele at 6:30. The parcel is "
                                            "due at 6:10, and I'll ping you at 6:25."))
        out = self.manager.send_user_message("what's later?")
        self.assertNotIn("On the plan", out)
        self.assertEqual(len(self.engine.call_history), 1)

    def test_a_calendar_time_said_as_it_is_passes(self):
        manager = self.make_manager(events=[__import__("tests.fixtures", fromlist=["event"]).event(
            "drop", "08:15", "08:50", "School drop-off")])
        set_debug_time(at("06:32"))
        self.engine.set_next_response(reply("Drop-off is at 8:15."))
        self.assertEqual(manager.send_user_message("when's drop-off?"), "Drop-off is at 8:15.")

    def test_said_off_the_plan_while_still_on_it(self):
        for _ in range(3):
            self.engine.set_next_response(reply("The ukulele is off the schedule."))
        out = self.manager.send_user_message("skip the ukulele tonight")
        self.assertIn("(Practice the ukulele is still on the plan at 6:30.)", out)


class TestNoCorrectionOfAnUnseenDraft(ConversationTestCase):
    """Replies opened "I need to correct my last message" about a draft the user never saw: the send-back's rewrite
    talked about the draft."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:05"))
        self.manager.execute_tool("add_event", {"title": "Fold the laundry", "start": "5:00 PM", "duration": "30 min"})

    def test_the_rewrite_is_told_the_draft_was_never_seen_and_written_fresh(self):
        self.engine.set_next_response(reply("Shifted the laundry to 6."))
        self.engine.set_next_response(reply("I need to correct my last message: the laundry hadn't actually moved "
                                            "then. It's at 6:00 now.\n"
                                            'CALL update_event {"event": "Fold the laundry", "start": "6:00 PM"}'))
        self.engine.set_next_response(reply("The laundry is at 6:00 now."))
        out = self.manager.send_user_message("laundry at 6")
        sent_back = self.engine.call_history[1][-1]["content"]
        self.assertIn("hasn't seen your draft", sent_back)
        self.assertEqual(out, "The laundry is at 6:00 now.")

    def test_an_opening_that_stays_is_cut(self):
        self.engine.set_next_response(reply("Shifted the laundry to 6."))
        self.engine.set_next_response(reply("One correction: the laundry hadn't actually moved then. It's at 6:00 "
                                            'now.\nCALL update_event {"event": "Fold the laundry", "start": "6:00 PM"}'))
        self.engine.set_next_response(reply("I need to correct my last message: it's at 6:00 now."))
        out = self.manager.send_user_message("laundry at 6")
        self.assertEqual(out, "It's at 6:00 now.")

    def test_a_correction_of_what_the_user_read_stays(self):
        self.engine.set_next_response(reply("The laundry is at five."))
        self.manager.send_user_message("when's the laundry?")
        self.manager.execute_tool("update_event", {"event": "Fold the laundry", "start": "6:00 PM"})
        self.engine.set_next_response(reply("Correction: the laundry is at six, not five."))
        out = self.manager.send_user_message("are you sure?")
        self.assertEqual(out, "Correction: the laundry is at six, not five.")


class TestTheClaudeBrainDays(RoutineCase):
    """From the second brain's simulated days, rebuilt with invented items: a range whose start is wrong, a pickup
    said half an hour early, and an item put on tonight that the user had said was for tomorrow."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("15:12"))
        from thirties_core import morning
        morning.mark_done(self.sched.state_db, DAY, at("07:30"))
        self.t.execute("add_event", {"title": "Bagel lunch", "start": "3:30 PM", "duration": "30 minutes"})
        self.t.execute("add_event", {"title": "Collect the framed prints", "start": "5:30 PM", "duration": "45 minutes"})

    def wrong(self, text):
        from thirties_core.brains.frontier import FrontierBrain
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.t
        return brain._wrong_times(text)

    def test_a_range_with_a_wrong_start(self):
        self.assertEqual(self.wrong("I put the bagel lunch on the plan from 3:00 to 4:00."),
                         ["Bagel lunch is at 3:30 PM"])
        self.assertEqual(self.wrong("The bagel lunch is from 3:30 to 4:00."), [])

    def test_a_pickup_said_early(self):
        self.assertEqual(self.wrong("5:00 – Collect the framed prints (until 6:00)"),
                         ["Collect the framed prints is at 5:30 PM"])
        self.assertEqual(self.wrong("5:30 – Collect the framed prints (until 6:15)"), [])

    def test_an_item_for_tomorrow_stays_off_tonight(self):
        from datetime import timedelta
        self.t.user_message = "the gallery tweaks are for tomorrow"
        out = self.t.execute("add_task", {"title": "Gallery: tweak the mat colours", "list": "Errands/Admin",
                                          "due": (DAY + timedelta(days=1)).isoformat()})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.t.user_message = "prints collected and lunch eaten. what's left tonight?"
        out = self.t.execute("propose_day", {"items": [{"item": "Gallery: tweak the mat colours", "start": "7:00 PM"}]})
        self.assertTrue(out.startswith(NOT_DONE) and "is for tomorrow" in out, out)
        out = self.t.execute("add_event", {"title": "Gallery: tweak the mat colours", "start": "7:00 PM",
                                           "duration": "30 minutes"})
        self.assertTrue(out.startswith(NOT_DONE) and "is for tomorrow" in out, out)
        self.t.user_message = "actually do the mat colours tonight at 7"
        out = self.t.execute("add_event", {"title": "Gallery: tweak the mat colours", "start": "7:00 PM",
                                           "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
