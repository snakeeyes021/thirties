"""A done the user never gave is never recorded.

- **Said not to tick it.** "leave the soup bowls unticked", "don't tick the bowls", "bowls: not yet finished": the
  item's name with a done-ish word nearby is no done when the words around it say no. The tick doesn't happen, on
  the plan or in Joplin, however the model calls log_outcome.
- **Said of something not on the plan.** "the glaze order is sent" while a Glaze recipe test is planned and Send
  the glaze order is listed: the planned item shares a word, but the user's words name the listed one. Which one
  they meant is the model's to read: the done goes on, and its result says what else the words say, so the model
  can ask or put it right.

Through the real log_outcome path on the mock Joplin API, with the model calling it the careless way (by the
planned item's id, its title, or a short form of it), and through a whole turn with a scripted model. Fixtures are
invented: a potter who paddles a canoe on days off.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

STUDIO = "- [ ] Glaze the soup bowls\n- [ ] Wax the canoe paddles\n- [ ] Glaze recipe test\n"


class WrongDoneBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = STUDIO
        self.services.tasks[:] = [item("t_bowls", "Glaze the soup bowls"), item("t_paddles", "Wax the canoe paddles"),
                                  item("t_recipe", "Glaze recipe test")]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in (("Glaze the soup bowls", "10:20 AM", "t_bowls"),
                                  ("Wax the canoe paddles", "10:50 AM", "t_paddles"),
                                  ("Glaze recipe test", "11:10 AM", "t_recipe")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
        set_debug_time(at("11:40"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, args, prior=None):
        """The model calls log_outcome during a turn of the user's: the plan and every Joplin write see their words."""
        self.t.user_message, self.t.prior_exchange = message, prior
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute("log_outcome", args)
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def assert_not_ticked(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertNotIn(f"[x] {title}", self.body("n_today"), out)
        self.assertNotIn(f"[x] {title}", self.body("n_errands"), out)
        for n in self.api.notes.values():
            if (n.get("title") or "").startswith("Done "):
                self.assertNotIn(title, n.get("body") or "", out)

    def fresh(self):
        """A new day for each case: what one case ticked never shows in the next."""
        self.doCleanups()
        self.setUp()

    WAYS = ("id", "title", "short")

    def way(self, title, how):
        """How a careless model names the item: its id, its title, or a short form of it."""
        event = {"id": self.span(title).id, "title": title, "short": title.split()[-1].lower()}[how]
        return {"event": event, "status": "done"}


class SaidNotToTickTest(WrongDoneBase):
    NEGATED = ("leave the soup bowls unticked for now",
               "keep the soup bowls unchecked, the second coat is still to go",
               "don't tick the soup bowls, they need another dip",
               "do not mark the soup bowls done",
               "soup bowls: not yet finished",
               "the soup bowls aren't finished",
               "hold off on ticking the soup bowls",
               "the soup bowls are nowhere near done",
               "soup bowls are far from done",
               "no tick for the soup bowls yet")

    def test_a_negated_done_never_ticks(self):
        for message in self.NEGATED:
            for how in self.WAYS:
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.say(message, self.way("Glaze the soup bowls", how))
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assert_not_ticked("Glaze the soup bowls", out)

    def test_another_items_done_beside_it_ticks_only_that_one(self):
        out = self.say("paddles are waxed, but leave the soup bowls unticked",
                       {"event": self.span("Glaze the soup bowls").id, "status": "done"})
        self.assertTrue(str(out).startswith(NOT_DONE), out)
        self.assert_not_ticked("Glaze the soup bowls", out)
        out = self.say("paddles are waxed, but leave the soup bowls unticked",
                       {"event": self.span("Wax the canoe paddles").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span("Wax the canoe paddles").status, SpanStatus.DONE)

    def test_a_plain_done_still_ticks(self):
        out = self.say("soup bowls are glazed", {"event": self.span("Glaze the soup bowls").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span("Glaze the soup bowls").status, SpanStatus.DONE)


    def test_a_complaint_about_the_record_still_ticks(self):
        for message in ("the soup bowls still aren't ticked, I glazed them at 10:40",
                        "the soup bowls show as unticked but I glazed them"):
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, {"event": self.span("Glaze the soup bowls").id, "status": "done"})
                self.assertFalse(str(out).startswith(NOT_DONE), out)
                self.assertEqual(self.span("Glaze the soup bowls").status, SpanStatus.DONE)

    def test_a_reply_pointing_back_with_a_no(self):
        out = self.say("leave it unticked, the rims need another pass",
                       {"event": self.span("Glaze the soup bowls").id, "status": "done"},
                       prior=("", "Are the soup bowls glazed?"))
        self.assertTrue(str(out).startswith(NOT_DONE), out)
        self.assert_not_ticked("Glaze the soup bowls", out)


class SomethingNotPlannedTest(WrongDoneBase):
    def test_words_for_something_off_the_plan_are_the_models_to_read(self):
        for message in ("the glaze order is sent", "glaze order done"):
            for how in self.WAYS:
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.say(message, self.way("Glaze recipe test", how))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assertIn('"glaze order"', out)  # the result says what else the words say

    def test_the_list_item_that_fits_every_word_is_the_one_ticked(self):
        self.api.notes["n_errands"]["body"] += "- [ ] Send the glaze order\n"
        self.services.tasks.append(item("t_order", "Send the glaze order"))
        self.services.tasks[-1].list_role = "list"
        out = self.say("the glaze order is sent", {"event": "glaze order", "status": "done"})
        self.assert_not_ticked("Glaze recipe test", out)
        self.assertIn("[x] Send the glaze order", self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.api.notes.values() if (n.get("title") or "").startswith("Done ")))

    def test_the_note_says_what_else_the_words_say(self):
        self.api.notes["n_errands"]["body"] += "- [ ] Send the glaze order\n"
        self.services.tasks.append(item("t_order", "Send the glaze order"))
        self.services.tasks[-1].list_role = "list"
        out = self.say("the glaze order is sent", {"event": self.span("Glaze recipe test").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertIn('also says "glaze order"', out)
        out = self.say("the glaze order is sent", {"event": "t_order", "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertIn("[x] Send the glaze order", self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.api.notes.values() if (n.get("title") or "").startswith("Done ")))

    def test_the_planned_item_said_in_full_still_ticks(self):
        out = self.say("glaze recipe test is done", {"event": self.span("Glaze recipe test").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span("Glaze recipe test").status, SpanStatus.DONE)

    def test_a_loose_word_among_several_is_the_models_reading(self):
        out = self.say("the glazing is done", {"event": self.span("Glaze recipe test").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span("Glaze the soup bowls").status, SpanStatus.PLANNED)


class _Kept:
    """A database holding only the open items Thirties keeps from the lists."""

    def __init__(self, *titles):
        import json
        self.pool = json.dumps({t.lower(): {"title": t} for t in titles})

    def get_sync_meta(self, key):
        return self.pool if key == "open_pool" else None


class JoplinWriteTest(unittest.TestCase):
    """The last check before Joplin: a tick for another item the words name, or one they say not to tick, isn't
    written."""

    def test_a_tick_told_not_to_is_refused_and_which_item_is_the_models_reading(self):
        from thirties_core.write_guard import refused
        for kept in (_Kept("Send the glaze order"), None):  # which item the done is for: the model's reading
            self.assertEqual(refused({"op": "complete", "title": "Glaze recipe test"},
                                     "[app] the glaze order is sent", kept), "")
        for message, title in (("leave the soup bowls unticked", "Glaze the soup bowls"),
                               ("don't tick the soup bowls yet", "Glaze the soup bowls")):
            self.assertTrue(refused({"op": "complete", "title": title}, f"[app] {message}", None), message)
        for message, title in (("glaze recipe test is done", "Glaze recipe test"),
                               ("finished the soup bowls first thing", "Glaze the soup bowls")):
            self.assertEqual(refused({"op": "complete", "title": title}, f"[app] {message}", None), "", message)


class FitWordsTest(unittest.TestCase):
    def test_what_names_something_else(self):
        from thirties_core.done_fit import names_something_else
        for message, title in (("the glaze order is sent", "Glaze recipe test"),
                               ("glaze order done", "Glaze recipe test"),
                               ("paddle rack is built", "Wax the canoe paddles")):
            self.assertTrue(names_something_else(message, title), message)
        for message, title in (("glaze recipe test is done", "Glaze recipe test"),
                               ("soup bowls finally glazed", "Glaze the soup bowls"),
                               ("waxed the paddles twenty minutes ago", "Wax the canoe paddles"),
                               ("glaze recipe test done, and the glaze order is sent", "Glaze recipe test"),
                               ("that's done", "Glaze recipe test"), ("yes", "Glaze recipe test")):
            self.assertEqual(names_something_else(message, title), [], message)

    def test_another_planned_items_words_dont_count_against_it(self):
        from thirties_core.done_fit import names_something_else
        self.assertEqual(names_something_else("bowls paddles done", "Glaze the soup bowls",
                                              ["Wax the canoe paddles"]), [])


class WholeTurnTest(WrongDoneBase):
    """A whole turn with a scripted model (no real calls) that ticks the wrong thing anyway."""

    def turn(self, message, event):
        from tests.test_conversation import reply
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        engine = MockInferenceEngine()
        m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                joplin_engine=self.services.joplin, inference_engine=engine)
        engine.responses = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "log_outcome", "arguments": {"event": event, "status": "done"}}]},
            reply("Marked it done."), reply("Marked it done.")]
        m.send_user_message(message)
        return m, [str(x.get("content")) for x in m.brain.messages if x.get("role") == "tool"]

    def status(self, m, title):
        return next(s for s in m.day_plan.spans if s.title == title).status

    def test_leave_it_unticked_in_a_whole_turn(self):
        m, results = self.turn("leave the soup bowls unticked for now", "Glaze the soup bowls")
        self.assertEqual(self.status(m, "Glaze the soup bowls"), SpanStatus.PLANNED, results)
        self.assertNotIn("[x]", self.body("n_today") + self.body("n_errands"), results)

    def test_something_off_the_plan_in_a_whole_turn(self):
        self.t.execute("add_event", {"title": "Send the glaze order", "start": "1:10 PM", "duration": "10 min"})
        m, results = self.turn("the glaze order is sent", "Glaze recipe test")
        self.assertEqual(self.status(m, "Glaze recipe test"), SpanStatus.DONE, results)  # the model's reading
        self.assertIn("Send the glaze order", " ".join(results))  # with a note naming the item the words fit


if __name__ == "__main__":
    unittest.main()
