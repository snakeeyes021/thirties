"""A bare done right after a reply that listed several open items.

- **The model's pick stands.** "that one's sorted", "done!", "finished it": no word of any item in it, so which of
  the open items just listed it is, is the model's to pick. Whichever listed item it ticks is ticked, on the plan
  and in Joplin.
- **Outside what was listed, a note.** An item the reply didn't list, a count or "both" that doesn't fit the list,
  or words that name another item go on as the model read them, with a note when there's something to name. No done
  word at all is still refused, and so is any pick after a reply that asked which.
- **The reply says which.** A reply that doesn't name the ticked item is sent back once; if it still doesn't, it
  opens with what was ticked. A reply that names it goes as written.
- **One word takes it back.** "no", "not that one", "undo" right after such a tick reopens it, on the plan and in
  Joplin. A "no" reopens nothing the reply didn't name.

Through the real tool path on the mock Joplin API and through whole turns with a scripted model (no real calls).
Fixtures are invented: an office day.
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

BINDERS, BADGES, ROOM, TENDER = ("Collate the budget binders", "Laminate the visitor badges",
                                 "Reserve the east boardroom", "Proofread the tender summary")
ITEMS = ((BINDERS, "9:20 AM", "t_binders"), (BADGES, "9:50 AM", "t_badges"), (ROOM, "10:40 AM", "t_room"),
         (TENDER, "11:10 AM", "t_tender"))
LISTED = (BINDERS, BADGES, ROOM)
SHOWN = f"Still open: {BINDERS}, {BADGES} and {ROOM}."
BARE = ("that one's sorted", "done!", "finished it", "that's done now")
POINTED_BACK = "points back to a reply that named"


class OfficeDay(TodayTestCase):
    """Four office items from one list, each on the plan; the reply before listed three of them as still open."""

    def setUp(self):
        super().setUp()
        TITLES["n_office"] = "Office day"
        self.addCleanup(TITLES.pop, "n_office", None)
        self.api.notes["n_office"] = {"id": "n_office", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_office", "list", "Office day")
        self.services.tasks[:] = [item(tid, title, "n_office") for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        set_debug_time(at("11:40"))

    def span(self, title, plan=None):
        return next(s for s in (plan or self.t.day_plan).spans if s.title == title)

    def say(self, message, title, shown=SHOWN, status="done"):
        """The model calls log_outcome during a turn of the user's: the plan and every Joplin write see their words."""
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", shown), None
        token = current_turn.set(f"[app] {message}")
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": status}))
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def ticked(self, title):
        done = "".join(n.get("body") or "" for n in self.api.notes.values()
                       if (n.get("title") or "").startswith("Done "))
        return f"[x] {title}" in self.body("n_today") + self.body("n_office") or title in done

    def assertTicked(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertTrue(self.ticked(title), out)

    def assertOpen(self, title, out=""):
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertFalse(self.ticked(title), out)

    def fresh(self):
        self.doCleanups()
        self.setUp()


class TestThePickStands(OfficeDay):

    def test_any_listed_item_the_model_picks(self):
        for message in BARE:
            for title in LISTED:
                with self.subTest(message=message, item=title):
                    self.fresh()
                    out = self.say(message, title)
                    self.assertFalse(out.startswith(NOT_DONE), out)
                    self.assertTicked(title, out)
                    for other in LISTED:
                        if other != title:
                            self.assertOpen(other, out)

    def test_never_after_a_question_asking_which(self):
        out = self.say("done!", ROOM, shown=f"Which did you finish: {BINDERS}, {BADGES} or {ROOM}?")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Ask again which one", out)
        self.assertOpen(ROOM, out)


class TestOutsideTheListIsANote(OfficeDay):
    """A done the model read for an item outside what the reply listed goes on; its result says what else fits."""

    def test_an_item_the_reply_did_not_list(self):
        out = self.say("that one's sorted", TENDER)
        self.assertIn(POINTED_BACK, out)
        self.assertTicked(TENDER, out)

    def test_nothing_listed_before(self):
        out = self.say("done!", BADGES, shown="Good to hear from you.")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertTicked(BADGES, out)

    def test_a_count_that_does_not_fit(self):
        for message in ("both done", "those two are finished"):
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, BADGES)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertTicked(BADGES, out)

    def test_words_that_name_another_item(self):
        out = self.say("the binders are collated", BADGES)
        self.assertIn(f"fits {BINDERS} and says nothing of {BADGES}", out)
        self.assertTicked(BADGES, out)

    def test_an_item_already_done_is_still_listed(self):
        self.t.by_hand = True
        self.t.execute("log_outcome", {"event": self.span(BINDERS).id, "status": "done"})
        self.t.by_hand = False
        out = self.say("finished it", TENDER)
        self.assertIn(POINTED_BACK, out)
        self.assertTicked(TENDER, out)


class TestStillRefused(OfficeDay):

    def test_no_done_word(self):
        for message in ("that one", "ok", "that one isn't done yet"):
            with self.subTest(message=message):
                out = self.say(message, BADGES)
                self.assertTrue(out.startswith(NOT_DONE), out)
                self.assertOpen(BADGES, out)

class WholeTurns(OfficeDay):
    """Whole turns with a scripted model, one conversation across turns: what the plan and the reply say."""

    def setUp(self):
        super().setUp()
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        self.m.brain.messages.append({"role": "assistant", "content": SHOWN})

    def status(self, title):
        return self.span(title, self.m.day_plan).status

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

    def tick(self, message="that one's sorted", title=BADGES, *answers):
        text, new = self.turn(message, [{"name": "log_outcome", "arguments": {"event": title, "status": "done"}}],
                              *(answers or (f"Marked {title} done.",)))
        self.assertEqual(self.status(title), SpanStatus.DONE, self.results(new))
        return text, new


class TestTheReplySaysWhich(WholeTurns):

    def test_a_reply_naming_it_goes_as_written(self):
        text, new = self.tick()
        self.assertIn(f"Marked {BADGES} done.", text)
        self.assertFalse(self.checks(new))

    def test_a_reply_that_does_not_say_is_sent_back_once(self):
        text, new = self.tick("done!", ROOM, "Got it.", f"{ROOM} is ticked off.")
        self.assertEqual(len(self.checks(new)), 1, self.checks(new))
        self.assertIn(ROOM, self.checks(new)[0])
        self.assertIn(f"{ROOM} is ticked off.", text)

    def test_still_not_said_it_opens_with_what_was_ticked(self):
        text, new = self.tick("finished it", BINDERS, "Nice work.", "Nice work.")
        self.assertTrue(text.startswith(f"Marked {BINDERS} done."), text)

    def test_a_named_done_is_never_sent_back(self):
        text, new = self.tick("the badges are laminated", BADGES, "Nice.")
        self.assertFalse(self.checks(new))


class TestOneWordTakesItBack(WholeTurns):
    UNDO = ("no", "not that one", "undo", "nope")

    def test_right_after_the_tick(self):
        for message in self.UNDO:
            for tool in ("reopen_item", "log_outcome"):
                with self.subTest(message=message, tool=tool):
                    self.fresh()
                    self.tick()
                    args = ({"item": BADGES} if tool == "reopen_item" else {"event": BADGES, "status": "open"})
                    _, new = self.turn(message, [{"name": tool, "arguments": args}],
                                       f"Okay, {BADGES} is open again.")
                    self.assertEqual(self.status(BADGES), SpanStatus.PLANNED, self.results(new))

    def test_a_no_reopens_nothing_the_reply_did_not_name(self):
        self.t.by_hand = True
        self.t.execute("log_outcome", {"event": self.span(TENDER).id, "status": "done"})
        self.t.by_hand = False
        self.tick()
        _, new = self.turn("no", [{"name": "reopen_item", "arguments": {"item": TENDER}}], "Okay.")
        self.assertTrue(any(r.startswith(NOT_DONE) for r in self.results(new)), self.results(new))
        self.assertEqual(self.status(TENDER), SpanStatus.DONE)
        self.assertEqual(self.status(BADGES), SpanStatus.DONE)


class TestTakenBackInJoplin(OfficeDay):
    """The same undo through the tool path, where the Joplin line is checked too."""
    AFTER = (f"that one's sorted", f"Marked {BADGES} done.")

    def reopen(self, message, title=BADGES):
        self.t.user_message, self.t.prior_exchange = message, self.AFTER
        token, before = current_turn.set(f"[app] {message}"), reply_before.set(self.AFTER[1])
        try:
            return str(self.t.execute("reopen_item", {"item": title}))
        finally:
            current_turn.reset(token)
            reply_before.reset(before)
            self.t.user_message, self.t.prior_exchange = None, None

    def test_one_word_unticks_it(self):
        for message in TestOneWordTakesItBack.UNDO:
            with self.subTest(message=message):
                self.fresh()
                out = self.say("that one's sorted", BADGES)
                self.assertTicked(BADGES, out)
                out = self.reopen(message)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertOpen(BADGES, out)
                self.assertIn(f"- [ ] {BADGES}", self.body("n_office"), out)

    def test_joplin_takes_the_untick(self):
        from thirties_core.write_guard import refused
        before = reply_before.set(self.AFTER[1])
        self.addCleanup(reply_before.reset, before)
        for message in TestOneWordTakesItBack.UNDO:
            self.assertEqual(refused({"op": "untick", "title": BADGES, "note_id": "n_office"}, f"[app] {message}",
                                     None), "", message)
        self.assertTrue(refused({"op": "untick", "title": BADGES, "note_id": "n_office"},
                                "[app] no, the tender summary needs another pass", None))


if __name__ == "__main__":
    unittest.main()
