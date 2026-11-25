"""A reply can't say the whole plan is fixed when nothing changed; an item comes off the plan only on the user's
words for it; and adding what's already planned keeps it one item.

Seen in a simulated day: "All fixed" and "everything is back in place" over a plan that hadn't changed, items
taken off that the user had only complained about, and items put on again after that.
"""

import unittest

from tests.test_conversation import ConversationTestCase, reply
from thirties_core.own_up import claims_all_fixed
from thirties_core.plan_tools import NOT_DONE


class TestBlanketClaims(unittest.TestCase):
    def test_what_counts(self):
        for text in ("All fixed!", "Everything is back in place exactly as you laid out.", "It's all sorted.",
                     "Everything is right where you want it.", "I've fixed it.", "All back the way you had it."):
            self.assertTrue(claims_all_fixed(text), text)
        for text in ("Everything was already where you had it.", "Is everything where you want it?",
                     "Everything else is still as it was.", "The fern is at 2:00."):
            self.assertFalse(claims_all_fixed(text), text)


class PlanCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        for title, start in (("Oil change", "1:00 PM"), ("Repot the fern", "2:00 PM"), ("Bake bread", "5:00 PM")):
            out = self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)
        self.pt = self.manager.tools

    def planned(self, title):
        return [s for s in self.manager.day_plan.spans if s.title == title]


class TestAllFixedWithNothingChanged(PlanCase):
    def test_it_is_sent_back_and_the_true_reply_is_kept(self):
        self.engine.responses = [reply("Everything is back in place exactly as you laid out."),
                                 reply("Nothing on the plan has changed yet: the fern is still at 2:00 and bread at 5:00.")]
        out = self.manager.send_user_message("put my day back the way i wrote it pls")
        self.assertEqual(out, "Nothing on the plan has changed yet: the fern is still at 2:00 and bread at 5:00.")

    def test_after_calls_that_changed_nothing(self):
        self.engine.responses = [reply('CALL update_event {"event": "Bake bread", "start": "5:00 PM"}'),
                                 reply("All fixed: everything is right where you want it."),
                                 reply("Bake bread was already at 5:00, so nothing needed to change.")]
        out = self.manager.send_user_message("bread at 5 like i said")
        self.assertEqual(out, "Bake bread was already at 5:00, so nothing needed to change.")

    def test_insisting_never_reaches_the_user(self):
        self.engine.responses = [reply("All fixed!")] * 6
        out = self.manager.send_user_message("fix my day")
        self.assertNotIn("All fixed", out)

    def test_saying_it_was_already_so_is_fine(self):
        self.engine.responses = [reply("Everything was already where you had it: the fern at 2:00, bread at 5:00.")]
        out = self.manager.send_user_message("is it all where i wrote it?")
        self.assertEqual(out, "Everything was already where you had it: the fern at 2:00, bread at 5:00.")


class TestRemovalNeedsTheUsersWords(PlanCase):
    def remove(self, title, said, shown=""):
        self.pt.user_message = said
        self.pt.prior_exchange = ("", shown)
        try:
            return self.pt.execute("remove_event", {"event": title})
        finally:
            self.pt.user_message = None
            self.pt.prior_exchange = None

    def test_a_complaint_about_where_it_is_is_the_models_reading(self):
        # words about the item are a basis: whether they mean taking it off is the model's reading, with a note
        # so it can put the item back where the user wants it (change_asks.removal_note)
        out = self.remove("Oil change", "how come the oil change got shoved to tonight?? not what i asked")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Note:", out)
        self.assertEqual(len(self.planned("Oil change")), 0)
        out = self.remove("Repot the fern", "how come the oil change got shoved to tonight?? not what i asked")
        self.assertFalse(out.startswith(NOT_DONE), out)  # today: which item is the model's reading
        self.assertIn("Removed Repot the fern", out)

    def test_asked_for_a_removal_which_item_is_the_models_reading(self):
        # which items a removal the user asked for means is the model's to read (change_asks.py)
        self.assertFalse(self.remove("Repot the fern", "drop the fern please").startswith(NOT_DONE))
        self.assertEqual(self.planned("Repot the fern"), [])
        self.assertFalse(self.remove("Bake bread", "drop the fern please").startswith(NOT_DONE))
        self.assertEqual(self.planned("Bake bread"), [])

    def test_on_today_the_models_reading_stands(self):
        out = self.remove("Bake bread", "the fern looks happier already")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Removed Bake bread", out)

    def test_asked_for_by_name_or_pointed_back_to_it_goes(self):
        self.assertFalse(self.remove("Oil change", "take the oil change off, it's done next week").startswith(NOT_DONE))
        out = self.remove("Bake bread", "yes, take it off", shown="Want me to take Bake bread off the plan?")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_apps_own_controls_are_never_held(self):
        self.pt.by_hand = True
        try:
            self.assertFalse(self.pt.execute("remove_event", {"event": "Oil change"}).startswith(NOT_DONE))
        finally:
            self.pt.by_hand = False


class TestReaddingKeepsOneItem(PlanCase):
    def add(self, said, **args):
        self.pt.user_message = said
        try:
            return self.pt.execute("add_event", {"duration": "30 min", **args})
        finally:
            self.pt.user_message = None

    def test_re_adding_at_the_time_the_user_asks_moves_it(self):
        out = self.add("can the oil change go at 4 instead", title="Oil change", start="4:00 PM")
        self.assertFalse(out.startswith(NOT_DONE), out)
        copies = self.planned("Oil change")
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].start_dt.hour, 16)

    def test_re_adding_at_its_own_time_adds_nothing(self):
        out = self.add("oil change at 1 like before", title="Oil change", start="1:00 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("nothing to add", out)
        self.assertEqual(len(self.planned("Oil change")), 1)

    def test_not_named_it_still_points_to_moving_it(self):
        out = self.add("plan my evening", title="Oil change", start="7:00 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("update_event", out)
        self.assertEqual(self.planned("Oil change")[0].start_dt.hour, 13)

    def test_a_second_session_on_purpose_still_goes_on(self):
        out = self.add("bake bread again at 8", title="Bake bread", start="8:00 PM", again=True)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.planned("Bake bread")), 2)


class WithBookkeeping:
    def setUp(self):
        super().setUp()
        self.manager.scheduler.config.assistant.turn_bookkeeping = True


class TestAllFixedWithBookkeeping(WithBookkeeping, TestAllFixedWithNothingChanged):
    pass


if __name__ == "__main__":
    unittest.main()
