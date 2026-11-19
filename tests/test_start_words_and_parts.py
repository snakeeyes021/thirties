"""Starting something is no done for it, and words about an item's parts never take the item off or tick it.

- **Starting, beside a done.** "the clay mugs are trimmed, on to the gunwales": the mugs are done, the gunwales
  have only begun (or come next). The gunwales are never ticked, on the plan or in Joplin, however the model calls
  log_outcome, and a reopen the user asks for after a wrong tick goes through the first time.
- **Parts of an item.** "what are the two clays under the clay bins? get rid of those": the pointer is about the
  lines under the item, not the item. The item stays on the plan and its list, and nothing is ticked.
- **A question changes nothing**: a removal or a done asked about is no ask.
- **Nothing ticks without the user's done**, in a turn or later: an item the model said finishes by itself only
  does so when the user asked for that, an item taken off the plan is never ticked afterwards, and the morning's
  housekeeping never writes a tick for a done the user never gave.

Exercised through the real tool paths on the mock Joplin API, with the debug clock moving the background on,
and whole turns with a scripted model (no real calls). Fixtures are invented: a potter who paddles a canoe on days off.
"""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools

STUDIO = ("- [ ] Trim the clay mugs\n- [ ] Sand the canoe gunwales\n- [ ] Restock the clay bins\n"
          "    - [ ] red stoneware\n    - [ ] speckled buff\n")
ITEMS = (("Trim the clay mugs", "1:10 PM", "t_mugs"), ("Sand the canoe gunwales", "1:40 PM", "t_gunwales"),
         ("Restock the clay bins", "2:20 PM", "t_bins"))
MUGS, GUNWALES, BINS = (title for title, _, _ in ITEMS)


class PotterBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = STUDIO
        self.services.tasks[:] = [item(tid, title) for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            self.t.execute("add_event", {"title": title, "start": start, "duration": "25 min", "task_id": tid})
        set_debug_time(at("13:35"))

    def span(self, title, tools=None):
        return next((s for s in (tools or self.t).day_plan.spans if s.title == title), None)

    def call(self, message, name, args, prior=None, tools=None):
        """The model calls a tool during a turn of the user's: the plan and every Joplin write see their words."""
        t = tools or self.t
        t.user_message, t.prior_exchange = message, prior
        token = current_turn.set(f"[app] {message}")
        try:
            return t.execute(name, args)
        finally:
            current_turn.reset(token)
            t.user_message, t.prior_exchange = None, None

    def done_notes(self):
        return "".join(n.get("body") or "" for n in self.api.notes.values()
                       if (n.get("title") or "").startswith("Done "))

    def ticked_anywhere(self, title):
        return (f"[x] {title}" in self.body("n_today") + self.body("n_errands")) or title in self.done_notes()

    def assert_not_ticked(self, title, out=""):
        span = self.span(title)
        self.assertTrue(span is None or span.status != SpanStatus.DONE, out)
        self.assertFalse(self.ticked_anywhere(title), out)

    def assert_ticked(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertTrue(self.ticked_anywhere(title), out)

    def fresh(self):
        """A new day for each case: what one case did never shows in the next."""
        self.doCleanups()
        self.setUp()

    def way(self, title, how, status="done"):
        """How a careless model names the item: its id, its title, or a short form of it."""
        event = {"id": self.span(title).id, "title": title, "short": title.split()[-1].lower()}[how]
        return {"event": event, "status": status}

    def morning_built(self):
        """The day is under way: the morning has gone out (the question rule holds from then on)."""
        from thirties_core import morning
        morning.mark_done(self.sched.state_db, DAY, at("08:40"))

    def next_morning(self):
        """The next day's housekeeping, as the background runs it."""
        set_debug_time(at("08:25", 1))
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        t2 = PlanTools(plan, pending, self.sched, services=self.services)
        t2.assistant.start_the_day()
        return t2


class StartingBesideADoneTest(PotterBase):
    MIXED = ("clay mugs are trimmed. getting going on the canoe gunwales now",
             "mugs trimmed, on to the gunwales",
             "Finished the clay mugs, gunwales next",
             "the clay mugs are done; now doing the canoe gunwales",
             "the gunwales I'm working on; the mugs are done",
             "mugs are done; about to begin on the gunwales",
             "clay mugs finished, starting in on the canoe gunwales",
             "mugs wrapped up. moving on to the gunwales",
             "Clay mugs done, gunwales up next",
             "trimmed the clay mugs; time for the gunwales",
             "clay mugs complete! heading into the canoe gunwales",
             "the mugs are done. Now the gunwales")

    def test_the_one_started_is_never_ticked(self):
        for message in self.MIXED:
            for how in ("id", "title", "short"):
                with self.subTest(message=message, way=how):
                    self.fresh()
                    out = self.call(message, "log_outcome", self.way(GUNWALES, how))
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assert_not_ticked(GUNWALES, out)

    def test_the_one_done_still_ticks(self):
        for message in self.MIXED:
            with self.subTest(message=message):
                self.fresh()
                out = self.call(message, "log_outcome", self.way(MUGS, "id"))
                self.assertFalse(str(out).startswith(NOT_DONE), out)
                self.assert_ticked(MUGS, out)
                self.call(message, "log_outcome", self.way(GUNWALES, "id"))
                self.assert_not_ticked(GUNWALES)

    def test_joplin_refuses_the_one_started(self):
        from thirties_core.write_guard import refused
        for message in self.MIXED:
            with self.subTest(message=message):
                self.assertTrue(refused({"op": "complete", "title": GUNWALES}, f"[app] {message}", None), message)
                self.assertEqual(refused({"op": "complete", "title": MUGS}, f"[app] {message}", None), "")

    def test_the_one_started_said_alone_is_no_done(self):
        for message in ("on to the gunwales", "gunwales next", "now doing the canoe gunwales",
                        "moving on to the gunwales", "time for the gunwales"):
            with self.subTest(message=message):
                self.fresh()
                out = self.call(message, "log_outcome", self.way(GUNWALES, "id"))
                self.assertTrue(str(out).startswith(NOT_DONE), out)
                self.assert_not_ticked(GUNWALES, out)

    def test_a_done_for_both_still_ticks_both(self):
        for message in ("clay mugs trimmed and canoe gunwales sanded", "the mugs and the gunwales are both done",
                        "finished the clay mugs, finished the canoe gunwales"):
            with self.subTest(message=message):
                self.fresh()
                for title in (MUGS, GUNWALES):
                    out = self.call(message, "log_outcome", self.way(title, "id"))
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assert_ticked(title, out)

    def test_a_wrong_tick_reopens_on_the_first_ask(self):
        asks = (("the gunwales aren't finished, I began them a moment ago. untick that", None),
                ("no, I've only started sanding the canoe gunwales; reopen it", None),
                ("that's wrong, put it back", ("", f"Marked {GUNWALES} done.")))
        for message, prior in asks:
            for tool, args in (("reopen_item", {"item": GUNWALES}),
                               ("log_outcome", {"event": GUNWALES, "status": "open"})):
                with self.subTest(message=message, tool=tool):
                    self.fresh()
                    self.t.by_hand = True  # ticked in the app by mistake
                    try:
                        self.t.execute("log_outcome", {"event": self.span(GUNWALES).id, "status": "done"})
                    finally:
                        self.t.by_hand = False
                    self.assert_ticked(GUNWALES)
                    out = self.call(message, tool, args, prior=prior)
                    self.assertFalse(str(out).startswith(NOT_DONE), out)
                    self.assertEqual(self.span(GUNWALES).status, SpanStatus.PLANNED, out)
                    self.assertIn(f"- [ ] {GUNWALES}", self.body("n_errands"), out)


class FinishesByItselfTest(PotterBase):
    """A done at an item's end, with nobody talking, only when the user asked for it."""

    def run_past(self, title, minutes=90):
        set_debug_time(self.span(title).end_dt + timedelta(minutes=minutes))
        self.t.assistant.settle_moot()

    def test_started_with_no_ask_is_never_ticked_by_the_clock(self):
        for message in ("clay mugs are trimmed. getting going on the canoe gunwales now",
                        "starting the canoe gunwales, should take 40 minutes"):
            with self.subTest(message=message):
                self.fresh()
                self.call(message, "still_doing", {"event": GUNWALES, "length": "40 minutes", "auto_done": True})
                self.run_past(GUNWALES)
                self.assert_not_ticked(GUNWALES)

    def test_asked_for_it_finishes_by_itself(self):
        out = self.call("the gunwales take me 40 minutes; once that's up, tick them off without asking me",
                        "still_doing", {"event": GUNWALES, "length": "40 minutes", "auto_done": True})
        self.assertIn("done by itself", out)
        self.run_past(GUNWALES)
        self.assert_ticked(GUNWALES)


class PartsOfAnItemTest(PotterBase):
    PARTS = ("what are the two clays under the clay bins? get rid of those",
             "which lines sit inside Restock the clay bins? take them off",
             "the clay bins has two clays listed in it, which are they? remove them",
             "what's tucked under the clay bins item? scrap those two")

    def test_a_pointer_at_the_parts_never_removes_the_item(self):
        for message in self.PARTS:
            for how in ("id", "title"):
                with self.subTest(message=message, way=how):
                    self.fresh()
                    event = self.span(BINS).id if how == "id" else BINS
                    out = self.call(message, "remove_event", {"event": event})
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assertIsNotNone(self.span(BINS), out)
                    self.assertIn(f"- [ ] {BINS}", self.body("n_errands"), out)
                    self.assert_not_ticked(BINS, out)

    def test_a_pointer_at_the_parts_never_ticks_the_item(self):
        for message in self.PARTS:
            with self.subTest(message=message):
                self.fresh()
                out = self.call(message, "log_outcome", self.way(BINS, "id"))
                self.assertTrue(str(out).startswith(NOT_DONE), out)
                self.assert_not_ticked(BINS, out)

    def test_the_item_named_for_removal_still_comes_off(self):
        for message in ("take the clay bins off today", "please remove Restock the clay bins",
                        "could you take the clay bins off?", "the clay bins can go, drop it for today"):
            with self.subTest(message=message):
                self.fresh()
                out = self.call(message, "remove_event", {"event": self.span(BINS).id})
                self.assertFalse(str(out).startswith(NOT_DONE), out)
                self.assertIsNone(self.span(BINS), out)
                self.assert_not_ticked(BINS, out)


class AQuestionChangesNothingTest(PotterBase):
    QUESTIONS = ("should the clay bins come off today?", "what's still inside the clay bins item?",
                 "is the clay bins thing done?", "which ones would you remove from the clay bins?",
                 "did I already restock the clay bins?")

    def test_no_removal_and_no_tick_on_a_question(self):
        for built in (False, True):
            for message in self.QUESTIONS:
                for name, args in (("remove_event", {"event": BINS}), ("log_outcome", {"event": BINS,
                                                                                        "status": "done"})):
                    with self.subTest(message=message, tool=name, built=built):
                        self.fresh()
                        if built:
                            self.morning_built()
                        out = self.call(message, name, args)
                        self.assertTrue(str(out).startswith(NOT_DONE), out)
                        self.assertIsNotNone(self.span(BINS), out)
                        self.assert_not_ticked(BINS, out)

    def test_a_done_said_with_a_question_still_ticks(self):
        out = self.call("restocked the clay bins. what's next?", "log_outcome", self.way(BINS, "id"))
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assert_ticked(BINS, out)


class RemovedStaysUntickedTest(PotterBase):
    """An item taken off is never ticked after, by a later call, the clock or the morning."""

    def remove(self):
        out = self.call("take the clay bins off today", "remove_event", {"event": self.span(BINS).id})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertIsNone(self.span(BINS))

    def test_a_careless_done_after_the_removal(self):
        for message in ("splendid, carry on", "righto", "anything left for the afternoon?",
                        "alright, on to the gunwales"):
            for args in ({"event": BINS, "status": "done"}, {"event": BINS, "status": "done", "new": True},
                         {"event": "t_bins", "status": "done"}):
                with self.subTest(message=message, args=args):
                    self.fresh()
                    self.remove()
                    out = self.call(message, "log_outcome", args)
                    self.assertTrue(str(out).startswith(NOT_DONE), out)
                    self.assertFalse(any(s.title == BINS and s.status == SpanStatus.DONE
                                         for s in self.t.day_plan.spans), out)
                    self.assertFalse(self.ticked_anywhere(BINS), out)
                    self.next_morning()
                    self.assertFalse(self.ticked_anywhere(BINS), out)

    def test_the_clock_and_the_morning_never_tick_it(self):
        self.remove()
        set_debug_time(at("17:50"))
        self.t.assistant.settle_moot()
        self.next_morning()
        self.assertFalse(self.ticked_anywhere(BINS))
        self.assertNotIn(f"[x] {BINS}", self.body("n_today"))

    def test_a_done_the_user_gives_for_it_by_name_still_counts(self):
        self.remove()
        out = self.call("actually I restocked the clay bins after all", "log_outcome", {"event": BINS,
                                                                                         "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertTrue(self.ticked_anywhere(BINS), out)


class QuestionNeverTickedLaterTest(PotterBase):
    """No done the user never gave is recorded, so the clock and the morning never write one to Joplin."""
    PLATE, OIL = "Glaze a kiln-side snack plate", "Oil the paddle blades"

    def setUp(self):
        super().setUp()
        self.t.execute("add_event", {"title": self.PLATE, "start": "4:40 PM", "duration": "25 min"})
        self.api.notes["n_today"]["body"] += f"- [ ] {self.OIL}\n"  # only on Today, with no time

    def test_the_morning_never_ticks_what_the_user_never_said_done(self):
        for message, args in (("what's still inside the clay bins item?", self.way(BINS, "id")),
                              ("is the snack plate glazed yet?", {"event": self.PLATE, "status": "done"}),
                              ("did the snack plate get glazed?", {"event": self.PLATE, "status": "done"}),
                              ("splendid, carry on", {"event": self.PLATE, "status": "done"}),
                              ("splendid, carry on", {"event": self.OIL, "status": "done", "new": True}),
                              ("which paddle blades still need oil?", {"event": self.OIL, "status": "done",
                                                                       "new": True})):
            with self.subTest(message=message, event=args["event"]):
                self.fresh()
                out = self.call(message, "log_outcome", args)
                self.assertTrue(str(out).startswith(NOT_DONE), out)
                self.assertFalse(any(s.status == SpanStatus.DONE for s in self.t.day_plan.spans), out)
                set_debug_time(at("18:10"))
                self.t.assistant.settle_moot()
                self.next_morning()
                for title in (BINS, self.PLATE, self.OIL):
                    self.assertFalse(self.ticked_anywhere(title), (title, out))

    def test_said_done_it_ticks(self):
        out = self.call("glazed the kiln-side snack plate", "log_outcome", {"event": self.PLATE, "status": "done"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertEqual(self.span(self.PLATE).status, SpanStatus.DONE)
        out = self.call("oiled the paddle blades", "log_outcome", {"event": self.OIL, "status": "done", "new": True})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        self.assertIn(f"[x] {self.OIL}", self.body("n_today"))


class WholeTurnTest(PotterBase):
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
        span = next((s for s in m.day_plan.spans if s.title == title), None)
        return span.status if span is not None else None

    def test_a_done_and_a_start_in_one_turn(self):
        m, results = self.turn("mugs trimmed, on to the gunwales", [
            {"name": "log_outcome", "arguments": {"event": MUGS, "status": "done"}},
            {"name": "log_outcome", "arguments": {"event": GUNWALES, "status": "done"}}])
        self.assertEqual(self.status(m, MUGS), SpanStatus.DONE, results)
        self.assertNotEqual(self.status(m, GUNWALES), SpanStatus.DONE, results)
        self.assertNotIn(f"[x] {GUNWALES}", self.body("n_errands") + self.body("n_today"), results)

    def test_parts_asked_about_in_a_whole_turn(self):
        m, results = self.turn("what are the two clays under the clay bins? get rid of those", [
            {"name": "remove_event", "arguments": {"event": BINS}},
            {"name": "log_outcome", "arguments": {"event": BINS, "status": "done"}}])
        self.assertEqual(self.status(m, BINS), SpanStatus.PLANNED, results)
        self.assertIn(f"- [ ] {BINS}", self.body("n_errands"), results)
        self.assertNotIn(f"[x] {BINS}", self.body("n_today"), results)


if __name__ == "__main__":
    unittest.main()
