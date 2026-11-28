"""A reply about what an earlier turn did is never called a false claim.

Invented items only: a bike co-op's open-shop afternoon, at odd times. Each case scripts the model's draft and the
plan, and asserts that a true sentence passes with no retry and the reply unchanged, while a false one is still caught.

- Changes (FrontierBrain._claims_change): a clause about what a successful change call in an earlier turn made (a
  plan, list or goal change, with its item or goal name) is no claim about this turn; a draft saying this turn changed
  something with no call still gets the check.
- Dones (FrontierBrain._unmade_dones): an item a sentence names is the one whose whole title it says, never a longer
  item that shares its first word; an item done on the plan or recorded in an earlier turn is skipped; a draft saying
  this turn marked something done when nothing was logged still gets its line.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.frontier import FrontierBrain
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

LUBE, SAND, STENCIL, SIGN, TIRES = ("Lube the demo chains", "Sand the seat posts", "Stencil", "Stencil the new shop sign",
                                    "Inflate the display tires")


class OpenShop(ConversationTestCase):
    """Five jobs the user placed: lube, sand, a short stencil job, the longer sign job, tires."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("12:40"))
        spans = self.manager.tools.day_plan.spans
        for key, title, start, end in (("lube", LUBE, "13:10", "13:40"), ("sand", SAND, "14:20", "14:50"),
                                       ("stencil", STENCIL, "15:10", "15:40"), ("sign", SIGN, "16:20", "16:50"),
                                       ("tires", TIRES, "17:30", "18:00")):
            spans.append(EventSpan(f"shop-{key}", title, at(start), at(end), SpanCategory.TASK,
                                   given_start=at(start), given_end=at(end)))

    def span(self, key):
        return next(s for s in self.manager.tools.day_plan.spans if s.id == f"shop-{key}")

    def brain(self, messages=()):
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain.messages = list(messages)
        brain._turn_start = len(brain.messages)
        brain._status_before = brain._statuses()
        return brain

    def mark(self, key, said):
        t = self.manager.tools
        t.user_message = said
        try:
            return t.execute("log_outcome", {"event": f"shop-{key}", "status": "done"})
        finally:
            t.user_message = None

    @staticmethod
    def earlier(tool, arguments, result="Done."):
        """An earlier turn: the user's message, the model's call, its result and the reply."""
        return [{"role": "user", "content": "go ahead"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": tool, "arguments": arguments}]},
                {"role": "tool", "tool_call_id": "c1", "name": tool, "content": result},
                {"role": "assistant", "content": "Done."}]

    def turn(self, user, *model):
        self.engine.responses = list(model)
        self.engine.call_history.clear()
        out = self.manager.send_user_message(user)
        return out, len(self.engine.call_history)


class TestAClauseAboutAnEarlierChange(OpenShop):

    def test_each_kind_of_change_made_before_is_no_claim_now(self):
        for tool, arguments, said in (
                ("remove_event", {"event": LUBE}, "I removed Lube the demo chains from the plan back then."),
                ("update_event", {"event": "shop-sand", "start": "3:50 PM"},
                 "I moved Sand the seat posts later in the last message."),
                ("pencil_in", {"items": [TIRES]}, "I put Inflate the display tires under the if-there's-time part."),
                ("edit_list_item", {"item": "Weigh the donated frames", "rename_to": "Tag the donated frames"},
                 "I updated Tag the donated frames so it reads the way you said."),
                ("check_goal", {"goal": "Learn to true a wheel", "state": "dropped"},
                 "I removed Learn to true a wheel from your goals.")):
            with self.subTest(tool=tool):
                brain = self.brain(self.earlier(tool, arguments))
                self.assertFalse(brain._claims_change(said, brain._turn_start), said)

    def test_a_name_with_a_break_inside_it_still_matches(self):
        brain = self.brain(self.earlier("remove_event", {"event": "Wash, dry and shelve the spare tubes"}))
        said = "I cleared Wash, dry and shelve the spare tubes off today in that other message."
        self.assertFalse(brain._claims_change(said, brain._turn_start))

    def test_the_same_sentence_with_no_earlier_call_is_a_claim(self):
        brain = self.brain()
        self.assertTrue(brain._claims_change("I removed Lube the demo chains from the plan back then.", 0))

    def test_a_claim_about_another_item_is_still_a_claim(self):
        brain = self.brain(self.earlier("remove_event", {"event": LUBE}))
        self.assertTrue(brain._claims_change("I blocked out 3:40 PM for Sand the seat posts.", brain._turn_start))

    def test_a_later_clause_about_this_turn_is_still_a_claim(self):
        brain = self.brain(self.earlier("remove_event", {"event": LUBE}))
        said = "Earlier I cleared Lube the demo chains off your list; separately, I've placed Sand the seat posts at 3:40 PM."
        self.assertTrue(brain._claims_change(said, brain._turn_start))

    def test_a_call_that_was_refused_changed_nothing(self):
        brain = self.brain(self.earlier("remove_event", {"event": LUBE}, "NOT_DONE: nothing by that name."))
        self.assertTrue(brain._claims_change("I removed Lube the demo chains from the plan back then.",
                                             brain._turn_start))

    def test_a_lookup_changed_nothing(self):
        brain = self.brain(self.earlier("list_schedule", {"event": LUBE}, "1:10 PM Lube the demo chains"))
        self.assertTrue(brain._claims_change("I removed Lube the demo chains from the plan back then.",
                                             brain._turn_start))

    def test_only_the_calls_of_earlier_turns_count(self):
        messages = self.earlier("remove_event", {"event": LUBE})
        brain = self.brain(messages)
        brain._turn_start = 0  # the same messages as this turn's own
        self.assertTrue(brain._claims_change("I removed Lube the demo chains from the plan.", brain._turn_start))

    def test_all_fixed_over_an_earlier_changed_item_is_no_claim(self):
        brain = self.brain(self.earlier("remove_event", {"event": LUBE}))
        self.assertFalse(brain._claims_change("All fixed on Lube the demo chains, as of the last message.",
                                              brain._turn_start))
        self.assertTrue(brain._claims_change("All fixed!", brain._turn_start))


class TestThroughTheTurn(OpenShop):

    def test_a_true_reply_about_the_turn_before_costs_no_retry(self):
        out, _ = self.turn("take Lube the demo chains off today", reply('CALL remove_event {"event": "shop-lube"}'),
                           reply("It's off."))
        self.assertEqual(out, "It's off.")
        said = "I removed Lube the demo chains from your plan earlier, so the afternoon starts a bit later."
        out, asked = self.turn("what changed so far?", reply(said), reply(said), reply(said))
        self.assertEqual(out, said)
        self.assertEqual(asked, 1)

    def test_a_draft_claiming_another_change_with_no_call_is_still_sent_back(self):
        self.turn("take Lube the demo chains off today", reply('CALL remove_event {"event": "shop-lube"}'),
                  reply("It's off."))
        true = "Sand the seat posts stays at 2:20 PM for now. Want it moved?"
        out, asked = self.turn("anything shifting?", reply("I blocked out 3:40 PM for Sand the seat posts."), reply(true))
        self.assertEqual(out, true)
        self.assertEqual(asked, 2)

    def test_a_draft_claiming_a_change_with_no_call_at_all_is_still_sent_back(self):
        true = "The afternoon stands as it was. Should I slide anything?"
        out, asked = self.turn("how's the afternoon?", reply("I blocked out 3:40 PM for Sand the seat posts."),
                               reply(true))
        self.assertEqual(out, true)
        self.assertEqual(asked, 2)


class TestAShortItemIsNotALongOne(OpenShop):

    def test_a_short_item_marked_done_says_nothing_of_the_longer_one(self):
        self.mark("stencil", "Stencil is finished")
        self.assertEqual(self.brain()._unmade_dones("Stencil is marked done."), [])

    def test_the_longer_item_done_says_nothing_of_the_short_one(self):
        brain = self.brain()
        self.mark("sign", "Stencil the new shop sign is finished")
        self.assertEqual(brain._unmade_dones("Stencil the new shop sign is marked done."), [])

    def test_the_longer_item_not_done_gets_its_own_line_only(self):
        self.assertEqual(self.brain()._unmade_dones("Stencil the new shop sign is marked done."),
                         ["Stencil the new shop sign isn't recorded as done"])

    def test_the_short_item_not_done_gets_its_own_line_only(self):
        self.assertEqual(self.brain()._unmade_dones("Stencil is marked done."), ["Stencil isn't recorded as done"])

    def test_a_short_word_the_longer_item_starts_with_names_nothing_when_the_short_item_isnt_there(self):
        spans = self.manager.tools.day_plan.spans
        spans.remove(self.span("stencil"))
        self.assertEqual(self.brain()._unmade_dones("Stencil is marked done."), [])

    def test_two_items_in_a_sentence_are_each_read_by_their_whole_titles(self):
        brain = self.brain()
        self.mark("lube", "Lube the demo chains is finished")
        said = "Logged as finished: Lube the demo chains, plus Stencil."
        self.assertEqual(brain._unmade_dones(said), ["Stencil isn't recorded as done"])

    def test_both_the_short_and_the_longer_item_named_in_a_sentence_are_two_items(self):
        said = "Logged as finished: Stencil, plus Stencil the new shop sign."
        self.assertEqual(sorted(self.brain()._unmade_dones(said)),
                         ["Stencil isn't recorded as done", "Stencil the new shop sign isn't recorded as done"])

    def test_an_item_in_a_clause_with_its_own_action_is_not_the_dones(self):
        for said in ("Morning shift recorded as complete; Stencil the new shop sign got set at 3:10 PM.",
                     "Opening shift ticked off as finished, then I slid Sand the seat posts to 2:20 PM."):
            with self.subTest(said=said):
                self.assertEqual(self.brain()._unmade_dones(said), [])

    def test_a_list_of_items_marked_done_is_still_one_claim(self):
        said = "Inflate the display tires, Lube the demo chains and Sand the seat posts: all three marked done."
        self.assertEqual(sorted(self.brain()._unmade_dones(said)),
                         ["Inflate the display tires isn't recorded as done", "Lube the demo chains isn't recorded as done",
                          "Sand the seat posts isn't recorded as done"])

    def test_the_done_item_beside_another_action_still_gets_its_line(self):
        said = "Logged Inflate the display tires as finished, and set Lube the demo chains for 1:10 PM."
        self.assertEqual(self.brain()._unmade_dones(said), ["Inflate the display tires isn't recorded as done"])

    def test_through_the_turn_the_reply_has_no_parenthetical(self):
        said = "Stencil the new shop sign is marked done."
        out, asked = self.turn("Stencil the new shop sign is finished",
                               reply('CALL log_outcome {"event": "shop-sign", "status": "done"}'), reply(said))
        self.assertEqual(self.span("sign").status, SpanStatus.DONE)
        self.assertEqual(out, said)
        self.assertEqual(asked, 2)


class TestAnItemDoneBeforeIsNoFalseDone(OpenShop):

    def test_an_item_a_call_in_an_earlier_turn_recorded_is_skipped(self):
        messages = self.earlier("log_outcome", {"event": TIRES, "status": "done"})
        self.assertEqual(self.brain(messages)._unmade_dones("Inflate the display tires is marked done."), [])

    def test_an_item_already_done_on_the_plan_is_skipped(self):
        self.mark("tires", "Inflate the display tires is finished")
        self.assertEqual(self.brain()._unmade_dones("Inflate the display tires is marked done."), [])

    def test_an_earlier_call_that_was_refused_recorded_nothing(self):
        messages = self.earlier("log_outcome", {"event": TIRES, "status": "done"}, "NOT_DONE: not today.")
        self.assertEqual(self.brain(messages)._unmade_dones("Inflate the display tires is marked done."),
                         ["Inflate the display tires isn't recorded as done"])

    def test_a_draft_saying_this_turn_marked_something_with_nothing_logged_still_gets_its_line(self):
        for said in ("Inflate the display tires is marked done.", "I've marked Inflate the display tires as done."):
            with self.subTest(said=said):
                self.assertEqual(self.brain()._unmade_dones(said), ["Inflate the display tires isn't recorded as done"])

    def test_through_the_turn_a_false_done_is_still_sent_back(self):
        out, asked = self.turn("the display tires are inflated", reply("Marked Inflate the display tires done."),
                               reply('CALL log_outcome {"event": "shop-tires", "status": "done"}'),
                               reply("Marked Inflate the display tires done."))
        self.assertGreater(asked, 1)
        self.assertEqual(self.span("tires").status, SpanStatus.DONE)


if __name__ == "__main__":
    unittest.main()
