"""A goal marked done counts as recorded, and only an item the reply names in full is held to a done.

- **check_goal is a record.** The user says two near-term goals are done; the model calls check_goal (done) for
  both and says so. The reply goes out as written: no check that nothing was logged, and no plan item it didn't
  name is called "not recorded as done".
- **The plan is untouched.** A plan item sharing a word with a goal stays as it was.
- **A done claimed for a plan item by its whole title is still checked.**

Through whole turns with a scripted model (no real calls). Fixtures are invented: a workshop's goals.
"""

import unittest

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

LATHE, HANDLE = "Oil the lathe", "Carve the bellows handle"
BLOCK, KITE = "Carve a linocut block", "Fly the box kite"
SAID = f"Logged {BLOCK} and {KITE} as finished goals."


class WorkshopGoals(ConversationTestCase):
    """A plan of two items, and a profile holding two goals to check."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:10"))
        from thirties_core import morning  # the day's first message was earlier: this turn isn't the morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("08:10"), "in_conversation")
        spans = self.manager.tools.day_plan.spans
        spans.append(EventSpan("own-lathe", LATHE, at("09:40"), at("10:10"), SpanCategory.TASK))
        spans.append(EventSpan("own-handle", HANDLE, at("10:40"), at("11:10"), SpanCategory.TASK))
        self.profile = self.manager.tools.assistant.s.profile
        self.profile.set_section("Near-term goals", f"- {BLOCK}\n- {KITE}")

    def turn(self, message, calls, *answers):
        before = len(self.manager.brain.messages)
        self.engine.responses = ([{"role": "assistant", "content": "", "tool_calls": calls}] if calls else []) + [
            reply(a) for a in answers]
        out = self.manager.send_user_message(message)
        new = self.manager.brain.messages[before:]
        checks = [str(m.get("content")) for m in new if str(m.get("content") or "").startswith("[System check")]
        results = [str(m.get("content")) for m in new if m.get("role") == "tool"]
        return out, checks, results

    def plan(self):
        return {s.title: (s.status, s.start_dt) for s in self.manager.tools.day_plan.spans
                if s.title in (LATHE, HANDLE)}


class TestAGoalMarkedDone(WorkshopGoals):

    def test_two_goals_done_pass_with_no_check(self):
        before = self.plan()
        calls = [{"name": "check_goal", "arguments": {"goal": BLOCK, "state": "done"}},
                 {"name": "check_goal", "arguments": {"goal": KITE, "state": "done"}}]
        out, checks, results = self.turn("The linocut block and the box kite are both done.", calls,
                                         SAID, SAID, SAID)
        self.assertTrue(all("(done)" in r for r in results), results)
        self.assertEqual(checks, [])
        self.assertEqual(out, SAID)
        self.assertNotIn(BLOCK, self.profile.sections().get("Near-term goals", ""))
        self.assertNotIn(KITE, self.profile.sections().get("Near-term goals", ""))
        self.assertEqual(self.plan(), before)
        self.assertEqual(self.plan()[HANDLE][0], SpanStatus.PLANNED)

    def test_a_plan_item_named_in_full_is_still_held_to_its_done(self):
        out, checks, _ = self.turn("thanks", [], f"Marked {HANDLE} done.", f"Marked {HANDLE} done.")
        self.assertTrue(any(f"{HANDLE} isn't recorded as done" in c for c in checks), checks)
        self.assertEqual(self.plan()[HANDLE][0], SpanStatus.PLANNED)


if __name__ == "__main__":
    unittest.main()
