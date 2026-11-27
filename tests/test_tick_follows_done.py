"""A done the plan accepted ticks the item's list lines too, and says so when a line stays open.

- **One decision.** When log_outcome accepts a done for an item, both its list line and its Today line follow that
  decision, also when the user's last message only named the item in answer to the app's "which one?".
- **A raw tick keeps the old floors.** A bare complete op, with no accepted done behind it, is
  refused when nothing the user said is a done.
- **Never silent.** A line left open (refused, not found, no permission) is named in log_outcome's result:
  the item is marked done here, and the result says which line stayed open and why.

Through the real tool path on the mock Joplin API. Fixtures are invented: a volunteer bike co-op.
"""

from unittest.mock import patch

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import JoplinWriter, current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.write_guard import refused

HUBS, TUBES = "Regrease the loaner hubs", "Sort the spare tube bin"
SHOWN = f"Which one did you finish: {HUBS} or {TUBES}?"
REPLY = "the tube bin"


class CoopDay(TodayTestCase):
    """Two co-op jobs from one list, each on the plan and on Today."""

    def setUp(self):
        super().setUp()
        TITLES["n_coop"] = "Co-op jobs"
        self.addCleanup(TITLES.pop, "n_coop", None)
        self.api.notes["n_coop"] = {"id": "n_coop", "body": f"- [ ] {HUBS}\n- [ ] {TUBES}\n"}
        ListCatalog(self.sched.state_db).set_role("n_coop", "list", "Co-op jobs")
        self.services.tasks[:] = [item("t_hubs", HUBS, "n_coop"), item("t_tubes", TUBES, "n_coop")]
        for t in self.services.tasks:
            t.list_role = "list"
        self.t.execute("add_event", {"title": HUBS, "start": "9:20 AM", "duration": "20 min", "task_id": "t_hubs"})
        self.t.execute("add_event", {"title": TUBES, "start": "9:50 AM", "duration": "20 min", "task_id": "t_tubes"})
        self.assertIn(f"- [ ] {TUBES}\n", self.body("n_today"))
        set_debug_time(at("11:40"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def reply_to_which(self, message=REPLY, title=TUBES):
        """The model calls log_outcome during a turn whose only words name the item, in answer to "which one?"."""
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", SHOWN), None
        token = current_turn.set(f"[app] {message}")
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"}))
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None


class TestTickFollowsTheAcceptedDone(CoopDay):

    def test_a_done_accepted_after_a_which_question_ticks_both_lines(self):
        out = self.reply_to_which()
        self.assertEqual(self.span(TUBES).status, SpanStatus.DONE, out)
        self.assertIn(f"- [x] {TUBES}\n", self.body("n_coop"))
        self.assertIn(f"- [x] {TUBES}\n", self.body("n_today"))
        self.assertNotIn("stays open in Joplin", out)
        self.assertIn(f"- [ ] {HUBS}\n", self.body("n_coop"))  # the other one stays open

    def test_a_raw_complete_op_for_the_same_line_still_meets_the_words_floor(self):
        self.t.prior_exchange = ("", SHOWN)
        for note in ("n_coop", "n_today"):
            op = {"op": "complete", "is_checkbox_item": True, "note_id": note, "title": TUBES}
            self.assertIn("isn't ticked", refused(op, f"[app] {REPLY}", self.sched.state_db), note)
            self.assertEqual(refused({**op, "_done_on_plan": True}, f"[app] {REPLY}", self.sched.state_db), "")
        writer = JoplinWriter(self.sched.config, self.sched.state_db, self.api)
        token = current_turn.set(f"[app] {REPLY}")
        try:
            status, why = writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n_coop",
                                         "title": TUBES})
        finally:
            current_turn.reset(token)
        self.assertEqual(status, "failed")
        self.assertIn("isn't ticked", why)
        self.assertIn(f"- [ ] {TUBES}\n", self.body("n_coop"))


class TestAFailedTickIsReported(CoopDay):

    def failing_on(self, note_id, reason):
        real = JoplinWriter._apply

        def apply(writer, op):
            if op.get("note_id") == note_id:
                return False, f"gone: {reason}"
            return real(writer, op)
        return patch.object(JoplinWriter, "_apply", apply)

    def test_a_today_line_that_cant_be_ticked_is_named_in_the_result(self):
        with self.failing_on("n_today", "that line isn't on the note any more."):
            out = self.reply_to_which()
        self.assertEqual(self.span(TUBES).status, SpanStatus.DONE, out)
        self.assertIn("Marked done here; its Today line stays open in Joplin, reason: ", out)
        self.assertIn("that line isn't on the note any more", out)
        self.assertIn(f"- [x] {TUBES}\n", self.body("n_coop"))  # the list line went through
        self.assertIn(f"- [ ] {TUBES}\n", self.body("n_today"))

    def test_a_list_line_that_cant_be_ticked_is_named_in_the_result(self):
        with self.failing_on("n_coop", "that line isn't on the list any more."):
            out = self.reply_to_which()
        self.assertEqual(self.span(TUBES).status, SpanStatus.DONE, out)
        self.assertIn("its list line stays open in Joplin", out)
        self.assertIn("any more", out)
        self.assertIn(f"- [ ] {TUBES}\n", self.body("n_coop"))

    def test_joplin_being_closed_is_not_a_failure(self):
        self.api.up = False  # the ticks wait in the outbox and go through when Joplin is open
        out = self.reply_to_which()
        self.assertEqual(self.span(TUBES).status, SpanStatus.DONE, out)
        self.assertNotIn("stays open in Joplin", out)
        self.assertIn("as soon as Joplin is open", out)

    def test_a_tick_not_allowed_is_named_in_the_result(self):
        self.sched.config.joplin.write_back = False
        out = self.reply_to_which()
        self.assertEqual(self.span(TUBES).status, SpanStatus.DONE, out)
        self.assertIn("its list line stays open in Joplin", out)
        self.assertIn("no permission", out)
