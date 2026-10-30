"""Taking turns (0.2 items 23-25): bursts get one reply, model timeouts are retried and lost turns kept, user
messages are stamped when they arrive, and the desktop's turns are mirrored to the phone."""

import threading
import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core import turns
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.turns import LOST_TURN_PREFIX, BrainUnavailable, RetryingEngine

TIMEOUT = {"role": "assistant", "content": "The Antigravity assistant timed out waiting for a response.",
           "tool_calls": []}


class TurnsCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.engine = MockInferenceEngine()
        waits = turns.RETRY_WAITS
        turns.RETRY_WAITS = (0.0, 0.0)
        self.addCleanup(setattr, turns, "RETRY_WAITS", waits)

    def manager(self, engine=None) -> ConversationManager:
        plan, pending = self.sched.build_day_plan(DAY)
        return ConversationManager(plan, [], pending, self.sched, inference_engine=engine or self.engine,
                                   active_notes=[])


class TestRetries(TurnsCase):

    def test_a_timeout_is_tried_again(self):
        engine = RetryingEngine(MockInferenceEngine([TIMEOUT, reply("Logged!")]), waits=(0.0, 0.0))
        self.assertEqual(engine.chat([])["content"], "Logged!")

    def test_a_usage_limit_is_not_retried(self):
        quota = dict(TIMEOUT, content="Failed invoking Antigravity CLI: quota exhausted")
        mock = MockInferenceEngine([quota, reply("never")])
        with self.assertRaises(BrainUnavailable) as caught:
            RetryingEngine(mock, waits=(0.0,)).chat([])
        self.assertEqual(caught.exception.why, "its usage limit is reached")
        self.assertEqual(len(mock.call_history), 1)

    def test_scripted_responses_still_reach_the_real_engine(self):
        manager = self.manager()
        manager.brain.inference_engine.responses = [reply("Hi there.")]
        self.assertEqual(manager.send_user_message("hello"), "Hi there.")

    def test_a_lost_turn_is_kept_and_answered_later(self):
        self.engine.responses = [TIMEOUT] * 3
        manager = self.manager()
        out = manager.send_user_message("cats are fed")
        self.assertTrue(out.startswith(LOST_TURN_PREFIX), out)
        self.assertIn("timed out", out)
        self.assertEqual([m["content"] for m in manager.unanswered()], ["cats are fed"])
        self.assertEqual([m["role"] for m in manager.history()], ["user", "assistant"])  # the user sees both
        self.assertIsNone(manager.answer_unanswered(at("09:01")))  # waits two minutes first
        self.engine.responses = [reply("Logged: cats fed.")]
        later = self.manager()  # e.g. the background service's manager
        self.assertEqual(later.answer_unanswered(at("09:03")), "Logged: cats fed.")
        self.assertEqual([m["content"] for m in later.history()],
                         ["cats are fed", out, "Logged: cats fed."])
        self.assertEqual(later.unanswered(), [])
        model_saw = self.engine.call_history[-1]
        self.assertNotIn(out, [m.get("content") for m in model_saw])  # the status note isn't the model's

    def test_a_retry_that_fails_again_stays_quiet_and_waits_longer(self):
        self.engine.responses = [TIMEOUT] * 6
        manager = self.manager()
        manager.send_user_message("cats are fed")
        self.assertIsNone(manager.answer_unanswered(at("09:03")))
        self.assertEqual(len(manager.history()), 2)  # no second "I couldn't reach the model"
        self.assertEqual(manager.unanswered()[0]["attempts"], 2)

    def test_a_new_message_answers_the_lost_one_too(self):
        self.engine.responses = [TIMEOUT] * 3 + [reply("Got both.")]
        manager = self.manager()
        manager.send_user_message("cats are fed")
        self.assertEqual(manager.send_user_message("hello?"), "Got both.")
        self.assertEqual(manager.unanswered(), [])
        self.assertIn("cats are fed", [m.get("content") for m in self.engine.call_history[-1]])

    def test_a_turn_lost_after_a_change_says_what_went_through(self):
        self.engine.responses = [reply('CALL add_event {"title": "Walk", "start": "5:00 PM", "duration": "30 min"}'),
                                 TIMEOUT, TIMEOUT, TIMEOUT]
        manager = self.manager()
        out = manager.send_user_message("add a walk at 5")
        self.assertIn("This went through:\n- ", out)
        self.assertIn("Walk", out)
        self.assertEqual(manager.unanswered(), [])


class TestBursts(TurnsCase):

    def test_messages_waiting_are_answered_in_one_turn(self):
        manager = self.manager()
        manager.enqueue("dinner at 7")
        self.engine.responses = [reply("Added dinner and a walk.")]
        self.assertEqual(manager.send_user_message("and a walk after"), "Added dinner and a walk.")
        self.assertEqual(len(self.engine.call_history), 1)
        users = [m["content"] for m in self.engine.call_history[0] if m["role"] == "user"]
        self.assertEqual(users, ["dinner at 7", "and a walk after"])  # each as sent
        self.assertEqual([m["role"] for m in manager.history()], ["user", "user", "assistant"])

    def test_a_message_sent_during_a_turn_joins_the_next_one(self):
        started, release = threading.Event(), threading.Event()

        class Slow(MockInferenceEngine):
            def chat(self, messages, tools=None):
                if not self.call_history:
                    started.set()
                    release.wait(5)
                return super().chat(messages, tools)
        engine = Slow([reply("First."), reply("Second, for both.")])
        desk, phone = self.manager(engine), self.manager(engine)  # the window's and the service's
        results = {}
        a = threading.Thread(target=lambda: results.setdefault("a", desk.send_user_message("one")))
        a.start()
        started.wait(5)
        b = threading.Thread(target=lambda: results.setdefault("b", phone.send_user_message("two", via="telegram")))
        c = threading.Thread(target=lambda: results.setdefault("c", desk.send_user_message("three")))
        b.start()
        c.start()
        while desk.gate.waiting() < 2:
            pass
        release.set()
        for t in (a, b, c):
            t.join(5)
        self.assertEqual(results["a"], "First.")
        self.assertEqual(sorted([results["b"], results["c"]]), ["", "Second, for both."])
        self.assertEqual(len(engine.call_history), 2)
        stored = self.manager(MockInferenceEngine())
        self.assertEqual([m["content"] for m in stored.history()], ["one", "First.", "two", "three",
                                                                    "Second, for both."])

    def test_a_quiet_wait_gathers_a_burst(self):
        manager = self.manager()
        threading.Timer(0.1, lambda: manager.enqueue("and the other thing")).start()
        self.engine.responses = [reply("Both done.")]
        self.assertEqual(manager.send_user_message("do the thing", quiet=0.4), "Both done.")
        self.assertEqual(len(self.engine.call_history), 1)


class TestStamps(TurnsCase):

    def test_a_message_is_stamped_when_it_arrived(self):
        manager = self.manager()
        manager.enqueue("first")
        set_debug_time(at("09:05"))
        manager.send_user_message("second")
        stamps = [(m["content"], m["at"][11:16]) for m in manager.history(times=True)]
        self.assertEqual(stamps[:2], [("first", "09:00"), ("second", "09:05")])
        self.assertNotIn("at", manager.history()[0])  # only asked for


class TestMirror(TurnsCase):

    def setUp(self):
        super().setUp()
        turns.MIRROR_IN_BACKGROUND = False
        self.addCleanup(setattr, turns, "MIRROR_IN_BACKGROUND", True)
        self.sent = []
        self.mirror = lambda texts, text, sent: self.sent.append((texts, text, sent))
        turns.add_mirror(self.mirror)
        self.addCleanup(turns.remove_mirror, self.mirror)

    def test_a_desktop_turn_goes_to_the_phone(self):
        self.engine.responses = [reply("Sure.")]
        self.manager().send_user_message("plan my evening")
        self.assertEqual(self.sent, [(["plan my evening"], "Sure.", False)])

    def test_a_phone_turn_isnt_echoed_but_a_desktop_message_in_it_is(self):
        manager = self.manager()
        manager.send_user_message("from the phone", via="telegram")
        self.assertEqual(self.sent, [])
        manager.enqueue("typed on the desktop", via="app")
        manager.send_user_message("and the phone again", via="telegram")
        self.assertEqual(self.sent[0][0], ["typed on the desktop"])
        self.assertTrue(self.sent[0][2])  # the reply goes to the phone with the turn itself


if __name__ == "__main__":
    unittest.main()
