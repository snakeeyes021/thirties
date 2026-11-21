"""How the assistant behaves, guaranteed by Python rather than left to its instructions (turn_guards.py,
reply_checks.py, today_times.py, morning_day.py, sky.py). Invented data throughout."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, mark_up
from tests.test_conversation import ConversationTestCase, reply
from tests.test_routines import FIRST, SECOND, RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE


def done_with_morning(tools):
    from thirties_core import morning
    morning.mark_done(tools._state_db, tools.day_plan.target_date, at("07:30"))


class TestAQuestionGetsAnAnswer(RoutineCase):
    """A question was taken for an instruction and acted on, again and again."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))
        done_with_morning(self.t)
        self.t.execute("add_event", {"title": "Fold the towels", "start": "1:00 PM", "duration": "30 minutes"})

    def test_what_counts_as_a_question(self):
        from thirties_core.turn_guards import is_question
        for asked in ("should I move the towels to 3?", "why are the towels at 1?", "what's next",
                      "ok. is 1 too early for the towels?", "what if the towels went at 3?"):
            self.assertTrue(is_question(asked), asked)
        for told in ("can you move the towels to 3?", "move the towels to 3", "towels done. what's next?",
                     "please put the towels at 3?", "do the towels at 3", "how about the towels at 3?", "", None):
            self.assertFalse(is_question(told), told)

    def test_a_question_changes_nothing(self):
        self.t.user_message = "should I move the towels to 3?"
        out = self.t.execute("update_event", {"event": "Fold the towels", "start": "3:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE) and "asked a question" in out, out)
        self.assertEqual(next(s.start_dt for s in self.t.day_plan.spans if s.title == "Fold the towels"), at("13:00"))
        self.assertFalse(self.t.execute("show_schedule", {}).startswith(NOT_DONE))  # looking is fine

    def test_a_request_or_a_report_is_acted_on(self):
        self.t.user_message = "can you move the towels to 3?"
        self.assertFalse(self.t.execute("update_event", {"event": "Fold the towels", "start": "3:00 PM"})
                         .startswith(NOT_DONE))
        self.t.user_message = "towels are folded. what's next?"
        set_debug_time(at("15:40"))
        self.assertFalse(self.t.execute("log_outcome", {"event": "Fold the towels", "status": "done"})
                         .startswith(NOT_DONE))

    def test_while_the_day_is_built_the_morning_goes_ahead(self):
        from thirties_core import morning
        self.t._state_db.set_sync_meta(morning.done_key(DAY), "")
        self.t.user_message = "what's the plan today?"
        self.assertFalse(self.t.execute("add_event", {"title": "Return the drill", "start": "4:00 PM",
                                                      "duration": "30 minutes"}).startswith(NOT_DONE))

    def test_the_apps_own_controls_are_never_held(self):
        self.t.user_message, self.t.by_hand = "why is this here?", True
        self.assertFalse(self.t.execute("update_event", {"event": "Fold the towels", "start": "3:00 PM"})
                         .startswith(NOT_DONE))


class TestFreeTimeStaysOpen(RoutineCase):
    """Relaxing was put on the plan, and checked in on."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("16:10"))
        done_with_morning(self.t)

    def test_relaxing_never_goes_on_unasked(self):
        self.t.user_message = "plan my evening"
        for title in ("Relax", "Downtime", "Free time", "Chill on the couch"):
            out = self.t.execute("add_event", {"title": title, "start": "8:00 PM", "duration": "1 hour"})
            self.assertTrue(out.startswith(NOT_DONE) and "Free time stays open" in out, (title, out))
        self.assertFalse(self.t.execute("add_event", {"title": "Relabel the spice jars", "start": "8:00 PM",
                                                      "duration": "30 minutes"}).startswith(NOT_DONE))

    def test_rest_they_ask_for_goes_on_and_is_never_checked_in_on(self):
        self.t.user_message = "I need a break from 5 to 6"
        out = self.t.execute("add_event", {"title": "Break", "start": "5:00 PM", "end": "6:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Break")
        self.assertTrue(self.services.mutes.is_muted(span.id, at("18:10")))

    def test_a_day_proposal_never_plans_relaxing(self):
        from thirties_core.turn_guards import refuse
        from thirties_core.plan_log import tool_call
        with tool_call("propose_day", None):
            held = refuse(self.t, "add_event", {"title": "Relax"}, set(), outermost=False)
        self.assertIn("Free time stays open", held)


class TestAnythingAskedForTodayGetsATime(RoutineCase):
    """An item added to a list for today had no time on the plan."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("16:10"))
        done_with_morning(self.t)

    def start_of(self, title):
        return next((s.start_dt for s in self.t.day_plan.spans if s.title == title), None)

    def test_added_to_a_list_for_today_its_placed(self):
        self.t.user_message = "add pick up the parcel to my errands, I'll grab it today"
        out = self.t.execute("add_task", {"title": "Pick up the parcel", "list": "Errands/Admin"})
        self.assertIn("On today's plan at", out)
        self.assertIsNotNone(self.start_of("Pick up the parcel"))
        self.assertIn("Pick up the parcel", self.body("n_errands"))
        self.assertNotIn("Pick up the parcel", self.body("n_routine"))  # never into a routine note

    def test_added_for_another_time_it_waits(self):
        self.t.user_message = "add renew the passport to my errands"
        out = self.t.execute("add_task", {"title": "Renew the passport", "list": "Errands/Admin"})
        self.assertNotIn("On today's plan", out)
        self.assertIsNone(self.start_of("Renew the passport"))

    def test_due_today_counts(self):
        self.t.user_message = "add the library books to errands"
        out = self.t.execute("add_task", {"title": "Return the library books", "list": "Errands/Admin",
                                          "due": DAY.isoformat()})
        self.assertIn("On today's plan at", out)


class TestTheReplyTalksRight(ConversationTestCase):
    """Made-up limits, made-up explanations, options handed back, talk of the workings, drops offered because the
    day is full, the moon guessed, the time now rounded."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:41"))
        self.manager.execute_tool("add_event", {"title": "Paint the fence", "start": "5:00 PM", "duration": "1 hour"})
        from thirties_core import morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("07:30"))

    def check_sent(self):
        return self.engine.call_history[1][-1]["content"]

    def test_the_time_now_is_said_as_it_is(self):
        from thirties_core.reply_checks import fix_now_time
        now = at("08:41")
        self.assertEqual(fix_now_time("It's 8:30 right now, so the fence is later.", now),
                         "It's 8:41 right now, so the fence is later.")
        self.assertEqual(fix_now_time("It's about 8:30 AM now.", now), "It's 8:41 AM now.")
        for kept in ("It's 8:39 now.", "You're in the 8:30 block now.", "It's 5:00 for the fence."):
            self.assertEqual(fix_now_time(kept, now), kept)
        self.engine.set_next_response(reply("It's 9:30 right now. The fence is at 5."))
        self.assertEqual(self.manager.send_user_message("what time is it?"), "It's 9:41 right now. The fence is at 5.")

    def test_no_made_up_limits(self):
        for _ in range(3):
            self.engine.set_next_response(reply("I can only talk here, so I can't change your schedule. "
                                                "The fence is at 5."))
        out = self.manager.send_user_message("fence at 6 instead")
        self.assertIn("can't do or see something you can", self.check_sent())
        self.assertEqual(out, "The fence is at 5.")

    def test_no_made_up_explanations(self):
        # where the item's record is comes with the message, as a note; the answer isn't sent back (why_notes.py)
        self.engine.set_next_response(reply('CALL item_history {"item": "Paint the fence"}'))
        self.engine.set_next_response(reply("It was added this morning at 5; nothing records why."))
        out = self.manager.send_user_message("why is painting the fence at 5?")
        self.assertTrue(any("item_history has the record for Paint the fence" in str(m.get("content") or "")
                            for m in self.engine.call_history[0] if m.get("role") == "user"))
        self.assertFalse(any(self.manager.brain._is_check(m) for m in self.manager.messages))
        self.assertEqual(out, "It was added this morning at 5; nothing records why.")

    def test_told_to_pick_the_choice_is_the_models_reading(self):
        # the model reads the choice left to it from the user's words: no note, never a rewrite
        self.engine.set_next_response(reply("Should I put the fence at 5:00 or 6:00?"))
        out = self.manager.send_user_message("you pick when the fence goes")
        self.assertFalse(any(str(m.get("content") or "").startswith("(The user's message")
                             for m in self.engine.call_history[0] if m.get("role") == "user"))
        self.assertEqual(out, "Should I put the fence at 5:00 or 6:00?")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_a_question_of_fact_is_fine(self):
        self.engine.set_next_response(reply("Is the paint delivery at 3 or 4?"))
        self.assertEqual(self.manager.send_user_message("add the paint delivery"), "Is the paint delivery at 3 or 4?")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_no_talk_of_the_workings(self):
        for _ in range(3):
            self.engine.set_next_response(reply("Under the hood, my internal planning session is anchored to "
                                                "yesterday. The fence is at 5."))
        out = self.manager.send_user_message("when's the fence?")
        self.assertIn("your own notes", self.check_sent())
        self.assertEqual(out, "The fence is at 5.")

    def test_the_days_end_is_no_reason_to_drop(self):
        self.engine.set_next_response(reply("The fence doesn't fit before bed tonight; want to drop it?"))
        self.engine.set_next_response(reply("The fence doesn't fit tonight, so it waits for tomorrow."))
        out = self.manager.send_user_message("add an hour of filing tonight")
        self.assertIn("no reason to drop", self.check_sent())
        self.assertEqual(out, "The fence doesn't fit tonight, so it waits for tomorrow.")

    def test_dropping_the_user_brought_up_is_fine(self):
        self.engine.set_next_response(reply("The fence doesn't fit before bed; want to drop it?"))
        self.manager.send_user_message("I might skip the fence, it's a lot")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_the_moon_is_computed(self):
        from thirties_core.sky import moon_phase, waxing
        self.assertEqual(moon_phase(datetime(2024, 4, 23, 23, 49, tzinfo=timezone.utc))[0], "full moon")
        self.assertEqual(moon_phase(datetime(2024, 4, 8, 18, 21, tzinfo=timezone.utc))[0], "new moon")
        self.assertTrue(waxing(datetime(2024, 4, 15, 19, 13, tzinfo=timezone.utc)))
        self.assertIn("Moon tonight:", self.manager.brain.build_system_prompt())
        now = at("09:41")
        wrong = "waxing" if not waxing(now) else "waning"
        self.engine.set_next_response(reply(f"The moon is {wrong} tonight."))
        self.engine.set_next_response(reply("Fine."))
        self.manager.send_user_message("how much moon tonight")
        self.assertIn("as computed", self.check_sent())

    def test_a_claimed_change_gets_a_fresh_reply_not_a_fixed_line(self):
        self.engine.set_next_response(reply("I've scheduled the fence for 6."))
        self.engine.set_next_response(reply("I've scheduled the fence for 6."))
        self.engine.set_next_response(reply("The fence is still at 5; say if you want it at 6."))
        out = self.manager.send_user_message("is 6 better for the fence")
        self.assertEqual(out, "The fence is still at 5; say if you want it at 6.")


class TestAPromiseNeedsItsCall(ConversationTestCase):
    """The reply promised to stop sending something ("got it, no more lists"), with no call behind it, and the
    lists kept coming."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:41"))
        self.manager.execute_tool("add_event", {"title": "Paint the fence", "start": "5:00 PM", "duration": "1 hour"})
        from thirties_core import morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("07:30"))

    def test_a_promise_with_no_call_is_sent_back_then_cut(self):
        for _ in range(3):
            self.engine.set_next_response(reply("Got it. I'll stop sending the lists from now on. The fence is at 5."))
        out = self.manager.send_user_message("stop sending me lists")
        # the lists are what Python adds to replies: only their setting keeps that promise
        self.assertIn("set_reply_extras wasn't called", self.engine.call_history[1][-1]["content"])
        self.assertEqual(out, "Got it. The fence is at 5.")

    def test_staying_quiet_is_a_promise_too(self):
        for _ in range(3):
            self.engine.set_next_response(reply("Understood. I won't say another word until the fence."))
        out = self.manager.send_user_message("quiet till the fence please")
        self.assertIn("no call recorded it", self.engine.call_history[1][-1]["content"])
        self.assertEqual(out, "Understood.")

    def test_another_call_doesnt_keep_a_promise_about_the_lists(self):
        for _ in range(3):
            self.engine.set_next_response(reply("No more lists.\nCALL mute_checkins {\"event\": \"Paint the fence\"}"))
        self.manager.send_user_message("no lists and don't ask about the fence")
        self.assertIn("set_reply_extras wasn't called", self.engine.call_history[1][-1]["content"])

    def test_a_promise_with_its_call_passes(self):
        self.engine.set_next_response(reply("I won't ask about the fence again.\n"
                                            'CALL mute_checkins {"event": "Paint the fence"}'))
        out = self.manager.send_user_message("don't ask me about the fence")
        self.assertEqual(out, "I won't ask about the fence again.")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_saying_what_it_will_do_next_is_no_promise(self):
        from thirties_core.reply_checks import PROMISE_RE
        self.assertIsNone(PROMISE_RE.search("I'll move the fence to 6 if you like."))
        self.assertIsNone(PROMISE_RE.search("I won't move the fence."))


class TestTheMorningHasTheWholeDayAndWhatWasAskedFor(RoutineCase):
    """The morning message left the routines out; a pinned item didn't come up on its day."""

    def setUp(self):
        super().setUp()
        from thirties_core.background import BackgroundService
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.catalog.set_urgency("t_bank", "now")  # something pressing, so the morning has a proposal to make
        self.services.tasks = self.catalog.apply(self.services.tasks)
        self.engine = MockInferenceEngine()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, self.services.tasks, pending, self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)), scheduler=self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc._manager, self.svc._day = self.m, DAY
        self.m.tools.execute("set_anchor", {"title": "Lunch", "time": "12:00 PM"})
        self.m.tools.execute("set_routine", {"routine": "Studio opening", "parts": [
            {"start": "9:00 AM", "items": FIRST}, {"start": "3:00 PM", "items": SECOND}]})

    def test_a_morning_reply_without_the_routines_gets_them(self):
        self.engine.responses = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "propose_day", "arguments": {"items": [{"item": "Call the bank", "start": "4:00 PM"}]}}]},
            {"role": "assistant", "content": "☀️ Good morning! 4:00 Call the bank, the pressing one."},
            {"role": "assistant", "content": "☀️ Good morning! 4:00 Call the bank, the pressing one."}]
        mark_up(self.sched.state_db, at("07:45"))
        self.svc.rollover(at("08:00"))
        text = self.sent[0][1]
        self.assertIn("leaves part of the day out", self.engine.call_history[-1][-1]["content"])
        self.assertIn("Feed the fish", text)
        self.assertIn("Lunch", text)
        self.assertNotIn("\n- ", text)

    def test_an_item_pinned_to_the_day_is_on_it_before_the_morning(self):
        set_debug_time(at("07:50"))
        self.m.tools.user_message = "make sure the bank call happens on Thursday"
        out = self.m.tools.execute("pin_to_period", {"task": "Call the bank", "when": "Thursday"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.m.tools.user_message = None
        mark_up(self.sched.state_db, at("07:45"))
        from thirties_core import morning
        morning.prepare(self.m)
        self.assertTrue(any(s.title == "Call the bank" and s.status == SpanStatus.PLANNED
                            for s in self.m.day_plan.spans))


if __name__ == "__main__":
    unittest.main()
