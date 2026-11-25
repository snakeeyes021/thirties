""""Both" or "all of them" to a question about groups of steps ticks every step of those groups, and one no takes
the whole batch back.

- **The groups asked about, every step of them.** The question before listed steps from two parts of the day's
  routines; "both, yes", "yep, both done" or "all of them" ticks each step of those two parts, whichever routine
  each part is in. "Both" means exactly two groups; "all" or "everything" takes in two or more.
- **Outside them, the model's reading.** A step of a part the question didn't ask about, or a list item, goes on
  as the model read the answer. An answer that makes an exception ("both, except the bailers") leaves out what it
  names; the other steps go on.
- **The reply names each step.** A reply that only says "Marked both done." is sent back once to name them; one
  that names the groups and every step goes as written.
- **One no takes them all back.** "no" or "undo" right after the reply that announced the batch reopens every
  step of it, on the plan and in Joplin. A no to a follow-up question about something else reopens nothing.

Through the real tool path on the mock Joplin API and through whole turns with a scripted model (no real calls).
Fixtures are invented: a sailing club's day on the water.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import reply
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE
from thirties_core.reopen import reply_before

RIGGING = ["Rig the dinghies", "Check the bailers", "Tension the stays"]
PACKING = ["Coil the halyards", "Rinse the sails", "Stow the rudders", "Lock the boat shed"]
EVENING = ["Hose the slipway", "Log the wind readings", "Hang the buoyancy aids"]
DECK = ["Sweep the clubhouse deck", "Empty the rain gauge", "Fold the race flags"]
BURGEES = "Order new burgees"
BATCH = RIGGING + PACKING


def series(titles):
    return ", ".join(titles[:-1]) + " and " + titles[-1]


TWO_PARTS = (f"Did you get through the morning rigging ({', '.join(RIGGING)}) and the afternoon packing "
             f"({', '.join(PACKING)})?")
THREE_PARTS = (f"How did the day go: the rigging ({', '.join(RIGGING)}), the packing ({', '.join(PACKING)}) and the "
               f"evening jobs ({', '.join(EVENING)})?")
ACROSS = f"Did the packing ({', '.join(PACKING)}) and the deck jobs ({', '.join(DECK)}) get done?"
ANNOUNCED = f"Marked both done: {series(BATCH)}."
WHOLE = ("both, yes", "yep, both done", "all of them", "yeah, the whole lot")


class ClubDay(TodayTestCase):
    """A Launch prep routine in three parts (3, 4 and 3 steps), a one-part Shore duty routine, and a list item,
    all past their time."""

    def setUp(self):
        super().setUp()
        TITLES.update({"n_launch": "Launch prep", "n_shore": "Shore duty", "n_club": "Club jobs"})
        self.addCleanup(lambda: [TITLES.pop(k, None) for k in ("n_launch", "n_shore", "n_club")])
        self.api.notes["n_launch"] = {"id": "n_launch", "body": "".join(
            f"- [ ] {s}\n" for s in RIGGING + PACKING + EVENING)}
        self.api.notes["n_shore"] = {"id": "n_shore", "body": "".join(f"- [ ] {s}\n" for s in DECK)}
        self.api.notes["n_club"] = {"id": "n_club", "body": f"- [ ] {BURGEES}\n"}
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_launch", "routine"), ("n_shore", "routine"), ("n_club", "list")):
            catalog.set_role(nid, role, TITLES[nid])
        self.services.tasks[:] = [item("t_burgees", BURGEES, "n_club")]
        self.services.tasks[0].list_role = "list"
        set_debug_time(at("21:10"))
        for routine, parts in (("Launch prep", (("9:40 AM", RIGGING), ("1:50 PM", PACKING), ("7:50 PM", EVENING))),
                               ("Shore duty", (("10:50 AM", DECK),))):
            out = self.t.execute("put_routine", {"routine": routine, "parts": [
                {"start": start, "items": steps} for start, steps in parts]})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        out = self.t.execute("add_event", {"title": BURGEES, "start": "4:20 PM", "duration": "20 min",
                                           "task_id": "t_burgees"})
        self.assertFalse(str(out).startswith(NOT_DONE), out)

    def span(self, title, plan=None):
        return next(s for s in (plan or self.t.day_plan).spans if s.title == title)

    def ticked(self, title):
        return any(f"[x] {title}" in n["body"] for n in self.api.notes.values())

    def say(self, message, title, shown):
        """The model calls log_outcome during a turn of the user's, right after the reply ``shown``."""
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        token, before = current_turn.set(f"[app] {message}"), reply_before.set(shown)
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"}))
        finally:
            current_turn.reset(token)
            reply_before.reset(before)
            self.t.user_message, self.t.prior_exchange = None, None

    def reopen(self, message, title, shown):
        """The model calls reopen_item during the user's turn, right after the reply ``shown``."""
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        token, before = current_turn.set(f"[app] {message}"), reply_before.set(shown)
        try:
            return str(self.t.execute("reopen_item", {"item": title}))
        finally:
            current_turn.reset(token)
            reply_before.reset(before)
            self.t.user_message, self.t.prior_exchange = None, None

    def fresh(self):
        self.doCleanups()
        self.setUp()

    def assert_done(self, outs):
        for title, out in outs.items():
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertEqual(self.span(title).status, SpanStatus.DONE, title)
            self.assertTrue(self.ticked(title), title)

    def assert_refused(self, outs):
        for title, out in outs.items():
            self.assertTrue(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertEqual(self.span(title).status, SpanStatus.PLANNED, title)
            self.assertFalse(self.ticked(title), title)


class TestTheGroupsAskedAbout(ClubDay):

    def test_a_whole_answer_ticks_every_step_of_both_parts(self):
        for message in WHOLE:
            with self.subTest(message=message):
                self.fresh()
                self.assert_done({t: self.say(message, t, TWO_PARTS) for t in BATCH})

    def test_a_part_it_did_not_ask_about_and_a_list_item_are_the_models_reading(self):
        for message in ("both, yes", "yep, both done"):
            with self.subTest(message=message):
                self.fresh()
                outs = {t: self.say(message, t, TWO_PARTS) for t in EVENING + DECK + [BURGEES]}
                self.assert_done(outs)

    def test_two_parts_of_two_routines(self):
        self.assert_done({t: self.say("both, yes", t, ACROSS) for t in PACKING + DECK})
        outs = {t: self.say("both, yes", t, ACROSS) for t in RIGGING}
        self.assert_done(outs)

    def test_both_for_three_groups_is_the_models_reading(self):
        self.assert_done({t: self.say("yep, both done", t, THREE_PARTS) for t in RIGGING + PACKING + EVENING})

    def test_all_takes_in_three(self):
        self.assert_done({t: self.say("yes, all done", t, THREE_PARTS) for t in RIGGING + PACKING + EVENING})

    def test_an_exception_leaves_out_what_it_names(self):
        self.assert_refused({"Check the bailers": self.say("both, except the bailers", "Check the bailers", TWO_PARTS)})
        self.assert_done({t: self.say("both, except the bailers", t, TWO_PARTS)
                          for t in ("Tension the stays", "Rinse the sails")})

    def test_the_not_marked_yet_list_under_it_is_not_a_group(self):
        from thirties_core.messages import join_sections
        from thirties_core.whats_next import unmarked_lines
        behind = [self.span(t) for t in BATCH + EVENING + DECK]
        shown = join_sections(TWO_PARTS, "\n".join(unmarked_lines(behind)))
        self.assert_done({t: self.say("yep, both done", t, shown) for t in BATCH})
        outs = {t: self.say("yep, both done", t, shown) for t in EVENING}
        self.assert_done(outs)

    def test_without_a_question_both_points_nowhere(self):
        shown = f"Next up: the rigging ({', '.join(RIGGING)}) and the packing ({', '.join(PACKING)})."
        self.assert_refused({t: self.say("both, yes", t, shown) for t in BATCH})


class TestOneNoTakesTheBatchBack(ClubDay):

    def tick_batch(self):
        self.assert_done({t: self.say("yep, both done", t, TWO_PARTS) for t in BATCH})

    def test_no_or_undo_right_after_the_announcement(self):
        for message in ("no", "nope", "undo"):
            with self.subTest(message=message):
                self.fresh()
                self.tick_batch()
                for title in BATCH:
                    out = self.reopen(message, title, ANNOUNCED)
                    self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
                    self.assertEqual(self.span(title).status, SpanStatus.PLANNED, title)
                    self.assertFalse(self.ticked(title), title)

    def test_a_no_to_a_follow_up_question_reopens_nothing(self):
        for shown in (f"{ANNOUNCED} Want me to move {BURGEES} to tomorrow at 9:20 AM?", f"{ANNOUNCED} Anything else?"):
            with self.subTest(shown=shown):
                self.fresh()
                self.tick_batch()
                for title in BATCH:
                    out = self.reopen("no", title, shown)
                    self.assertTrue(out.startswith(NOT_DONE), f"{title}: {out}")
                    self.assertEqual(self.span(title).status, SpanStatus.DONE, title)
                    self.assertTrue(self.ticked(title), title)


class TestWholeTurns(ClubDay):
    """The question, the answer and the reply as whole turns with a scripted model."""

    def setUp(self):
        super().setUp()
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        self.m.brain.messages.append({"role": "assistant", "content": TWO_PARTS})

    def turn(self, message, calls, *answers):
        before = len(self.m.brain.messages)
        self.engine.responses = [{"role": "assistant", "content": "", "tool_calls": calls}] + [
            reply(a) for a in answers]
        shown = self.m.send_user_message(message)
        new = self.m.brain.messages[before:]
        text = shown if isinstance(shown, str) else str(self.m.brain.messages[-1].get("content") or "")
        return text, new

    def checks(self, new):
        return [str(x.get("content")) for x in new if str(x.get("content") or "").startswith("[System check")
                and "which item you marked" in str(x.get("content"))]

    def results(self, new):
        return [str(x.get("content")) for x in new if x.get("role") == "tool"]

    def status(self, title):
        return self.span(title, self.m.day_plan).status

    def batch(self, *answers):
        calls = [{"name": "log_outcome", "arguments": {"event": t, "status": "done"}} for t in BATCH]
        text, new = self.turn("yep, both done", calls, *answers)
        for title in BATCH:
            self.assertEqual(self.status(title), SpanStatus.DONE, (title, self.results(new)))
            self.assertTrue(self.ticked(title), title)
        return text, new

    def test_a_reply_naming_the_groups_and_every_step_goes_as_written(self):
        text, new = self.batch(ANNOUNCED)
        self.assertFalse(self.checks(new))
        self.assertIn(ANNOUNCED, text)

    def test_a_reply_hiding_the_steps_is_sent_back(self):
        text, new = self.batch("Marked both done.", ANNOUNCED)
        self.assertEqual(len(self.checks(new)), 1, self.checks(new))
        for title in BATCH:
            self.assertIn(title, self.checks(new)[0])
        self.assertIn(ANNOUNCED, text)

    def test_still_hidden_it_opens_with_every_step(self):
        text, new = self.batch("Marked both done.", "Marked both done.")
        self.assertTrue(text.startswith("Marked "), text)
        for title in BATCH:
            self.assertIn(title, text)

    def test_then_one_no_takes_them_all_back(self):
        self.batch(ANNOUNCED)
        calls = [{"name": "reopen_item", "arguments": {"item": t}} for t in BATCH]
        text, new = self.turn("no", calls, f"Okay, all seven are open again: {series(BATCH)}.")
        for title in BATCH:
            self.assertEqual(self.status(title), SpanStatus.PLANNED, (title, self.results(new)))
            self.assertFalse(self.ticked(title), title)


if __name__ == "__main__":
    unittest.main()
