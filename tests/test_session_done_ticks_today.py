"""A done the plan accepted ticks the item's Today line too, so the plan and Joplin agree.

Writing a tick to Joplin in a turn needs the message to report the item finished. When
log_outcome has already accepted a done for the item this turn (its own checks read the user's words and found the
item finished, or a session over), the tick of that item's Today line follows the plan. A tick with no done
accepted on the plan still needs the user's words.

Invented: someone who builds moss terrariums in a spare room."""

from tests.fixtures import at
from tests.test_today import TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import JoplinWriter, current_turn
from thirties_core.models import SpanStatus
from thirties_core.write_guard import refused

WORK = "Terrarium work"


class SessionDoneTest(TodayTestCase):

    def say(self, message, tool, args):
        self.t.user_message = message
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute(tool, args)
        finally:
            current_turn.reset(token)
            self.t.user_message = None

    def setUp(self):
        super().setUp()
        set_debug_time(at("13:10"))
        self.say("terrarium work now till 3:10", "add_event", {"title": WORK, "start": "1:10 PM", "end": "3:10 PM"})
        self.assertIn(f"- [ ] {WORK}\n", self.body("n_today"))  # added during the day: on Today's list too
        set_debug_time(at("15:40"))

    def span(self):
        return next(s for s in self.t.day_plan.spans if s.title == WORK)

    def test_an_accepted_session_done_ticks_its_today_line(self):
        said = "terrarium from 1:10 to 3:40, stopping now to read for a bit"
        self.assertIn("isn't ticked", refused({"op": "complete", "is_checkbox_item": True, "note_id": "n_today",
                                               "title": WORK}, f"[app] {said}", self.sched.state_db))  # words alone
        out = self.say(said, "log_outcome", {"event": WORK, "status": "done", "started": "1:10 PM",
                                             "finished": "3:40 PM"})
        self.assertEqual(self.span().status, SpanStatus.DONE, out)
        self.assertIn(f"- [x] {WORK}\n", self.body("n_today"))
        self.assertNotIn(f"- [ ] {WORK}\n", self.body("n_today"))

    def test_a_tick_with_no_done_on_the_plan_and_no_words_is_still_refused(self):
        writer = JoplinWriter(self.sched.config, self.sched.state_db, self.api)
        token = current_turn.set("[app] stopping now to read for a bit")
        try:
            status, why = writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n_today",
                                         "title": WORK})
        finally:
            current_turn.reset(token)
        self.assertEqual(status, "failed")
        self.assertIn("isn't ticked", why)
        self.assertIn(f"- [ ] {WORK}\n", self.body("n_today"))

    def test_a_tick_that_follows_a_done_on_the_plan_is_written(self):
        op = {"op": "complete", "is_checkbox_item": True, "note_id": "n_today", "title": WORK}
        self.assertIn("isn't ticked", refused(op, "[app] stopping now to read for a bit", self.sched.state_db))
        self.assertEqual(refused({**op, "_done_on_plan": True}, "[app] stopping now to read for a bit",
                                 self.sched.state_db), "")
