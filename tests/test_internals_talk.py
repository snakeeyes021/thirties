"""A reply never talks about the app's own workings: CALL lines, tool or function calls, commands the model says it
can't run. The model is asked once for plain words; whatever it still says about them is taken out, and a reply that
was nothing else says plainly that the plan didn't change. Invented: a pottery studio shift and a canoe outing."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.internal_text import has_internal, strip_internal, talks_internals
from thirties_core.models import EventSpan, SpanCategory

REFUSALS = (
    "Shifting the glaze session needs a CALL line, which I can't write here.",
    "Sorry, my tools won't let me move the canoe outing.",
    "I'm not able to run that change as a function call right now.",
    "The update_event tool rejected that, so the kiln load stays put.",
    "The system returned an error when I tried to move it.",
)


class TestTheGuard(unittest.TestCase):

    def test_talk_about_the_workings_is_found_and_taken_out(self):
        for said in REFUSALS:
            self.assertTrue(talks_internals(said), said)
            self.assertTrue(has_internal(said), said)
            self.assertEqual(strip_internal("Good luck at the wheel! " + said), "Good luck at the wheel!", said)

    def test_the_users_own_words_stay(self):
        for said in ("Call the gallery about the kiln at 2.",
                     "Pack the clay tools before your shift.",
                     "The trimming tools are on today's list.",
                     "Your call with the canoe club is at 4:00 PM.",
                     "Give the studio a call when the firing is done."):
            self.assertFalse(talks_internals(said), said)
            self.assertEqual(strip_internal(said), said)

    def test_the_day_simulation_counts_it(self):
        from tests.day_sim import LEAK_RE
        self.assertTrue(LEAK_RE.search("I can only do that with CALL commands."))
        self.assertTrue(LEAK_RE.search("That needs a tool call."))
        self.assertFalse(LEAK_RE.search("Call the gallery at 2."))


class TestInATurn(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:10"))
        self.addCleanup(set_debug_time, None)

    def manager(self, bookkeeping):
        sched = make_scheduler()
        sched.config.assistant.turn_bookkeeping = bookkeeping
        sched.state_db.upsert_span(EventSpan("glz", "Glaze the vases", at("17:00"), at("17:30"),
                                             SpanCategory.ACTIVITY))
        plan, pending = sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        return ConversationManager(plan, [], pending, sched, inference_engine=self.engine, active_notes=[])

    def test_sent_back_once_for_plain_words(self):
        for bookkeeping in (False, True):
            m = self.manager(bookkeeping)
            self.engine.set_next_response(reply(REFUSALS[0]))
            self.engine.set_next_response(reply("I couldn't move the glazing; it's still at 5:00 PM. Want me to try "
                                                "6:00 PM instead?"))
            out = m.send_user_message("Is there room to push the vase glazing toward 6?")
            self.assertIn("still at 5:00 PM", out, bookkeeping)
            self.assertFalse(talks_internals(out), out)
            checks = [x["content"] for x in m.messages if str(x.get("content") or "").startswith("[System check")]
            self.assertTrue(any("calls, tools or commands" in c for c in checks), checks)

    def test_said_again_it_never_reaches_the_user(self):
        for bookkeeping in (False, True):
            m = self.manager(bookkeeping)
            for _ in range(8):
                self.engine.set_next_response(reply(REFUSALS[1]))
            out = m.send_user_message("Could vase glazing slide to after the canoe trip?")
            self.assertFalse(talks_internals(out), out)
            self.assertNotIn("tools", out)
            self.assertTrue(out.strip(), bookkeeping)


if __name__ == "__main__":
    unittest.main()
