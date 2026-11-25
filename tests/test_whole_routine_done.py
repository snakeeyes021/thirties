"""A whole routine said done marks every step of it, in every part of the day, in one go.

A routine's name points to all of its steps: its name with "whole", "all" or "everything", or "both" / "all of
them" to a question that asked about its parts. Which items a done is for is the model's reading: a step left out
by an exception isn't ticked, and an item outside the routine goes on as the model read it.

Invented: a potter whose Kiln check is split in two parts, one before the firing and one after it, with a canoe trip
and a permit to renew on the side."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

EARLY = ["Read the pyrometer", "Log the cone pack", "Open the damper"]
LATE = ["Sweep the kiln shelf", "Wipe the glaze bench"]
TITLES = {"n_today": "Today To-Do", "n_kiln": "Kiln check", "n_odds": "Odds and ends", "n_dock": "Dock days"}


def note_list(api):
    return [{"id": nid, "title": TITLES.get(nid, ""), "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in api.notes.items()]


class KilnCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.api = FakeJoplinAPI({
            "n_today": {"id": "n_today", "body": ""},
            "n_kiln": {"id": "n_kiln", "body": "".join(f"- [ ] {s}\n" for s in EARLY + LATE)},
            "n_dock": {"id": "n_dock", "body": "- [ ] Wax the canoe hull\n- [ ] Coil the bow line\n"},
            "n_odds": {"id": "n_odds", "body": "- [ ] Renew the kiln permit\n"},
        })
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None
        joplin.fetch_active_notes.side_effect = lambda: note_list(self.api)
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_kiln", "routine"), ("n_dock", "routine"), ("n_odds", "list")):
            catalog.set_role(nid, role, TITLES[nid])
        permit = TaskItem(id="t_permit", source_notebook="1. Tasks", title="Renew the kiln permit",
                          is_checkbox_item=True, parent_note_title="Odds and ends", parent_note_id="n_odds")
        self.services = Services.for_db(self.sched.state_db, catalog.apply([permit]), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)
        out = self.t.execute("put_routine", {"routine": "Kiln check", "parts": [
            {"start": "9:45 AM", "items": EARLY}, {"start": "7:15 PM", "items": LATE}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        out = self.t.execute("put_routine", {"routine": "Dock days", "parts": [
            {"start": "8:30 PM", "items": ["Wax the canoe hull", "Coil the bow line"]}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.t.execute("add_event", {"title": "Renew the kiln permit", "start": "1:00 PM", "duration": "30 minutes",
                                     "task_id": "t_permit", "category": "task"})

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, title, shown=""):
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange = None, None

    def all_done(self, message, shown=""):
        return {title: self.done(message, title, shown) for title in EARLY + LATE}


class TestTheWholeRoutineByName(KilnCase):

    def assert_every_step_done(self, outs):
        for title, out in outs.items():
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertEqual(self.span(title).status, SpanStatus.DONE, title)

    def test_all_its_items_done(self):
        self.assert_every_step_done(self.all_done("kiln check wrapped up, all parts"))

    def test_the_whole_routine(self):
        self.assert_every_step_done(self.all_done("whole kiln check: finished"))

    def test_everything_in_it(self):
        self.assert_every_step_done(self.all_done("kiln check: everything in it got done"))

    def test_another_routine_and_a_list_item_are_the_models_reading(self):
        self.all_done("kiln check wrapped up, all parts")
        for title in ("Wax the canoe hull", "Renew the kiln permit"):
            out = self.done("kiln check wrapped up, all parts", title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
        self.assertIn("may also mean Kiln check", out)

    def test_with_an_exception_the_step_it_names_stays_open(self):
        message = "kiln check wrapped up; the sweep is still to do"
        out = self.done(message, "Sweep the kiln shelf")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Sweep the kiln shelf").status, SpanStatus.PLANNED)
        out = self.done(message, "Wipe the glaze bench")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_running_is_still_no_done(self):
        out = self.done("the whole kiln check is running now", "Read the pyrometer")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Read the pyrometer").status, SpanStatus.PLANNED)


class TestBothPartsAnswered(KilnCase):
    """The question asked about the two parts; "both" or "all of them" covers every step."""

    QUESTION = ("Read the pyrometer, Log the cone pack and Open the damper are early; Sweep the kiln shelf and Wipe "
                "the glaze bench come later. Which of those?")

    def test_both(self):
        for title, out in self.all_done("both of them, yes", self.QUESTION).items():
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")

    def test_all_of_them(self):
        for title, out in self.all_done("all of them", self.QUESTION).items():
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")

    def test_a_question_naming_the_routine(self):
        shown = "Is that the whole Kiln check, both parts?"
        for title, out in self.all_done("both", shown).items():
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")

    def test_both_beyond_the_routine_carries_a_note(self):
        out = self.done("Both", "Renew the kiln permit", self.QUESTION)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Renew the kiln permit wasn't in it", out)

    def test_both_without_a_question_about_it_is_unchanged(self):
        out = self.done("both", "Sweep the kiln shelf", "Next: Renew the kiln permit at 1:00 PM.")
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestTheWholeExchange(unittest.TestCase):
    """A mock conversation through the app's own service: the user says the routine is done once, and every step
    is marked in that turn. The same calls for words with no done in them are refused."""

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        from thirties_core import morning
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.sched = make_scheduler()
        self.api = FakeJoplinAPI({
            "n_today": {"id": "n_today", "body": ""},
            "n_kiln": {"id": "n_kiln", "body": "".join(f"- [ ] {s}\n" for s in EARLY + LATE)},
            "n_odds": {"id": "n_odds", "body": "- [ ] Renew the kiln permit\n"},
        })
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None
        joplin.fetch_tasks.return_value = [TaskItem(
            id="t_permit", source_notebook="1. Tasks", title="Renew the kiln permit", is_checkbox_item=True,
            parent_note_title="Odds and ends", parent_note_id="n_odds")]
        joplin.fetch_active_notes.side_effect = lambda: note_list(self.api)
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_kiln", "routine"), ("n_odds", "list")):
            catalog.set_role(nid, role, TITLES[nid])
        self.engine = MockInferenceEngine()
        svc = BackgroundService(self.sched.config, lambda *a: None, scheduler=self.sched, joplin_engine=joplin,
                                inference_engine=self.engine, sync_engine=MagicMock())
        svc.today = lambda: DAY
        svc.bridge = MagicMock()
        self.m = svc.manager()
        morning.mark_done(self.sched.state_db, DAY, at("07:30"))
        out = self.m.tools.execute("put_routine", {"routine": "Kiln check", "parts": [
            {"start": "9:45 AM", "items": EARLY}, {"start": "7:15 PM", "items": LATE}]})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.m.day_plan.spans if s.title == title)

    def turn(self, said, titles, answer):
        calls = "\n".join(f'CALL log_outcome {{"event": "{self.span(t).id}", "status": "done"}}' for t in titles)
        self.engine.set_next_response(reply(calls))
        self.engine.set_next_response(reply(answer))
        self.m.send_user_message(said)

    def test_said_once(self):
        self.turn("wrapped up the kiln check, all parts", EARLY + LATE, "Marked the whole Kiln check.")
        for title in EARLY + LATE:
            self.assertEqual(self.span(title).status, SpanStatus.DONE, title)

    def test_words_with_no_done_mark_nothing(self):
        self.turn("the canoe trip is next week", EARLY + LATE, "Noted.")
        for title in EARLY + LATE:
            self.assertEqual(self.span(title).status, SpanStatus.PLANNED, title)


if __name__ == "__main__":
    unittest.main()
