"""What the user keeps "if there's time" (add_to_today with only_if_time) is the last part of the standard view, in
the reply to that very turn and in show_schedule's view: also while Joplin is closed and the line is still queued.
Invented print-shop items; the day is the Today fixture's (tests/test_today.py)."""

from tests.fixtures import at, morning_sent
from tests.test_conversation import reply
from tests.test_today import TodayTestCase
from thirties_core.astronomy import set_debug_time

KEEP = ["Recut the mat board", "Sort the ink drawer"]
CALL = {"name": "add_to_today", "arguments": {"items": KEEP, "only_if_time": True,
                                              "because": "recut the mat board and sort the ink drawer"}}
SAID = "recut the mat board and sort the ink drawer, both only if there's time"


class IfTimeInTheView(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("14:40"))
        self.sched.config.assistant.after_change = "rest"
        morning_sent(self.sched.state_db)
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)

    def turn(self, said, calls, answer):
        self.engine.responses = [{"role": "assistant", "content": "", "tool_calls": calls}, reply(answer)]
        return self.m.send_user_message(said)

    def assertEndsWithTheSection(self, out):
        self.assertTrue(out.rstrip().endswith("If there's time:\n" + "\n".join(KEEP)), out)
        self.assertEqual(out.count("If there's time:"), 1, out)

    def check(self):
        out = self.turn(SAID, [CALL], "Both are on the list, no set time.")
        self.assertEndsWithTheSection(out)
        out = self.turn("what's left today?", [{"name": "show_schedule", "arguments": {}}], "Here you go.")
        self.assertEndsWithTheSection(out)

    def test_with_joplin_open(self):
        self.check()

    def test_with_joplin_closed_the_queued_lines_still_show(self):
        self.api.ping = lambda: False
        self.check()
        self.assertNotIn("Recut the mat board", self.body("n_today"))  # still waiting for Joplin
