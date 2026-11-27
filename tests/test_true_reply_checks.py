"""The reply checks fire only on a claim that is false at the end of the turn.

Invented items only: a bike co-op's workshop day, at odd times. Each case scripts the model's draft and the plan, and
asserts that a true sentence passes with no retry and the reply unchanged, while a false one is still caught.

- Times (FrontierBrain._wrong_times): a block phrase names a block, not an item's time; a time an outcome records,
  as given or by its block, is the item's real time; one handled with no time known has none to be wrong about.
- Moves (_unmade_moves, time_claims.unmade_time_claims): an "is still at ...; I couldn't move it" line is only for an
  item still planned whose move the reply claims; a skipped, removed or done item never gets one; edits leave clean
  prose.
- Dones (_claims_outcome): only a draft saying this turn marked something done, with nothing logged for it and the
  item not already marked, is false.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core import clean_prose
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.frontier import FrontierBrain
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

TUNE, PATCH, TRUE, GREASE = "Tune the loaner bikes", "Patch the spare tubes", "True the wheel rims", "Grease the hubs"


class CoOpDay(ConversationTestCase):
    """The afternoon's four jobs, each placed by the user: tuning, tubes, rims, hubs."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("12:50"))
        spans = self.manager.tools.day_plan.spans
        for key, title, start, end in (("tune", TUNE, "13:20", "13:50"), ("patch", PATCH, "14:40", "15:10"),
                                       ("true", TRUE, "16:10", "16:40"), ("grease", GREASE, "17:40", "18:10")):
            # the user gave these times, so the schedule shows them to the minute
            spans.append(EventSpan(f"own-{key}", title, at(start), at(end), SpanCategory.TASK,
                                   given_start=at(start), given_end=at(end)))

    def span(self, key):
        return next(s for s in self.manager.tools.day_plan.spans if s.id == f"own-{key}")

    def mark(self, key, status="done", **times):
        """An outcome recorded in an earlier turn (the user's words say it)."""
        t = self.manager.tools
        t.user_message = f"{self.span(key).title} went {status}"
        try:
            return t.execute("log_outcome", {"event": f"own-{key}", "status": status, **times})
        finally:
            t.user_message = None

    def turn(self, user, *model):
        """The reply after a turn where the model answers with what's scripted (more of its last line follows, so a
        retry would show) and how many times the model was asked."""
        self.engine.responses = list(model) + [model[-1]] * 4
        self.engine.call_history.clear()
        out = self.manager.send_user_message(user)
        return out, len(self.engine.call_history)

    def stands(self, said, user="how does the afternoon look?"):
        out, asked = self.turn(user, reply(said))
        self.assertEqual(out, said)
        self.assertEqual(asked, 1, f"a true reply cost a retry: {said!r}")

    def times_wrong(self, said):
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain.messages = [{"role": "user", "content": "how does the afternoon look?"}]
        return brain._wrong_times(said, 0)


class TestABlockPhraseNamesABlock(CoOpDay):

    def test_where_the_user_is_with_another_item_ahead(self):
        for said in ("At the moment you're in the 1:00 PM block with Patch the spare tubes still waiting ahead.",
                     "At the moment you're in the 1:30 PM block: Patch the spare tubes comes up after that.",
                     "The 1:30 PM block has Tune the loaner bikes in it.",
                     "Right now it's the 2:00 – 2:30 PM block, and Grease the hubs waits for later."):
            with self.subTest(said=said):
                self.assertEqual(self.times_wrong(said), [])

    def test_such_a_reply_stands_with_no_retry(self):
        self.stands("At the moment you're in the 1:00 PM block with Patch the spare tubes still waiting ahead.")

    def test_a_wrong_clock_time_beside_a_block_phrase_is_still_caught(self):
        self.assertEqual(self.times_wrong("You're in the 1:00 PM block, though Patch the spare tubes sits at 3:40 PM according to me."),
                         [f"{PATCH} is at 2:40 PM"])
        out, asked = self.turn("I'm curious when the tube patching happens.", reply("Patch the spare tubes is at 3:40 PM."))
        self.assertGreater(asked, 1)
        self.assertNotIn("3:40", out)


class TestATimeAnOutcomeRecords(CoOpDay):

    def setUp(self):
        super().setUp()
        set_debug_time(at("15:30"))  # the tuning and the tubes are behind

    def test_an_item_marked_done_with_no_time_known_has_none_to_get_wrong(self):
        tune = self.span("tune")  # marked done with no time known: only its plan to go by
        tune.status, tune.time_source = SpanStatus.DONE, "planned"
        tune.actual_start, tune.actual_end = tune.start_dt, tune.end_dt
        self.assertEqual(self.times_wrong("Tune the loaner bikes was done about 3:20 PM, going by the shop clock."),
                         [])

    def test_a_skipped_item_has_no_time_to_get_wrong(self):
        self.mark("grease", "skipped")
        self.assertEqual(self.times_wrong("Skipping Grease the hubs leaves 5:00 PM open."), [])

    def test_an_item_left_unknown_has_no_time_to_get_wrong(self):
        self.span("grease").status = SpanStatus.UNKNOWN
        self.assertEqual(self.times_wrong("Grease the hubs stayed unmarked around 3:20 PM."), [])

    def test_the_times_it_happened_at_stand_as_given_and_by_block(self):
        self.mark("tune", started="1:10 PM", finished="1:40 PM")
        for said in ("Tune the loaner bikes ran 1:10 to 1:40.", "Tune the loaner bikes started at 1:00 PM.",
                     "Tune the loaner bikes ended at 1:40 PM.", "Tune the loaner bikes sat at 1:20 PM, the same as in the plan."):
            with self.subTest(said=said):
                self.assertEqual(self.times_wrong(said), [])

    def test_an_estimated_end_is_the_same_by_its_block_or_as_it_is(self):
        self.mark("tune", started="1:10 PM", finished="1:40 PM", estimated=True)
        for said in ("Tune the loaner bikes finished up at 1:40 PM by the shop clock.", "Tune the loaner bikes finished around 2:00 PM.",
                     "Tune the loaner bikes finished around 1:30 PM.", "1:30 PM Tune the loaner bikes"):
            with self.subTest(said=said):
                self.assertEqual(self.times_wrong(said), [])

    def test_a_wrong_time_for_a_done_item_is_corrected_to_when_it_happened(self):
        self.mark("tune", started="1:10 PM", finished="1:40 PM")
        self.assertEqual(self.times_wrong("Tune the loaner bikes was at 3:00 PM."), [f"{TUNE} is at 1:10 PM"])

    def test_a_reply_about_a_marked_item_stands_with_no_retry(self):
        self.mark("tune", started="1:10 PM", finished="1:40 PM")
        self.stands("That puts Tune the loaner bikes at 1:10 to 1:40 on the shop clock, with True the wheel rims as the following job.")


class TestNothingIsAddedThatWasntSaidWrong(CoOpDay):

    def test_the_check_tells_the_model_to_change_only_what_it_gave(self):
        self.turn("I'm curious when the tube patching happens.", reply("Patch the spare tubes is at 3:40 PM."))
        check = self.engine.call_history[1][-1]["content"]
        self.assertIn("Patch the spare tubes is at 2:40 PM", check)
        self.assertIn("add no time it didn't give", check)

    def test_a_reply_that_names_items_and_gives_their_times_right_is_left_alone(self):
        self.stands("Today runs Tune the loaner bikes from 1:20 PM, Patch the spare tubes from 2:40 PM and True the "
                    "wheel rims from 4:10 PM.")

    def test_a_reply_that_names_an_item_with_no_time_is_left_alone(self):
        self.stands("Patch the spare tubes comes after the tuning, and by the way the shop doors close at 7:00 PM sharp today.")


class TestAMoveSaidOfAnItemThatWasHandled(CoOpDay):
    """Something already dealt with (a skip, a removal, a done) never counts as a move that failed."""

    MOVE = reply('CALL update_event {"event": "Tune the loaner bikes", "start": "2:10 PM"}')

    def test_an_item_skipped_before_is_no_move_that_failed(self):
        self.mark("grease", "skipped")
        said = "I moved Tune the loaner bikes to 2:10 PM and Grease the hubs shifted too."
        out, asked = self.turn("tune at 2:10", self.MOVE, reply(said))
        self.assertEqual(out, said)
        self.assertEqual(asked, 2)

    def test_an_item_done_before_is_no_move_that_failed(self):
        self.mark("grease")
        said = "I moved Tune the loaner bikes to 2:10 PM and Grease the hubs shifted too."
        out, asked = self.turn("tune at 2:10", self.MOVE, reply(said))
        self.assertEqual(out, said)
        self.assertEqual(asked, 2)

    def test_an_item_taken_off_is_no_move_that_failed(self):
        self.manager.tools.user_message = "take the hubs off"
        self.manager.tools.execute("remove_event", {"event": "own-grease"})
        self.manager.tools.user_message = None
        said = "I moved Tune the loaner bikes to 2:10 PM and Grease the hubs shifted too."
        out, asked = self.turn("tune at 2:10", self.MOVE, reply(said))
        self.assertEqual(out, said)
        self.assertEqual(asked, 2)

    def test_a_time_said_set_for_a_skipped_item_is_no_refused_change(self):
        from thirties_core.time_claims import unmade_time_claims
        self.mark("grease", "skipped")
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain._times_before = brain._times()
        brain.messages = [
            {"role": "user", "content": "hubs at 6:20"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "update_event", "arguments": {"event": "own-grease", "start": "6:20 PM"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "update_event", "arguments": {"event": "own-grease"},
             "content": "NOT_DONE: that item is already marked."}]
        self.assertEqual(unmade_time_claims(brain, 1, "Grease the hubs ended up set for 6:20 PM, as requested."), [])

    def test_a_real_unmade_move_still_gets_its_line(self):
        out, asked = self.turn("tune at 2:10", reply("Moved Tune the loaner bikes over to 2:10 PM sharp for you."))
        self.assertGreater(asked, 1)
        self.assertIn("Tune the loaner bikes is still", out)
        self.assertIn("couldn't move it", out)

    def test_a_move_that_didnt_go_through_for_an_item_still_planned_gets_its_line(self):
        said = "Tube patching took the 3:10 PM slot; Grease the hubs I pushed to 7:05 PM."
        move = reply('CALL update_event {"event": "Patch the spare tubes", "start": "3:10 PM"}')
        out, asked = self.turn("Slide the tube job to 3:10 for me, then give the hub job a later start afterwards.", move, reply(said))
        self.assertIn("Grease the hubs is still", out)
        self.assertNotIn("Patch the spare tubes is still", out)


class TestAnEditLeavesCleanProse(CoOpDay):

    def test_a_lead_in_with_a_sentence_after_it_on_its_line_ends_as_a_sentence(self):
        out, changed = clean_prose.edit("Here's the new order: Oil the chains comes in at 4:25 PM.",
                                        lambda s: "Here's the new order:" if s.startswith("Here's") else None)
        self.assertTrue(changed)
        out, changed = clean_prose.edit("Done. Quick recap for the shop: see here. Oil the chains is still put.",
                                        lambda s: "Quick recap for the shop:" if s.startswith("Quick recap") else None)
        self.assertNotIn(":.", out)
        self.assertNotIn(": ", out)
        self.assertFalse(clean_prose.problems(out), out)

    def test_a_claim_over_a_list_leaves_no_stranded_colon(self):
        move = reply('CALL update_event {"event": "Patch the spare tubes", "start": "3:10 PM"}')
        for said in ("Your Patch the spare tubes job runs from 3:10 PM, while the later jobs got nudged along behind it: the last one, Grease the hubs, now starts 5:40 PM.",
                     "Patch the spare tubes is now at 3:10 PM, and the later jobs were bumped down the board:\n- True the wheel rims\n"
                     "- Grease the hubs",
                     "Board after the tube move reads thus: Grease the hubs 5:40 PM; True the wheel rims 4:10 PM."):
            with self.subTest(said=said):
                self.setUp()
                out, _ = self.turn("tubes at 3:10", move, reply(said))
                self.assertFalse(clean_prose.introduced(out, [said]), out)
                self.assertNotIn(":.", out)


class TestAnOutcomeClaim(CoOpDay):
    """Only a draft saying this turn marked something done, with nothing logged, is false."""

    def claims(self, said, messages=(), turn_start=None):
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain.messages = list(messages)
        return brain._claims_outcome(said, len(brain.messages) if turn_start is None else turn_start)

    def test_saying_it_isnt_marked_is_no_claim(self):
        for said in ("Patch the spare tubes isn't marked done yet, going by what the app holds.", "Neither Tune the loaner bikes nor Patch the "
                     "spare tubes is marked done.", "Nothing from today's list is ticked off so far.",
                     "Grease the hubs hasn't been marked as skipped.",
                     "Should I mark Patch the spare tubes done?", "If you want, I can mark it done once you're back.",
                     "Patch the spare tubes can be marked done whenever you say."):
            with self.subTest(said=said):
                self.assertFalse(self.claims(said), said)

    def test_an_item_already_marked_on_the_plan_says_nothing_of_this_turn(self):
        self.mark("grease", "skipped")
        self.mark("tune")
        for said in ("Grease the hubs is marked skipped, and True the wheel rims is next.",
                     "Tune the loaner bikes got marked done quite a while back this afternoon."):
            with self.subTest(said=said):
                self.assertFalse(self.claims(said), said)

    def test_a_tick_from_an_earlier_turn_says_nothing_of_this_turn(self):
        earlier = [{"role": "user", "content": "the parts wall is tidy"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": "g1", "name": "check_goal", "arguments": {"goal": "Tidy the parts wall"}}]},
                   {"role": "tool", "tool_call_id": "g1", "name": "check_goal", "content": "Ticked."},
                   {"role": "assistant", "content": "Ticked."}]
        self.assertFalse(self.claims("Tidy the parts wall is marked done from earlier.", earlier))
        self.assertTrue(self.claims("Tidy the parts wall is marked done from earlier.", earlier[:0]))

    def test_a_draft_saying_this_turn_marked_something_with_nothing_logged_is_false(self):
        for said in ("I marked Patch the spare tubes done.", "I've logged it as done, nice work today.", "Grease the hubs is ticked off as done."):
            with self.subTest(said=said):
                self.assertTrue(self.claims(said), said)

    def test_through_the_turn_a_not_marked_reply_costs_no_retry(self):
        self.stands("Patch the spare tubes isn't marked done yet, so it remains pending for now.",
                    user="is the tube patching done?")

    def test_through_the_turn_a_false_done_still_asks_for_the_call(self):
        out, asked = self.turn("tubes are patched", reply("Marked Patch the spare tubes done."),
                               reply('CALL log_outcome {"event": "Patch the spare tubes", "status": "done"}'),
                               reply("Marked Patch the spare tubes done."))
        self.assertGreater(asked, 1)
        self.assertEqual(self.span("patch").status, SpanStatus.DONE)


if __name__ == "__main__":
    unittest.main()
