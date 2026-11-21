"""A bare no takes back only the done the reply just before announced.

- **Right after the tick, it takes it back.** "Marked Calibrate the pipettes done." then "no": the item opens again,
  on the plan and in Joplin, however long Python's "Not marked yet" list under that reply is ("undo" too).
- **After a question about something else, it answers that.** "Marked Calibrate the pipettes done. Want me to move
  Label the sample racks to 2:50 PM?" then "no" is no to the move: the done stays, on the plan and in Joplin. So
  does "Anything else?".
- **A question about the item itself is still about it.** "Was Calibrate the pipettes the one you meant?" then "no"
  takes it back.

Through the real tool path on the mock Joplin API and through whole turns with a scripted model (no real calls).
Fixtures are invented: a lab's morning.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import reply
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE
from thirties_core.reopen import reply_before

PIPETTES, GLASSWARE, RACKS, FREEZER, GLOVES, HOOD = (
    "Calibrate the pipettes", "Autoclave the glassware", "Label the sample racks", "Log the freezer readings",
    "Restock the nitrile gloves", "Wipe down the fume hood")
ITEMS = ((PIPETTES, "8:15 AM", "t_pipettes"), (GLASSWARE, "8:45 AM", "t_glassware"), (RACKS, "9:15 AM", "t_racks"),
         (FREEZER, "9:45 AM", "t_freezer"), (GLOVES, "10:15 AM", "t_gloves"), (HOOD, "10:45 AM", "t_hood"))
MARKED = f"Marked {PIPETTES} done."
ELSEWHERE = (f"{MARKED} Want me to move {RACKS} to 2:50 PM?", f"{MARKED} Anything else?")
NO = ("no", "nope", "not that one")


class LabMorning(TodayTestCase):
    """Six lab items from one list, each on the plan and past its time; the pipettes are ticked."""

    def setUp(self):
        super().setUp()
        TITLES["n_lab"] = "Lab bench"
        self.addCleanup(TITLES.pop, "n_lab", None)
        self.api.notes["n_lab"] = {"id": "n_lab", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_lab", "list", "Lab bench")
        self.services.tasks[:] = [item(tid, title, "n_lab") for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        set_debug_time(at("11:20"))
        self.t.by_hand = True
        self.t.execute("log_outcome", {"event": self.span(PIPETTES).id, "status": "done"})
        self.t.by_hand = False

    def span(self, title, plan=None):
        return next(s for s in (plan or self.t.day_plan).spans if s.title == title)

    def ticked(self, title):
        return f"[x] {title}" in self.body("n_lab")

    def reopen(self, message, shown):
        """The model calls reopen_item during the user's turn, right after the reply ``shown``."""
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        token, before = current_turn.set(f"[app] {message}"), reply_before.set(shown)
        try:
            return str(self.t.execute("reopen_item", {"item": PIPETTES}))
        finally:
            current_turn.reset(token)
            reply_before.reset(before)
            self.t.user_message, self.t.prior_exchange = None, None

    def with_not_marked(self, text):
        """The reply as the user reads it, with Python's Not marked yet list under it (whats_next.py)."""
        from thirties_core.messages import join_sections
        from thirties_core.whats_next import unmarked_lines
        behind = [s for s in self.t.day_plan.spans if s.title != PIPETTES]
        return join_sections(text, "\n".join(unmarked_lines(behind)))

    def fresh(self):
        self.doCleanups()
        self.setUp()


class TestRightAfterTheTick(LabMorning):

    def test_a_bare_no_takes_it_back(self):
        for message in NO:
            with self.subTest(message=message):
                self.fresh()
                out = self.reopen(message, MARKED)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(PIPETTES).status, SpanStatus.PLANNED, out)
                self.assertFalse(self.ticked(PIPETTES), out)

    def test_however_long_the_not_marked_list_under_it(self):
        shown = self.with_not_marked(MARKED)
        self.assertGreater(sum(t in shown for t, _, _ in ITEMS), 4, shown)
        for message in NO + ("undo", "put it back"):
            with self.subTest(message=message):
                self.fresh()
                out = self.reopen(message, self.with_not_marked(MARKED))
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(PIPETTES).status, SpanStatus.PLANNED, out)
                self.assertFalse(self.ticked(PIPETTES), out)

    def test_a_question_about_the_item_itself(self):
        out = self.reopen("no", f"{MARKED} Was {PIPETTES} the one you meant?")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(PIPETTES).status, SpanStatus.PLANNED, out)


class TestAfterAQuestionAboutSomethingElse(LabMorning):

    def test_the_done_stays(self):
        for shown in ELSEWHERE + tuple(self.with_not_marked(s) for s in ELSEWHERE):
            for message in NO:
                with self.subTest(shown=shown, message=message):
                    self.fresh()
                    out = self.reopen(message, shown)
                    self.assertTrue(out.startswith(NOT_DONE), out)
                    self.assertEqual(self.span(PIPETTES).status, SpanStatus.DONE, out)
                    self.assertTrue(self.ticked(PIPETTES), out)

    def test_joplin_keeps_the_line_ticked(self):
        from thirties_core.write_guard import refused
        op = {"op": "untick", "title": PIPETTES, "note_id": "n_lab"}
        for shown in ELSEWHERE:
            before = reply_before.set(shown)
            try:
                for message in NO:
                    self.assertTrue(refused(op, f"[app] {message}", None), (shown, message))
            finally:
                reply_before.reset(before)
        self.assertTrue(refused(op, "[app] no", None), "no reply before")


class TestWholeTurns(LabMorning):
    """The tick and the no as whole turns with a scripted model: the conversation gives the check the reply."""

    def setUp(self):
        super().setUp()
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)

    def turn(self, message, calls, answer):
        self.engine.responses = [{"role": "assistant", "content": "", "tool_calls": calls}, reply(answer)]
        before = len(self.m.brain.messages)
        self.m.send_user_message(message)
        return [str(x.get("content")) for x in self.m.brain.messages[before:] if x.get("role") == "tool"]

    def status(self):
        return self.span(PIPETTES, self.m.day_plan).status

    def test_no_after_the_announcement(self):
        self.m.brain.messages.append({"role": "assistant", "content": MARKED})
        results = self.turn("nope", [{"name": "reopen_item", "arguments": {"item": PIPETTES}}],
                            f"Okay, {PIPETTES} is open again.")
        self.assertEqual(self.status(), SpanStatus.PLANNED, results)
        self.assertFalse(self.ticked(PIPETTES), results)
        self.assertEqual(reply_before.get(), "")

    def test_no_after_a_question_about_a_move(self):
        self.m.brain.messages.append({"role": "assistant", "content": ELSEWHERE[0]})
        results = self.turn("no", [{"name": "reopen_item", "arguments": {"item": PIPETTES}}], "Okay.")
        self.assertTrue(any(r.startswith(NOT_DONE) for r in results), results)
        self.assertEqual(self.status(), SpanStatus.DONE, results)
        self.assertTrue(self.ticked(PIPETTES), results)


if __name__ == "__main__":
    unittest.main()
