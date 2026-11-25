"""A tick only for a done the user gave, and a reopen for a not-done they name.

- **Said ahead of time.** "I'll ping you as soon as the soup bowls are finished", "once the paddles are waxed I'll
  head out": a done promised or put off till later is no report. Nothing is ticked, on the plan or in Joplin,
  however the model calls log_outcome.
- **A count isn't a name.** "check those two off", "both of them are done": the number counts items named
  earlier. A done the model reads for an item whose title has the word in it (Fire two test tiles) goes on as its
  reading.
- **A named reopen goes through.** "nope, the paddles still need their wax: reopen them", "I never got to the
  paddles; please untick them": the user asks for a write, so the paddles go back to planned and their Joplin line
  is unticked, whether the model calls reopen_item or reports the item as open with log_outcome. Nothing the user
  didn't name is reopened.

Through the real tool path on the mock Joplin API and through whole turns with a scripted model (no real calls).
Fixtures are invented: a potter who paddles a canoe on days off.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

STUDIO = "- [ ] Glaze the soup bowls\n- [ ] Wax the canoe paddles\n- [ ] Fire two test tiles\n"
ITEMS = (("Glaze the soup bowls", "10:20 AM", "t_bowls"), ("Wax the canoe paddles", "10:50 AM", "t_paddles"),
         ("Fire two test tiles", "11:10 AM", "t_tiles"))


class SaidRightBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = STUDIO
        self.services.tasks[:] = [item(tid, title) for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
        set_debug_time(at("11:40"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def call(self, message, name, args, prior=None):
        """The model calls a tool during a turn of the user's: the plan and every Joplin write see their words."""
        self.t.user_message, self.t.prior_exchange = message, prior
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute(name, args)
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def say(self, message, args, prior=None):
        return self.call(message, "log_outcome", args, prior)

    def done_notes(self):
        return "".join(n.get("body") or "" for n in self.api.notes.values()
                       if (n.get("title") or "").startswith("Done "))

    def ticked_anywhere(self, title):
        return (f"[x] {title}" in self.body("n_today") + self.body("n_errands")) or title in self.done_notes()

    def assert_not_ticked(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertFalse(self.ticked_anywhere(title), out)

    def assert_ticked(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertTrue(self.ticked_anywhere(title), out)

    def fresh(self):
        """A new day for each case: what one case ticked never shows in the next."""
        self.doCleanups()
        self.setUp()

    WAYS = ("id", "title", "short")

    def way(self, title, how, status="done"):
        """How a careless model names the item: its id, its title, or a short form of it."""
        event = {"id": self.span(title).id, "title": title, "short": title.split()[-1].lower()}[how]
        return {"event": event, "status": status}


class SaidAheadTest(SaidRightBase):
    AHEAD = ("I'll ping you as soon as the soup bowls are finished",
             "text you once the soup bowls are glazed",
             "the minute the soup bowls are done you'll hear from me",
             "once the soup bowls are done I'm heading to the lake",
             "when I finish the soup bowls I'll say so",
             "after the soup bowls are glazed, the paddles are next",
             "the soup bowls will be done in a bit",
             "the soup bowls should be finished before lunch",
             "if the soup bowls get done today that's a win",
             "message you when the soup bowls are glazed",
             "soup bowls: done by 11:55 hopefully")

    def test_a_done_said_ahead_never_ticks(self):
        for message in self.AHEAD:
            for how in self.WAYS:
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.say(message, self.way("Glaze the soup bowls", how))
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assert_not_ticked("Glaze the soup bowls", out)

    def test_a_done_now_beside_one_said_ahead(self):
        message = "paddles are waxed; I'll give you a shout once the soup bowls are glazed"
        out = self.say(message, {"event": self.span("Glaze the soup bowls").id, "status": "done"})
        self.assertTrue(str(out).startswith(NOT_DONE), out)
        self.assert_not_ticked("Glaze the soup bowls", out)
        out = self.say(message, {"event": self.span("Wax the canoe paddles").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assert_ticked("Wax the canoe paddles", out)

    def test_a_done_in_the_past_still_ticks(self):
        for message in ("finished the soup bowls before my tea", "soup bowls are glazed",
                        "the soup bowls got done once the kiln cooled", "I told you the soup bowls were done",
                        "after the kiln cooled I glazed the soup bowls", "the soup bowls were done when I came in",
                        "soup bowls are glazed if you want to look"):
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, {"event": self.span("Glaze the soup bowls").id, "status": "done"})
                self.assertFalse(str(out).startswith(NOT_DONE), out)
                self.assert_ticked("Glaze the soup bowls", out)

    def test_joplin_refuses_a_tick_said_ahead(self):
        from thirties_core.write_guard import refused
        for message in self.AHEAD:
            self.assertTrue(refused({"op": "complete", "title": "Glaze the soup bowls"}, f"[app] {message}", None),
                            message)
        self.assertEqual(refused({"op": "complete", "title": "Glaze the soup bowls"},
                                 "[app] soup bowls are glazed", None), "")


class CountNotNameTest(SaidRightBase):
    SHOWN = "Still open from this morning: Glaze the soup bowls and Wax the canoe paddles."
    COUNTS = ("check those two off", "those two are done", "finished both", "the two of them are finished",
              "both of them, done", "mark the two done")

    def test_a_count_read_for_the_title_with_the_number_in_it_is_the_models_reading(self):
        for message in self.COUNTS:
            for how in ("id", "title"):
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.say(message, self.way("Fire two test tiles", how), prior=("", self.SHOWN))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)

    def test_a_count_with_nothing_named_before_is_the_models_reading(self):
        for message in ("check those two off", "finished both"):
            for how in ("id", "title"):
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.say(message, self.way("Fire two test tiles", how))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assert_ticked("Fire two test tiles", out)

    def test_the_two_named_before_are_the_ones_ticked(self):
        for message in ("those two are done", "finished both"):
            with self.subTest(message=message):
                self.fresh()
                for title in ("Glaze the soup bowls", "Wax the canoe paddles"):
                    out = self.say(message, {"event": self.span(title).id, "status": "done"}, prior=("", self.SHOWN))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assert_ticked(title, out)
                self.assert_not_ticked("Fire two test tiles")

    def test_the_tiles_named_by_their_own_words_still_tick(self):
        for message in ("fired the test tiles", "two test tiles fired", "the test tiles are done"):
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, {"event": self.span("Fire two test tiles").id, "status": "done"})
                self.assertFalse(str(out).startswith(NOT_DONE), out)
                self.assert_ticked("Fire two test tiles", out)


class NamedReopenTest(SaidRightBase):
    def setUp(self):
        super().setUp()
        out = self.say("waxed the canoe paddles", {"event": self.span("Wax the canoe paddles").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        out = self.say("soup bowls are glazed", {"event": self.span("Glaze the soup bowls").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assert_ticked("Wax the canoe paddles")
        self.assert_ticked("Glaze the soup bowls")

    ASKS = ("nope, the paddles still need their wax: reopen them",
            "I never got to the canoe paddles; please untick them",
            "reopen the paddles please, that tick was a mistake",
            "mark Wax the canoe paddles open again, it isn't finished")

    def assert_reopened(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertFalse(self.ticked_anywhere(title), out)
        self.assertIn(f"- [ ] {title}", self.body("n_errands"), out)

    def test_a_willing_model_reopens_the_one_named(self):
        for message in self.ASKS:
            for ref in ("Wax the canoe paddles", "paddles", "id"):
                with self.subTest(message=message, ref=ref):
                    self.fresh()
                    item_ref = self.span("Wax the canoe paddles").id if ref == "id" else ref
                    out = self.call(message, "reopen_item", {"item": item_ref})
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assert_reopened("Wax the canoe paddles", out)
                    self.assert_ticked("Glaze the soup bowls", out)

    def test_a_careless_model_reporting_it_open_reopens_it(self):
        for message in self.ASKS:
            for status in ("open", "planned", "not done", "undone"):
                with self.subTest(message=message, status=status):
                    self.fresh()
                    out = self.say(message, self.way("Wax the canoe paddles", "id", status))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assert_reopened("Wax the canoe paddles", out)
                    self.assert_ticked("Glaze the soup bowls", out)

    def test_nothing_unnamed_is_reopened(self):
        for message in self.ASKS:
            for name, args in (("reopen_item", {"item": "Glaze the soup bowls"}),
                               ("log_outcome", {"event": "Glaze the soup bowls", "status": "open"})):
                with self.subTest(message=message, tool=name):
                    self.fresh()
                    out = self.call(message, name, args)
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assert_ticked("Glaze the soup bowls", out)

    def test_no_reopen_without_an_ask(self):
        for message in ("waxing went well", "what's next?", "the paddles look great"):
            with self.subTest(message=message):
                self.fresh()
                out = self.call(message, "reopen_item", {"item": "Wax the canoe paddles"})
                self.assertTrue(str(out).startswith(NOT_DONE), out)
                self.assert_ticked("Wax the canoe paddles", out)

    def test_joplin_takes_the_untick_the_user_asked_for(self):
        from thirties_core.write_guard import refused
        self.assertEqual(refused({"op": "untick", "title": "Wax the canoe paddles", "note_id": "n_errands"},
                                 "[app] I never got to the canoe paddles; please untick them", None), "")
        self.assertTrue(refused({"op": "untick", "title": "Glaze the soup bowls", "note_id": "n_errands"},
                                "[app] I never got to the canoe paddles; please untick them", None))


class WholeTurnTest(SaidRightBase):
    """Whole turns with a scripted model (no real calls)."""

    def turn(self, message, calls, prior_shown=""):
        from tests.test_conversation import reply
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        engine = MockInferenceEngine()
        m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                joplin_engine=self.services.joplin, inference_engine=engine)
        if prior_shown:
            m.brain.messages.append({"role": "assistant", "content": prior_shown})
        engine.responses = [{"role": "assistant", "content": "", "tool_calls": calls},
                            reply("Done."), reply("Done.")]
        m.send_user_message(message)
        return m, [str(x.get("content")) for x in m.brain.messages if x.get("role") == "tool"]

    def status(self, m, title):
        return next(s for s in m.day_plan.spans if s.title == title).status

    def test_a_promise_in_a_whole_turn(self):
        m, results = self.turn("I'll ping you as soon as the soup bowls are finished",
                               [{"name": "log_outcome", "arguments": {"event": "Glaze the soup bowls",
                                                                      "status": "done"}}])
        self.assertEqual(self.status(m, "Glaze the soup bowls"), SpanStatus.PLANNED, results)
        self.assertNotIn("[x]", self.body("n_today") + self.body("n_errands"), results)

    def test_a_count_in_a_whole_turn(self):
        m, results = self.turn("check those two off", [{"name": "log_outcome", "arguments": {
            "event": "Fire two test tiles", "status": "done"}}], prior_shown=CountNotNameTest.SHOWN)
        self.assertEqual(self.status(m, "Fire two test tiles"), SpanStatus.DONE, results)  # the model's reading

    def test_a_reopen_in_a_whole_turn(self):
        out = self.say("waxed the canoe paddles", {"event": self.span("Wax the canoe paddles").id, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        for calls in ([{"name": "reopen_item", "arguments": {"item": "Wax the canoe paddles"}}],
                      [{"name": "log_outcome", "arguments": {"event": "Wax the canoe paddles", "status": "open"}}]):
            with self.subTest(tool=calls[0]["name"]):
                if self.span("Wax the canoe paddles").status != SpanStatus.DONE:
                    self.say("waxed the canoe paddles", {"event": self.span("Wax the canoe paddles").id,
                                                         "status": "done"})
                m, results = self.turn("nope, the paddles still need their wax: reopen them", calls)
                self.assertEqual(self.status(m, "Wax the canoe paddles"), SpanStatus.PLANNED, results)
                self.assertNotIn("[x] Wax the canoe paddles", self.body("n_today") + self.body("n_errands"),
                                 results)


if __name__ == "__main__":
    unittest.main()
