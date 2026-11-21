"""A done is for what the user said is done, and only for that.

- Asking why an item still shows as not marked, quoting the app's status ("not marked yet"), and saying "I did it
  at 6:40" is no denial: the done goes through. Saying it hasn't happened still holds it, and being in the
  middle of it is under way, not done (started.py).
- A done for an item the user's words don't point to is refused, and nothing is ticked in Joplin: the model has to
  ask which one they mean (done_reference.py). A tick in the app, and a request for many at once, go through.
"""

import unittest

from tests.fixtures import at, build_day
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.done_reference import prior_exchange
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.started import not_finished, said_not_done

OTHERS = ["Water the tomato seedlings", "Stretch routine", "Fold the towels"]


class TestAQuestionIsNoDenial(unittest.TestCase):

    def said(self, message, title="Water the tomato seedlings"):
        return not_finished(message, title, OTHERS)

    def test_asking_why_it_shows_not_marked_and_saying_it_was_done(self):
        for message in ("why do the seedlings and the stretch say 'not marked yet'?? I did them at 6:40",
                        'why do the seedlings still say "not marked yet"? did those at 6:40 pls fix',
                        "why do the seedlings still say “not marked yet”",
                        "how come the seedlings aren't marked done? I watered them first thing",
                        "the seedlings still show not done, I watered them at 6:40",
                        "the seedlings aren't ticked even though I did them",
                        "is the seedlings one not marked? I did it"):
            self.assertEqual(self.said(message), "", message)
            self.assertFalse(said_not_done(message, "Water the tomato seedlings", OTHERS), message)

    def test_saying_it_hasnt_happened_still_holds(self):
        for message in ("haven't watered the seedlings yet",
                        "didn't get to the seedlings",
                        "the seedlings didn't happen, sorry",
                        "I didn't water the seedlings, why does it say done?",
                        "not the seedlings yet, the towels are done"):
            self.assertEqual(self.said(message), "not", message)

    def test_under_way_is_begun(self):
        self.assertEqual(self.said("I'm doing the seedlings now"), "begun")
        self.assertEqual(self.said("I'm doing the stretch routine now", "Stretch routine"), "begun")

    def test_another_items_denial_is_not_this_ones(self):
        self.assertEqual(self.said("seedlings watered, haven't folded the towels yet"), "")
        self.assertEqual(self.said("seedlings watered, haven't folded the towels yet", "Fold the towels"), "not")


class GuardCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        sched, plan, pending = build_day()
        self.t = PlanTools(plan, pending, sched)
        for title, start in (("Water the tomato seedlings", "7:00 AM"), ("Stretch routine", "7:30 AM"),
                             ("Fold the towels", "8:00 AM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes"})

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, item, prior=None):
        self.t.user_message, self.t.prior_exchange = message, prior
        try:
            return self.t.execute("log_outcome", {"event": self.span(item).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange = None, None


class TestDoneFromTheUsersWords(GuardCase):

    def test_a_question_quoting_the_status_with_a_time_is_marked_done(self):
        message = "why do the seedlings and the stretch still say 'not marked yet'? I did both at 7:10, pls fix"
        for item in ("Water the tomato seedlings", "Stretch routine"):
            out = self.done(message, item)
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertEqual(self.span(item).status, SpanStatus.DONE)

    def test_not_done_yet_is_refused(self):
        out = self.done("haven't done the stretch routine yet", "Stretch routine")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Stretch routine").status, SpanStatus.PLANNED)

    def test_doing_it_now_is_under_way_not_done(self):
        out = self.done("I'm doing the stretch routine now", "Stretch routine")
        self.assertTrue(out.startswith(NOT_DONE) and "doing_now" in out, out)
        self.assertEqual(self.span("Stretch routine").status, SpanStatus.PLANNED)
        out = self.done("I'm in the middle of it", "Stretch routine", ("", "Next up: Stretch routine at 7:30."))
        self.assertTrue(out.startswith(NOT_DONE) and "doing_now" in out, out)  # "it" is the stretch

    def test_an_item_the_user_never_mentioned_is_refused(self):
        message = "why do the seedlings and the stretch still say 'not marked yet'? I did both at 7:10"
        out = self.done(message, "Fold the towels")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("ask which", out)
        self.assertEqual(self.span("Fold the towels").status, SpanStatus.PLANNED)

    def test_pointing_back_at_what_was_just_shown(self):
        asked = ("", "Did you get to Fold the towels?")
        self.assertFalse(self.done("yes, done", "Fold the towels", asked).startswith(NOT_DONE))

    def test_pointing_back_after_a_list_is_the_models_pick(self):
        listed = ("", "Not marked yet:\nStretch routine\nFold the towels")
        self.assertFalse(self.done("done", "Fold the towels", listed).startswith(NOT_DONE))  # the one it picked
        self.assertFalse(self.done("both done", "Fold the towels", listed).startswith(NOT_DONE))
        self.assertFalse(self.done("both done", "Stretch routine", listed).startswith(NOT_DONE))

    def test_both_means_two(self):
        listed = ("", "7:00 Water the tomato seedlings\n7:30 Stretch routine\n8:00 Fold the towels")
        self.assertTrue(self.done("did both", "Fold the towels", listed).startswith(NOT_DONE))

    def test_what_the_user_said_just_before(self):
        said = ("folding the towels now", "")
        self.assertFalse(self.done("finished", "Fold the towels", said).startswith(NOT_DONE))

    def test_by_its_id(self):
        towels = self.span("Fold the towels")
        self.assertFalse(self.done(f"{towels.id} is done", "Fold the towels").startswith(NOT_DONE))

    def test_a_request_for_many_at_once(self):
        message = "mark everything before 8:30 done"
        for item in ("Water the tomato seedlings", "Stretch routine", "Fold the towels"):
            self.assertFalse(self.done(message, item).startswith(NOT_DONE), item)

    def test_a_tick_in_the_app_and_the_background_are_never_held(self):
        self.t.by_hand = True
        try:
            self.assertFalse(self.done("why does the stretch say not marked", "Fold the towels").startswith(NOT_DONE))
        finally:
            self.t.by_hand = False
        self.assertFalse(self.t.execute("log_outcome", {"event": self.span("Stretch routine").id,
                                                        "status": "done"}).startswith(NOT_DONE))  # nobody talking

    def test_the_exchange_before_this_turn(self):
        messages = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": "plan my morning"},
                    {"role": "assistant", "content": "", "tool_calls": [{"name": "add_event"}]},
                    {"role": "assistant", "content": "Here's the morning."},
                    {"role": "user", "content": "[System check: …]"},
                    {"role": "assistant", "content": "Did you get to Fold the towels?"},
                    {"role": "user", "content": "hidden instruction", "hidden": True}]
        self.assertEqual(prior_exchange(messages), ("plan my morning", "Did you get to Fold the towels?"))


class TestNoJoplinTickForAnItemNotNamed(ListsTestCase):
    """Data guard: a done the user's words don't point to never ticks anything in Joplin."""

    def setUp(self):
        super().setUp()
        self.tasks = {t.title: t for t in self.services.tasks}
        for title, start in (("Call the bank", "12:30 PM"), ("Buy stamps", "1:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                         "task_id": self.tasks[title].id})

    def done(self, message, title):
        span = next(s for s in self.t.day_plan.spans if s.title == title)
        self.t.user_message = message
        try:
            return self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        finally:
            self.t.user_message = None

    def test_only_the_named_item_is_ticked(self):
        message = "why does call the bank still say 'not marked yet'? I did it at 12:40"
        writes = len(self.api.updates)
        out = self.done(message, "Buy stamps")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.api.updates), writes)  # nothing written to Joplin
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))
        out = self.done(message, "Call the bank")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [x] Call the bank", self.body("n_errands"))
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))

    def test_a_leftover_marked_done_needs_naming_too(self):
        from thirties_core.done_reference import check
        self.t.user_message = "the bank call is done"
        try:
            held, _ = check(self.t, "Buy stamps", [self.tasks["Buy stamps"].id])
            self.assertTrue(held)
            held, _ = check(self.t, "Call the bank", [self.tasks["Call the bank"].id])
            self.assertIsNone(held)
        finally:
            self.t.user_message = None


if __name__ == "__main__":
    unittest.main()
