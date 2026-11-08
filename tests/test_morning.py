"""One morning message (A1): the user's first message of the day builds the day, and its reply is the morning's
only message; the background sends one only if no turn did; and one written policy for proposing."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core import morning


class TestOneMorning(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:25"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        task = TaskItem(id="t_vase", source_notebook="1. Tasks", title="Glue the blue vase", is_checkbox_item=True,
                        parent_note_title="Home", parent_note_id="n_home")
        self.sched.state_db.upsert_span(EventSpan("gv", "Glue the blue vase", at("17:00", -1), at("17:30", -1),
                                                  SpanCategory.TASK, task_id="t_vase"))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [task], []
        self.sent = []
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)), scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.m = self.svc.manager()

    def test_the_first_reply_is_the_morning_and_nothing_follows(self):
        self.engine.set_next_response({"role": "assistant", "content": "☀️ Good morning! Kept 1 from before.",
                                       "tool_calls": []})
        reply = self.m.send_user_message("up and at it")
        self.assertIn("Good morning", reply)
        prompt = [m["content"] for m in self.engine.call_history[0] if m["role"] == "user"]
        self.assertTrue(any(p.startswith("[System check") and "the morning's only message" in p for p in prompt))
        self.assertEqual(self.sched.state_db.get_sync_meta(morning.done_key(DAY)), "in_conversation")
        set_debug_time(at("09:26"))
        self.svc.rollover(at("09:26"))
        self.assertEqual(self.sent, [])  # no second morning message (the review's A1)

    def test_when_the_turn_fails_the_background_sends_the_morning(self):
        from thirties_core.turns import BrainUnavailable
        with patch.object(self.m.brain, "respond", side_effect=BrainUnavailable("no network")):
            self.m.send_user_message("up and at it")
        self.assertFalse(morning.is_done(self.sched.state_db, DAY))  # never marked done before it went out
        self.assertFalse(any(str(m.get("content", "")).startswith("[System check") for m in self.m.brain.messages))
        mark_up(self.sched.state_db, at("09:25"))
        set_debug_time(at("09:40"))
        self.svc.rollover(at("09:40"))
        self.assertEqual([k for k, _ in self.sent].count("rollover"), 1)
        self.assertTrue(morning.is_done(self.sched.state_db, DAY))


class TestOnePolicy(unittest.TestCase):

    def test_the_prompt_the_tool_and_the_morning_say_the_same_thing(self):
        from thirties_core.day_proposal import MORNING_INSTRUCTION, PROPOSAL_TOOL_SPECS
        from tests.fixtures import make_scheduler as _ms
        from thirties_core.conversation import ConversationManager
        spec = next(s for s in PROPOSAL_TOOL_SPECS if s["name"] == "propose_day")
        self.assertIn(morning.POLICY, spec["description"])
        self.assertIn(morning.POLICY, MORNING_INSTRUCTION)
        sched = _ms()
        m = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=MockInferenceEngine(),
                                active_notes=[])
        prompt = m.brain.messages[0]["content"] if m.brain.messages else ""
        if not prompt:
            m.send_user_message("hi")
            prompt = m.brain.messages[0]["content"]
        self.assertIn(morning.POLICY, prompt)
        self.assertNotIn("on yes, apply_day_proposal", prompt)
