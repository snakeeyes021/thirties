"""A clear ask to be left alone is kept by Python, from the user's own words: nothing Thirties would say by itself
goes out until it ends, the user's next message ends an "until I write", a reminder the user set still rings unless
they said nothing at all, and a reply only promises quiet when quiet is kept. Invented: someone on the late shift at
a pottery studio who goes canoeing."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase, reply
from tests.test_schedule_reach import ServiceCase
from thirties_core import holds, quiet_asks
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory


class TestReadingTheAsk(unittest.TestCase):

    def parse(self, said, now="09:40", plan=None):
        return quiet_asks.parse(said, at(now), plan, day_end=at("23:00"))

    def test_until_a_clock_time(self):
        ask = self.parse("Throwing a batch of bowls now. Leave me be until 2 please.")
        self.assertEqual((ask.ends, ask.until_user, ask.strict), (at("14:00"), False, False))
        self.assertEqual(self.parse("No check-ins till 11:30am please, trimming mugs").ends, at("11:30"))

    def test_until_the_user_writes(self):
        for said in ("Not a peep from you for a while, please.",
                     "Pause all check-ins; I'll be back in touch after the kiln has cooled.",
                     "Radio silence please; I'll be back in touch after the kiln's unloaded."):
            ask = self.parse(said)
            self.assertIsNotNone(ask, said)
            self.assertTrue(ask.until_user, said)
            self.assertEqual(ask.ends, at("23:00"), said)  # never past the day

    def test_no_end_given_waits_for_the_user(self):
        self.assertTrue(self.parse("Stop the pings, I need to focus on the glaze tests").until_user)

    def test_for_a_stretch_and_the_rest_of_the_day(self):
        self.assertEqual(self.parse("Stay quiet for an hour, wedging clay").ends, at("10:40"))
        self.assertEqual(self.parse("quiet for 90 minutes pls").ends, at("11:10"))
        self.assertEqual(self.parse("Mute every notification for the rest of the day.").ends, at("23:00"))

    def test_until_a_meal_on_the_plan_or_its_usual_window(self):
        plan = type("Plan", (), {"spans": [EventSpan("ln", "Lunch at the studio", at("12:30"), at("13:00"),
                                                     SpanCategory.ACTIVITY)]})()
        self.assertEqual(self.parse("Keep things quiet until lunch, pulling handles", plan=plan).ends, at("12:30"))
        self.assertEqual(self.parse("Keep things quiet until lunch, pulling handles").ends, at("11:30"))

    def test_nothing_at_all_holds_reminders_too(self):
        self.assertTrue(self.parse("Nothing at all from you until 4, glaze firing. Not even reminders.").strict)
        self.assertFalse(self.parse("Quiet until 4 please").strict)

    def test_an_end_already_past_records_nothing(self):
        self.assertIsNone(self.parse("leave me be until 2", now="15:00"))

    def test_what_is_not_an_ask(self):
        for said in ("Did you send any check-ins while I was on the water?",
                     "There were no messages from the studio today.",
                     "No reminders about the clay order, it's handled.",
                     "Don't forget to remind me about the kiln at 3.",
                     "Remind me to rinse the canoe at 7.",
                     "Why didn't you ping me about the glaze order?"):
            self.assertIsNone(self.parse(said), said)
            self.assertFalse(quiet_asks.asks_quiet(said), said)


class QuietTurnCase(ConversationTestCase):

    def turn(self, said, model_says, *more):
        self.engine.set_next_response(reply(model_says))
        for extra in more:
            self.engine.set_next_response(reply(extra))
        return self.manager.send_user_message(said)

    def hold(self):
        return holds.active(self.manager.scheduler.state_db, at("09:00"))


class TestKeptFromTheWords(QuietTurnCase):

    def test_recorded_before_the_model_replies_whatever_it_calls(self):
        out = self.turn("I'll write when I'm free; keep things quiet till then.", "Enjoy the paddle!")
        self.assertEqual(out, "Enjoy the paddle!")
        data = self.hold()
        self.assertIsNotNone(data)
        self.assertTrue(data["until_user"])
        self.assertFalse(data["strict"])

    def test_a_pause_the_user_asked_for_isnt_refused(self):
        out = self.turn("Leave me be until 2 please, big glazing push.",
                        'CALL mute_checkins {"until": "2:00 PM"}', "Got it, quiet until 2.")
        tool_results = [m["content"] for m in self.manager.messages if m.get("role") == "tool"]
        self.assertFalse(any(r.startswith("NOT_DONE") for r in tool_results), tool_results)
        self.assertIn("quiet until 2", out)

    def test_the_models_hold_never_ends_on_the_asking_message(self):
        self.turn("Nothing from you before my next message, please.",
                  'CALL hold_messages {"until_said": "message"}', "Quiet it is.")
        data = self.hold()
        self.assertTrue(data["until_user"])  # the user's own ask stands, not a hold on a word in it
        self.assertFalse(holds.check(self.manager.scheduler.state_db, self.manager.day_plan, self.manager.messages,
                                     at("09:01")))

    def test_the_users_next_message_ends_until_i_write(self):
        self.turn("Nothing from you before my next message, please.", "Quiet it is.")
        set_debug_time(at("10:30"))
        self.turn("Back on shore. What's left today?", "Here's the rest of the day.")
        self.assertIsNone(holds.active(self.manager.scheduler.state_db, at("10:31")))

    def test_writing_again_with_the_same_ask_keeps_it(self):
        self.turn("Nothing from you before my next message, please.", "Quiet it is.")
        set_debug_time(at("10:30"))
        self.turn("Same again: nothing from you before my next message.", "Sure.")
        self.assertIsNotNone(holds.active(self.manager.scheduler.state_db, at("10:31")))


class TestNoFalsePromise(QuietTurnCase):

    def test_a_promise_of_quiet_with_nothing_kept_is_taken_out(self):
        out = self.turn("Starting on the mugs now.", "Have fun with the mugs! I'll keep things quiet so you can focus.")
        self.assertEqual(out, "Have fun with the mugs!")
        self.assertIsNone(self.hold())

    def test_kept_quiet_may_be_promised(self):
        out = self.turn("Leave me be until 2 please.", "Have fun! I'll keep things quiet until 2:00 PM.")
        self.assertIn("quiet until 2:00 PM", out)

    def test_the_guard_alone(self):
        self.assertTrue(quiet_asks.claims_quiet("You won't hear a peep from me."))
        self.assertTrue(quiet_asks.claims_quiet("I'll stay quiet till your next message."))
        self.assertFalse(quiet_asks.claims_quiet("Should I keep things quiet until 2?"))
        self.assertFalse(quiet_asks.claims_quiet("The studio is quiet on Mondays."))
        self.assertEqual(quiet_asks.without_false_quiet("I'll stay quiet.", None, at("09:00")), "OK.")


class TestAQuietStretchInTheBackground(ServiceCase):
    """A day in the background service: the bells, check-ins and catch-up wait out the quiet; the reminder the
    user set rings; the user's message ends it; nothing says "back on"."""

    def ask(self, said, model_says="Sure thing."):
        manager = self.svc.manager()
        self.svc.inference_engine.set_next_response(reply(model_says))
        return manager.send_user_message(said)

    def test_until_a_time(self):
        from thirties_core.memory import ReminderStore
        set_debug_time(at("16:50"))
        self.ask("Leave me be until 7:30, I'm trimming pots.")
        ReminderStore(self.sched.state_db).add(at("18:10"), "Bring the paddles in")
        self.svc.starts(at("17:00"))
        self.svc.check_ins(at("18:05"))
        self.svc.reminders(at("18:11"))
        self.svc.hold_check(at("18:12"))
        self.svc.check_ins(at("19:15"))
        self.svc.starts(at("20:00"))  # past 7:30: the film night's bell goes out
        kinds = [k for k, _ in self.desktop]
        self.assertEqual(kinds[0], "reminder")  # the one the user set rang
        self.assertNotIn("checkin", kinds[:2])
        self.assertNotIn("hold", kinds)  # it ends without a message
        self.assertEqual(self.desktop[0][1], "⏰ Reminder: Bring the paddles in")
        self.assertTrue(any(k == "start" and "Film night" in b for k, b in self.desktop), self.desktop)
        self.assertFalse(any("Sort the receipts" in b and k == "start" for k, b in self.desktop))

    def test_nothing_at_all_keeps_the_reminder_for_later(self):
        from thirties_core.memory import ReminderStore
        set_debug_time(at("16:50"))
        self.ask("Nothing at all from you until 6:30, not even reminders.")
        ReminderStore(self.sched.state_db).add(at("18:10"), "Bring the paddles in")
        self.svc.reminders(at("18:11"))
        self.assertEqual(self.desktop, [])
        self.svc.hold_check(at("18:31"))
        self.svc.reminders(at("18:32"))
        self.assertEqual(self.last("reminder"), "⏰ Reminder: Bring the paddles in")

    def test_until_the_user_writes(self):
        set_debug_time(at("16:50"))
        self.ask("Don't buzz me again before my next message.")
        self.svc.starts(at("17:00"))
        self.svc.check_ins(at("19:15"))
        set_debug_time(at("19:40"))
        self.svc.evening_catch_up(at("19:40"))
        self.assertEqual(self.desktop, [])  # no bell, no check-in, no catch-up
        self.ask("Back at the dock. Anything I missed?", "Here's where things stand.")
        self.assertIsNone(holds.active(self.sched.state_db, at("19:45")))
        self.svc.hold_check(at("19:46"))
        self.assertNotIn("hold", [k for k, _ in self.desktop])  # they're talking: no "back on" message
        self.assertFalse(self.svc._held(at("20:30"), "start"))  # bells are back for the next run


if __name__ == "__main__":
    unittest.main()
