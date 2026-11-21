"""A new item the model adds for the user's message quotes the words it comes from (add_source.py).

The name is the model's to choose; ``because`` is the span of the user's words the add stands on, and Python checks
only that those words are really there, this turn or in the user's last few messages. Thirties' own adds need no
quote. Invented data throughout.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import reply
from tests.test_model_reads_the_words import WordsCase
from tests.test_today import TodayTestCase
from thirties_core.plan_log import model_call, tool_call
from thirties_core.plan_tools import NOT_DONE


class CiteCase(WordsCase):
    def add(self, title, words, because=None, start="9:40 PM", earlier=()):
        args = {"title": title, "start": start, "duration": "30 min"}
        if because is not None:
            args["because"] = because
        self.pt.recent_said = list(earlier)
        try:
            with model_call():
                return self.said("add_event", args, words)
        finally:
            self.pt.recent_said = None


class TestACitedAdd(CiteCase):

    def test_a_natural_name_with_the_words_cited_goes_on(self):
        self.assertWent(self.add("Hardware store run", "right, need sandpaper before the thwart", "need sandpaper"))
        self.assertEqual(len(self.planned("Hardware store run")), 1)

    def test_case_spacing_and_curly_quotes_dont_matter(self):
        words = "the spokeshave’s blade is dull, I’ll get it  honed"
        self.assertWent(self.add("Hone the spokeshave", words, "“I'll get it honed.”"))
        self.assertWent(self.add("Strop the chisels", "Strop The CHISELS\nat 10:40", "strop the chisels at 10:40",
                                 start="10:40 PM"))


class TestATitleInTheUsersWords(CiteCase):

    def test_a_title_that_is_their_words_is_its_own_quote(self):
        self.assertWent(self.add("Oil the bench vise", "I'll oil the bench vise at 9:40"))
        self.assertHeld(self.add("Oil the vise jaws", "I'll oil the bench vise at 9:40", start="10:10 PM"))
        self.assertHeld(self.add("Vise", "I'll oil the bench vises at 9:40", start="10:40 PM"))


class TestAnUncitedAdd(CiteCase):

    def test_a_made_up_quote_is_refused(self):
        out = self.add("Oil the bench vise", "the thwart sanding is done", "oil the bench vise")
        self.assertHeld(out)
        self.assertIn("isn't what the user said", out)
        self.assertIn("ask", out)
        self.assertEqual(self.planned("Oil the bench vise"), [])

    def test_no_quote_is_refused(self):
        for because in (None, "", "  ", "…"):
            with self.subTest(because=because):
                out = self.add("Hardware store run", "need sandpaper before the thwart", because)
                self.assertHeld(out)
                self.assertIn("because", out)
                self.assertEqual(self.planned("Hardware store run"), [])


class TestAQuoteFromJustBefore(CiteCase):

    def test_the_last_messages_can_be_cited(self):
        out = self.add("Hone the spokeshave", "ok, after the varnish then", "get the spokeshave honed",
                       earlier=["the varnish first", "I should get the spokeshave honed"])
        self.assertWent(out)

    def test_a_message_further_back_cannot(self):
        # the window is the one removal asks read (change_asks.earlier_messages): two messages back at most
        self.pt.prior_exchange = None
        out = self.add("Hone the spokeshave", "ok, after the varnish then", "get the spokeshave honed",
                       earlier=["the varnish first", "the thwart is sanded", "I should get the spokeshave honed"])
        self.assertHeld(out)

    def test_through_two_turns_with_the_model(self):
        self.engine.responses = [reply("Noted."), reply(
            'CALL add_event {"title": "Hone the spokeshave", "start": "9:40 PM", "duration": "30 min", '
            '"because": "the spokeshave blade needs honing"}'), reply("Hone the spokeshave is on at 9:40.")]
        self.manager.send_user_message("the spokeshave blade needs honing")
        self.manager.send_user_message("ok, after the thwart then")
        self.assertEqual(len(self.planned("Hone the spokeshave")), 1)

    def test_a_turn_with_no_quote_leaves_it_off(self):
        self.engine.responses = [reply(
            'CALL add_event {"title": "Oil the bench vise", "start": "9:40 PM", "duration": "30 min"}'),
            reply("I can add oiling the vise if you want it.")]
        self.manager.send_user_message("the thwart sanding is done")
        self.assertEqual(self.planned("Oil the bench vise"), [])
        results = [str(m.get("content")) for m in self.manager.brain.messages if m.get("role") == "tool"]
        self.assertTrue(any(r.startswith(NOT_DONE) and "because" in r for r in results), results)


class TestThirtiesOwnAdds(CiteCase):

    def test_nobody_talking(self):
        with model_call():
            out = self.pt.execute("add_event", {"title": "Oil the bench vise", "start": "9:40 PM",
                                                "duration": "30 min"})
        self.assertWent(out)

    def test_thirties_code_calling_the_tool_in_a_turn(self):
        self.assertWent(self.said("add_event", {"title": "Oil the bench vise", "start": "9:40 PM",
                                                "duration": "30 min"}, "the thwart sanding is done"))

    def test_by_hand(self):
        self.pt.by_hand = True
        try:
            with model_call():
                out = self.said("add_event", {"title": "Oil the bench vise", "start": "9:40 PM",
                                              "duration": "30 min"}, "the thwart sanding is done")
        finally:
            self.pt.by_hand = False
        self.assertWent(out)

    def test_inside_another_tool(self):
        for outer in ("put_routine", "propose_day"):
            title = f"Oil the bench vise ({outer})"
            with self.subTest(outer=outer):
                self.pt.user_message = "the thwart sanding is done"
                try:
                    with model_call(), tool_call(outer):
                        out = self.pt.add_event(title, start="9:40 PM", duration="30 min", alongside=True)
                finally:
                    self.pt.user_message = None
                self.assertWent(out)


class ListCase(TodayTestCase):
    def said(self, tool, args, words):
        self.t.user_message = words
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def morning_done(self):
        from thirties_core import morning
        morning.mark_done(self.t._state_db, self.t.day_plan.target_date, at("07:50"))


class TestTheOtherAddTools(ListCase):

    def test_add_task(self):
        args = {"title": "Order spare drill bits", "list": "Errands/Admin", "reason": "an errand"}
        out = self.said("add_task", args, "the thwart sanding is done")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("drill bits", self.body("n_errands"))
        out = self.said("add_task", {**args, "because": "buy a new set of bits"},
                        "the drill bit snapped, order spare bits")
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = self.said("add_task", {**args, "because": "snapped, order spare"},
                        "the drill bit snapped, order spare bits")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [ ] Order spare drill bits", self.body("n_errands"))

    def test_add_to_today_a_new_thing(self):
        out = self.said("add_to_today", {"items": ["Wax the drawer runners"]}, "the bank first today")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("drawer runners", self.body("n_today"))
        out = self.said("add_to_today", {"items": ["Wax the drawer runners"], "because": "drawers keep sticking"},
                        "the drawers keep sticking, today please")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Wax the drawer runners", self.body("n_today"))

    def test_add_to_today_a_list_item_needs_no_quote(self):
        out = self.said("add_to_today", {"items": ["Call the bank"]}, "today I'd better phone them")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Call the bank", self.body("n_today"))

    def test_propose_day_a_new_thing(self):
        self.morning_done()
        item = {"item": "Wax the drawer runners", "start": "7:30 PM", "duration": "30 min"}
        out = self.said("propose_day", {"items": [item]}, "the evening is open")
        self.assertIn("because", out)
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertFalse(any(s.title == "Wax the drawer runners" for s in self.t.day_plan.spans))
        out = self.said("propose_day", {"items": [{**item, "because": "sticky drawers"}]},
                        "plan something for the sticky drawers")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertTrue(any(s.title == "Wax the drawer runners" for s in self.t.day_plan.spans))

    def test_propose_day_a_list_item_needs_no_quote(self):
        self.morning_done()
        out = self.said("propose_day", {"items": [{"item": "Call the bank", "start": "7:30 PM"}]}, "plan my evening")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_mornings_day_proposal_needs_no_quote(self):
        out = self.said("propose_day", {"items": [{"item": "Wax the drawer runners", "start": "7:30 PM",
                                                   "duration": "30 min"}]}, "morning")
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
