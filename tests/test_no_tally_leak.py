"""The leftovers' tally the model is given never reaches the user (internal_text.py, leftover_facts.py).

The morning hands the model a one-line tally of the leftovers from before ("kept N from before; N of them on today's
plan; N not on today's plan; N proposed for dropping") so it can check its own words. A model that copied that line
into its reply showed the user bookkeeping. Now the tally is marked as the model's own, the model is given the one
plain sentence it may say instead, and a copy that still gets into a reply is rewritten into that sentence.

Invented: a potter on the late shift at a studio, with a canoe on days off."""

import re
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_morning_notice import MorningCase
from tests.test_unknown_stays_off_today import STUDIO, studio_item
from thirties_core.astronomy import set_debug_time
from thirties_core.inference import MockInferenceEngine
from thirties_core.internal_text import has_internal, strip_internal
from thirties_core.messages import clean_message

TALLY_RE = re.compile(r"of them on today's plan|not on today's plan|proposed for dropping|kept \d+ from before;",
                      re.IGNORECASE)
GIVEN_RE = re.compile(r"never copy it: (kept [^\n]*?)\) If you mention")


class CopyingEngine(MockInferenceEngine):
    """A mock model that copies the tally it was given into its morning reply, word for word."""

    def __init__(self):
        super().__init__()
        self.copied = ""

    def chat(self, messages, tools=None):
        self.call_history.append(list(messages))
        if not self.copied:
            for m in messages:
                found = GIVEN_RE.search(str(m.get("content") or ""))
                if found:
                    self.copied = found.group(1)
                    break
        return reply("☀️ Good morning! Studio odds and ends: " + self.copied
                     + " The full list sits in Before today.\nThe canoe can go out after the late shift.")


class MorningService(unittest.TestCase):
    """A whole morning through the app's service with a mock model: the first message of the day builds it."""

    engine_class = MockInferenceEngine

    def setUp(self):
        set_debug_time(at("09:45"))
        self.addCleanup(set_debug_time, None)
        from thirties_core.background import BackgroundService
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value = [studio_item(tid, title) for tid, title in STUDIO]
        joplin.fetch_active_notes.return_value = []
        self.engine = self.engine_class()
        self.svc = BackgroundService(self.sched.config, lambda *a: None, scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.m = self.svc.manager()
        for i, tid in enumerate(("t_paddle", "t_clay")):  # unmarked yesterday: both are kept this morning
            title = next(t for i_, t in STUDIO if i_ == tid)
            self.m.tools.execute("add_event", {"title": title, "start": f"{1 + i}:40 PM", "duration": "30 minutes",
                                               "day": (DAY - timedelta(days=1)).isoformat(), "task_id": tid,
                                               "category": "task"})
        # already on today's plan before the morning: one kept item is on it, the other waits
        self.m.tools.execute("add_event", {"title": "Sand the spare paddle", "start": "4:10 PM",
                                           "duration": "30 minutes", "task_id": "t_paddle", "category": "task"})

    def instruction(self):
        return next(m["content"] for m in self.engine.call_history[0]
                    if m["role"] == "user" and "first message of the day" in str(m["content"]))


class TestTheCopiedTallyIsSaidPlainly(MorningService):
    """The leak itself: the model copies the tally from the morning's brief into its reply."""

    engine_class = CopyingEngine

    def test_the_copied_line_reaches_the_user_as_one_plain_sentence(self):
        out = self.m.send_user_message("awake, kiln is cooling")
        self.assertIn("kept 2 from before; 1 of them on today's plan; 1 not on today's plan", self.engine.copied)
        self.assertIsNone(TALLY_RE.search(out), out)
        self.assertIn("1 thing from before is waiting in Before today.", out)
        self.assertNotIn("The full list sits in Before today.", out)  # said once, not twice
        self.assertIn("The canoe can go out after the late shift.", out)  # the rest of the reply stands


class TestTheModelIsToldTheTallyIsItsOwn(MorningService):

    def test_the_morning_brief_marks_the_tally_and_offers_the_plain_sentence(self):
        self.engine.set_next_response(reply("☀️ Good morning! The canoe can go out after the late shift."))
        self.m.send_user_message("awake, kiln is cooling")
        text = self.instruction()
        self.assertIn("for the assistant only, never copy it: kept 2 from before;", text)
        self.assertIn('say at most: "1 thing from before is waiting in Before today."', text)
        self.assertNotIn("say these counts", text)
        self.assertNotIn("how many were kept and how many are on today's plan", text)


class TestProposeLeftoversResult(MorningCase):

    def test_the_result_marks_the_tally_and_offers_the_plain_sentence(self):
        self.task("t_permit").due = DAY.isoformat()
        for i, tid in enumerate(("t_permit", "t_clay")):
            title = next(t for i_, t in STUDIO if i_ == tid)
            self.t.execute("add_event", {"title": title, "start": f"{1 + i}:40 PM", "duration": "30 min",
                                         "day": (DAY - timedelta(days=1)).isoformat(), "task_id": tid,
                                         "category": "task"})
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("propose_leftovers", {"keep": [{"item": "Renew the kiln permit", "when": "today"}],
                                                   "drop": [{"item": "Order a bag of porcelain"}]})
        self.assertIn("for the assistant only, never copy it: kept 1 from before; 1 of them on today's plan", out)
        self.assertIn('say at most: "1 thing from before is waiting in Before today."', out)
        self.assertNotIn("exactly these counts", out)
        # copied whole into a reply, the tally still never reaches the user
        line = re.search(r"never copy it: (kept [^)]*)\)", out).group(1)
        shown = clean_message("Done. " + line)
        self.assertIsNone(TALLY_RE.search(shown), shown)
        self.assertIn("1 thing from before is waiting in Before today.", shown)


class TestAReplyCarryingTheTallyIsCleaned(unittest.TestCase):

    def test_the_bare_tally_line(self):
        text = ("☀️ Good morning!\nFrom the shelf: kept 5 from before; 2 of them on today's plan; 3 not on today's "
                "plan; 1 proposed for dropping. Everything else is in Before today.\n10:40 AM – Trim the bowls")
        self.assertTrue(has_internal(text))
        out = strip_internal(text)
        self.assertEqual(out, "☀️ Good morning!\n4 things from before are waiting in Before today.\n"
                              "10:40 AM – Trim the bowls")

    def test_the_tally_with_names_from_the_rewrite_check(self):
        text = ("kept 2 from before; 1 of them on today's plan (Sand the spare paddle at 4:30 PM); 1 not on today's "
                "plan (Order a bag of porcelain (Thursday)); 1 proposed for dropping (Mix a bucket of slip).\n"
                "The canoe waits for Sunday.")
        out = clean_message(text)
        self.assertIsNone(TALLY_RE.search(out), out)
        self.assertEqual(out, "2 things from before are waiting in Before today.\nThe canoe waits for Sunday.")

    def test_in_words_and_nothing_waiting(self):
        out = strip_internal("Morning! kept three from before; three of them on today's plan; none not on today's "
                             "plan; zero proposed for dropping. Glazing starts at 11:10 AM.")
        self.assertEqual(out, "Morning! Glazing starts at 11:10 AM.")


class TestThePlainWordsStillReachTheUser(MorningService):

    def test_the_plain_sentence_goes_out_as_written(self):
        said = ("☀️ Good morning! 1 thing from before is waiting in Before today.\n"
                "The canoe can go out after the late shift.")
        self.engine.set_next_response(reply(said))
        out = self.m.send_user_message("awake, kiln is cooling")
        self.assertIn("1 thing from before is waiting in Before today.", out)
        self.assertIn("The canoe can go out after the late shift.", out)
        sent_back = [m for call in self.engine.call_history for m in call
                     if m.get("role") == "user" and "doesn't match the plan" in str(m.get("content") or "")]
        self.assertEqual(sent_back, [])

    def test_plain_count_sentences_are_left_alone(self):
        for text in ("2 things from before are waiting in Before today.",
                     "Kept 2 from before, 1 on today's plan (sanding the spare paddle); the porcelain waits.",
                     "Two of them on the wheel; one not on the shelf yet."):
            self.assertFalse(has_internal(text), text)
            self.assertEqual(strip_internal(text), text)


if __name__ == "__main__":
    unittest.main()
