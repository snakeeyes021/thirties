"""A true done goes through, while the wrong-done checks still hold.

- **"now" about the app's display.** "lunch at 10:40, the time it shows now": the "now" is about what the plan
  says, not about lunch being under way. A clause about some other activity ("sanding until the kiln check") says
  nothing about an item named in another clause. A count right after the app showed the items ("those three are
  finished") covers them.
- **A container noun the item lacks.** "the firing paperwork's lodged" for Lodge kiln firing permit: the words
  share the item's own head words and its verb in the past, no other item shares them, and "paperwork" is only a
  word for the thing's papers. It ticks. "the glaze order is sent" while only Glaze recipe test is planned still
  doesn't: "order" is an object of its own, and "sent" isn't that item's verb.

Through the real log_outcome path on the mock Joplin API. Fixtures are invented: a potter who paddles a canoe on
days off.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE


class TrueDoneBase(TodayTestCase):
    ITEMS = ()  # (title, start, task id)

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "".join(f"- [ ] {t}\n" for t, _, _ in self.ITEMS)
        self.services.tasks[:] = [item(tid, t) for t, _, tid in self.ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in self.ITEMS:
            self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
        set_debug_time(at("11:10"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, title, prior=None):
        self.t.user_message, self.t.prior_exchange = message, prior
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"})
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def fresh(self):
        self.doCleanups()
        self.setUp()

    def assert_ticked(self, title, out):
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)

    def assert_not_ticked(self, title, out):
        self.assertTrue(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)


class NowAboutTheDisplayTest(TrueDoneBase):
    ITEMS = (("Trim the mugs", "9:40 AM", "t_mugs"), ("Rinse the canoe hull", "10:10 AM", "t_hull"),
             ("Lunch", "10:40 AM", "t_lunch"), ("Kiln check", "1:20 PM", "t_kiln"))
    SHOWN = ("Here's where the morning stands:\n9:30 AM Trim the mugs\n10:00 AM Rinse the canoe hull\n"
             "10:30 AM Lunch\n1:00 PM Kiln check\nAre those first three finished?")
    MESSAGES = (
        "The mugs got trimmed by 9:50 and the hull rinsed by 10:25. Lunch was 10:40, as the plan shows now. "
        "Sanding paddles fills the time until the kiln check.",
        # a done said after "before" is still a done
        "Lunch happened at the time it says now, and before that I'd trimmed the mugs and rinsed the hull, so those "
        "three are finished. The wheel stays busy until the kiln check.",
        "Lunch went the way the schedule has it now; the mugs and the hull were finished ahead of it. I'm on the "
        "paddles till the kiln check.",
        # "lunchroom" reads loosely as Lunch: the clause is about the wheel, and the lunchroom sweep is when it stops
        "Mugs trimmed, hull rinsed, and lunch eaten at 10:40 just as the schedule shows now. The wheel's going again "
        "until the lunchroom sweep.",
        # the display's "now" just before the item's name
        "as it shows now lunch was at 10:40, with the mugs and hull finished earlier",
    )

    def test_each_item_named_ticks(self):
        for message in self.MESSAGES:
            for title in ("Trim the mugs", "Rinse the canoe hull", "Lunch"):
                with self.subTest(message=message, title=title):
                    self.fresh()
                    self.assert_ticked(title, self.say(message, title, prior=("", self.SHOWN)))

    def test_the_item_named_only_as_an_end_does_not_tick(self):
        for message in self.MESSAGES[:3]:
            with self.subTest(message=message):
                self.fresh()
                self.assert_not_ticked("Kiln check", self.say(message, "Kiln check", prior=("", self.SHOWN)))

    def test_an_item_the_words_dont_name_is_the_models_reading_with_a_note(self):
        for message in self.MESSAGES[3:]:
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, "Kiln check", prior=("", self.SHOWN))
                self.assertIn("says nothing of Kiln check", out)
                self.assert_ticked("Kiln check", out)

    def test_now_about_the_item_itself_still_holds(self):
        for message in ("lunch now", "tucking into lunch now", "the mugs are trimmed, now lunch"):
            with self.subTest(message=message):
                self.fresh()
                self.assert_not_ticked("Lunch", self.say(message, "Lunch"))

    def test_the_display_now_is_not_an_onward_word(self):
        from thirties_core.said_for_it import begun_or_next
        from thirties_core.write_guard import state_not_done
        others = ["Trim the mugs", "Rinse the canoe hull", "Kiln check"]
        for message in ("lunch at 10:40 like it shows now", "lunch 10:40, as the plan says now",
                        "lunch at the time it says now"):
            with self.subTest(message=message):
                self.assertEqual(begun_or_next(message, "Lunch", others), "")
                self.assertEqual(state_not_done(message, "Lunch", others), "")
        self.assertTrue(begun_or_next("mugs are trimmed, now lunch", "Lunch", others))

    def test_an_item_named_only_as_when_something_else_stops(self):
        from thirties_core.write_guard import says_done, state_not_done
        others = ["Trim the mugs", "Rinse the canoe hull", "Kiln check"]
        for message in ("trimming mugs until lunch", "finished the hull before lunch"):
            with self.subTest(message=message):
                self.assertTrue(state_not_done(message, "Lunch", others))
                self.assertFalse(says_done(message, "Lunch", others))
        for message in ("had lunch until 11:20", "glazed until lunch was finished"):
            with self.subTest(message=message):
                self.assertEqual(state_not_done(message, "Lunch", others), "")


class CountPointingBackTest(TrueDoneBase):
    ITEMS = (("Trim the mugs", "9:40 AM", "t_mugs"), ("Rinse the canoe hull", "10:10 AM", "t_hull"),
             ("Lunch", "10:40 AM", "t_lunch"), ("Kiln check", "1:20 PM", "t_kiln"))
    THREE = "This morning had:\n9:30 AM Trim the mugs\n10:00 AM Rinse the canoe hull\n10:30 AM Lunch"
    FOUR = THREE + "\n1:00 PM Kiln check"

    def test_a_count_covers_exactly_the_items_just_shown(self):
        for message in ("Finished all three.", "those 3 are finished", "finished three of them"):
            for title in ("Trim the mugs", "Rinse the canoe hull", "Lunch"):
                with self.subTest(message=message, title=title):
                    self.fresh()
                    self.assert_ticked(title, self.say(message, title, prior=("", self.THREE)))

    def test_a_count_that_does_not_match_what_was_shown_is_the_models_reading(self):
        for title in ("Trim the mugs", "Lunch", "Kiln check"):
            with self.subTest(title=title):
                self.fresh()
                self.assert_ticked(title, self.say("Finished all three.", title, prior=("", self.FOUR)))
                self.fresh()
                self.assert_ticked(title, self.say("finished those two", title, prior=("", self.THREE)))

    def test_pointed_count(self):
        from thirties_core.done_counts import pointed_count
        self.assertEqual(pointed_count("all three finished"), 3)
        self.assertEqual(pointed_count("those 3 done"), 3)
        self.assertEqual(pointed_count("two of them"), 2)
        self.assertIsNone(pointed_count("finished at 4"))
        self.assertIsNone(pointed_count("all 3 pm"))
        self.assertIsNone(pointed_count("the mugs are done"))


class ContainerNounTest(TrueDoneBase):
    ITEMS = (("Pay the clay supplier", "9:40 AM", "t_clay"), ("Lodge kiln firing permit", "10:10 AM", "t_permit"),
             ("Glaze recipe test", "10:40 AM", "t_recipe"))

    def test_a_container_noun_the_item_lacks_still_ticks(self):
        for message in ("clay supplier's paid, and the firing paperwork's lodged",
                        "lodged the kiln permit forms this morning",
                        "kiln firing stuff lodged"):
            with self.subTest(message=message):
                self.fresh()
                self.assert_ticked("Lodge kiln firing permit", self.say(message, "Lodge kiln firing permit"))

    def test_the_other_item_in_the_same_message_ticks(self):
        out = self.say("clay supplier's paid, and the firing paperwork's lodged", "Pay the clay supplier")
        self.assert_ticked("Pay the clay supplier", out)

    def test_an_object_noun_goes_on_with_a_note(self):
        for message, title, said in (("the glaze order is sent", "Glaze recipe test", "glaze order"),
                                     ("the kiln order is lodged", "Lodge kiln firing permit", "kiln order")):
            with self.subTest(message=message, title=title):
                self.fresh()
                out = self.say(message, title)
                self.assert_ticked(title, out)
                self.assertIn(f'"{said}"', out)  # no other item named: the model's reading stands

    def test_a_container_noun_with_another_items_verb_goes_on_with_a_note(self):
        for message in ("the kiln paperwork is paid", "the kiln forms are done"):
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, "Lodge kiln firing permit")
                self.assert_ticked("Lodge kiln firing permit", out)
                self.assertIn("also says", out)

    def test_words_two_items_fit_alike_are_the_models_to_read(self):
        self.api.notes["n_errands"]["body"] += "- [ ] Lodge kiln venting report\n"
        self.services.tasks.append(item("t_claim", "Lodge kiln venting report"))
        self.services.tasks[-1].list_role = "list"
        out = self.say("the kiln paperwork's lodged", "Lodge kiln firing permit")
        self.assert_ticked("Lodge kiln firing permit", out)
        self.assertIn('"kiln paperwork', out)  # fits the other no better: said, for the model to ask if unsure


class FitWordsTest(unittest.TestCase):
    def test_container_nouns(self):
        from thirties_core.done_fit import names_something_else
        title = "Lodge kiln firing permit"
        for message in ("the firing paperwork's lodged", "kiln permit forms lodged", "lodged the kiln bits"):
            self.assertEqual(names_something_else(message, title), [], message)
        for message in ("the glaze order is sent", "kiln order lodged", "the kiln paperwork is done"):
            self.assertTrue(names_something_else(message, "Glaze recipe test" if "glaze" in message else title),
                            message)
        self.assertTrue(names_something_else("the kiln paperwork's lodged", title, listed=["Lodge kiln venting report"]))

    def test_the_joplin_write_follows(self):
        from thirties_core.write_guard import refused
        self.assertEqual(refused({"op": "complete", "title": "Lodge kiln firing permit"},
                                 "[app] clay supplier's paid, and the firing paperwork's lodged", None), "")
        self.assertEqual(refused({"op": "complete", "title": "Glaze recipe test"}, "[app] the glaze order is sent",
                                 None), "")  # no other item named: the model's reading stands


if __name__ == "__main__":
    unittest.main()
