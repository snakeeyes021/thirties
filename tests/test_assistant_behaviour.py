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


class ScriptedEngine:
    """A stand-in for the model: each round of a turn takes the next scripted answer; the system prompt of every
    round is kept. Calls without tools (the small helper) get an empty answer."""

    def __init__(self, *rounds):
        self.rounds, self.prompts = list(rounds), []

    def chat(self, messages=None, tools=None, **_):
        if tools is None:
            return {"content": ""}
        self.prompts.append(messages[0]["content"])
        return self.rounds.pop(0) if self.rounds else {"content": "Done."}


def call(name, **args):
    return {"content": "", "tool_calls": [{"id": "c1", "name": name, "arguments": args}]}


class TestAQuestionIsAFactNotABlock(RoutineCase):
    """A message that reads as a question was refused any change by Python, though a rhetorical question can be the
    ask. Now the model is told the fact and its call stands."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))
        done_with_morning(self.t)
        self.t.execute("add_event", {"title": "True the wheel", "start": "1:20 PM", "duration": "30 minutes"})
        self.t.execute("add_event", {"title": "Sort the spare tubes", "start": "2:40 PM", "duration": "30 minutes"})

    def titles(self):
        return [s.title for s in self.t.day_plan.spans]

    def start_of(self, title):
        return next(s.start_dt for s in self.t.day_plan.spans if s.title == title)

    def turn(self, words, *rounds):
        from thirties_core.brains.frontier import FrontierBrain
        engine = ScriptedEngine(*rounds)
        brain = FrontierBrain(self.t, engine)
        self.t.user_message = words
        return brain.respond(words), engine

    def test_the_fact_is_given_for_a_question_mark_only(self):
        from thirties_core.turn_guards import question_fact
        for asked in ("is the wheel job better later?", "what's left?", "the tubes again??", "ok?  "):
            self.assertIn("is a question", question_fact(asked), asked)
            self.assertIn("only when the message asks", question_fact(asked), asked)
        for told in ("swap the wheel job with the tubes", "what's next", "", "   ", None):
            self.assertEqual(question_fact(told), "", told)

    def test_the_model_is_told_in_its_prompt(self):
        _, engine = self.turn("what's left today?", {"content": "Two jobs."})
        self.assertIn("The user's message is a question", engine.prompts[0])
        _, engine = self.turn("what's left today", {"content": "Two jobs."})
        self.assertNotIn("The user's message is a question", engine.prompts[0])

    def test_a_pure_question_changes_nothing_because_the_model_changes_nothing(self):
        before = [(s.title, s.start_dt) for s in self.t.day_plan.spans]
        self.turn("what's left today?", {"content": "Two jobs: the wheel, then the tubes."})
        self.assertEqual([(s.title, s.start_dt) for s in self.t.day_plan.spans], before)

    def test_a_rhetorical_question_that_demands_a_removal_lets_it_through(self):
        self.turn("tubes sorting is pointless in this heat. who put it there??",
                  call("remove_event", event="Sort the spare tubes"), {"content": "Took it off."})
        self.assertNotIn("Sort the spare tubes", self.titles())

    def test_other_changes_the_model_makes_on_a_question_stand_too(self):
        self.t.user_message = "surely the wheel job fits better after 4:10, with the shop quiet?"
        out = self.t.execute("update_event", {"event": "True the wheel", "start": "4:10 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.start_of("True the wheel"), at("16:10"))
        self.t.user_message = "the tubes?"
        out = self.t.execute("add_event", {"title": "Oil the chains", "start": "5:20 PM", "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_done_asked_for_earlier_is_not_refused_for_the_next_question(self):
        set_debug_time(at("15:40"))
        self.t.pending_dones = ["Sort the spare tubes"]
        self.t.user_message = "why is it not ticked yet??"
        out = self.t.execute("log_outcome", {"event": "Sort the spare tubes", "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_turn_guards_hold_nothing_for_a_question(self):
        from thirties_core.turn_guards import refuse
        self.t.user_message = "the tubes, again??"
        for name, args in (("remove_event", {"event": "Sort the spare tubes"}),
                           ("log_outcome", {"event": "Sort the spare tubes", "status": "done"}),
                           ("update_event", {"event": "True the wheel", "start": "3:00 PM"})):
            self.assertIsNone(refuse(self.t, name, args), name)

    def test_a_report_is_acted_on(self):
        self.t.user_message = "the wheel is trued. what's next?"
        set_debug_time(at("15:40"))
        self.assertFalse(self.t.execute("log_outcome", {"event": "True the wheel", "status": "done"})
                         .startswith(NOT_DONE))

    def test_the_apps_own_controls_are_never_held(self):
        self.t.user_message, self.t.by_hand = "why is this here?", True
        self.assertFalse(self.t.execute("update_event", {"event": "True the wheel", "start": "3:00 PM"})
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
            held = refuse(self.t, "add_event", {"title": "Relax"}, outermost=False)
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
        asked = [m["content"] for call in self.engine.call_history for m in call if m["role"] == "user"]
        self.assertFalse(any("leaves part of the day out" in a for a in asked))  # never asked: Python places the list
        from thirties_core.day_lists import planned_ahead_text
        heading = planned_ahead_text(self.m.tools.assistant).splitlines()[0]  # Python's own list
        self.assertEqual(text.count(heading), 1)
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
