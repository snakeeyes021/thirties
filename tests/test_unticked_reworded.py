"""Unticked by hand, when the line no longer reads exactly as Thirties ticked it (unticked.py): the user fixed a
typo while unticking it. The line keeps its item (item_ids.py follows the rewording), so its done record must be
taken back all the same, or the item stays hidden for a week. And once a done record is taken back, the
conversation's own copy of the lists is read again, even if Joplin hasn't changed since its last read.
Invented lists, on a fake Joplin."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, temp_dir
from tests.test_unticked import UntickCase
from thirties_core.item_ids import ItemIds
from thirties_core.joplin_engine import checklist_tasks
from thirties_core.models import SpanStatus


class FollowedCase(UntickCase):
    """Items get the ids Thirties follows through edits, as the app's own reads give them."""

    def read_tasks(self):
        if not hasattr(self, "catalog"):
            return super().read_tasks()
        ids = ItemIds(self.sched.state_db)
        tasks = []
        for n in self.active_notes():
            tasks += checklist_tasks(n["id"], n["title"], n["source_notebook"], n["body"], ["Probation"],
                                     ids=ids.ids_for(n))
        return self.catalog.apply(tasks)

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "# Active\n- [ ] Call teh plumber\n- [ ] Buy stamps\n"
        self.services.tasks = self.read_tasks()

    def plumber_done_at_three(self):
        task = next(t for t in self.services.tasks if t.title == "Call teh plumber")
        self.t.execute("add_event", {"title": "Call teh plumber", "start": "3:00 PM", "duration": "30 min",
                                     "task_id": task.id})
        span = next(s for s in self.t.day_plan.spans if s.title == "Call teh plumber")
        from thirties_core.astronomy import set_debug_time
        set_debug_time(at("15:40"))
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        return task, span


class TestUntickedWithItsTypoFixed(FollowedCase):

    def test_the_reworded_line_is_not_done_after_all(self):
        task, span = self.plumber_done_at_three()
        self.assertIn("- [x] Call teh plumber", self.body("n_errands"))
        self.assertIn(task.id, self.done_ids())
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [x] Call teh plumber",
                                                                             "- [ ] Call the plumber")
        fresh = self.read_tasks()  # the app's next read: the reworded line is still the same item
        self.assertIn(task.id, {t.id for t in fresh if t.title == "Call the plumber"})
        before = self.writes()
        out = self.sweep(at("15:45"))
        self.assertTrue(any("Call the plumber" in line for line in out), out)
        self.assertNotIn(task.id, self.done_ids())  # offered again
        self.assertEqual(span.status, SpanStatus.PARTIAL)  # time spent on it, not done
        self.assertEqual(self.writes(), before)  # nothing written to Joplin
        self.assertIn("- [ ] Call the plumber", self.body("n_errands"))

    def test_a_new_line_in_place_of_a_ticked_one_isnt_taken_for_it(self):
        task, span = self.plumber_done_at_three()
        body = self.body("n_errands")  # the ticked line stays; an unrelated new one is added
        self.api.notes["n_errands"]["body"] = body + "- [ ] Water the ferns\n"
        self.read_tasks()
        self.assertEqual(self.sweep(at("15:45")), [])
        self.assertIn(task.id, self.done_ids())
        self.assertEqual(span.status, SpanStatus.DONE)


class TestTheConversationReadsTheListsAgain(UntickCase):
    """A turn read the lists while the item was still recorded done (so it was left out), then the sweep took
    the record back. Joplin's file hasn't changed since, but the next turn must read the lists again."""

    def test_a_done_record_taken_back_brings_the_item_back_to_the_assistant(self):
        from thirties_core.conversation import ConversationManager
        bank, _ = self.bank_done_at_three()
        self.untick_by_hand("Call the bank")
        engine = MagicMock()
        path = Path(temp_dir("thirties_joplin_")) / "database.sqlite"
        path.write_text("")
        engine.config.resolve_joplin_db_path.return_value = path
        engine.fetch_active_notes.return_value = []
        plan, pending = self.sched.build_day_plan(DAY)
        manager = ConversationManager(plan, [], pending, self.sched, joplin_engine=engine, active_notes=[])
        hidden = lambda: {r for r in self.sched.state_db.completed_since(DAY - timedelta(days=7))}

        def load(*_a, **_k):  # what load_backlog does with done records: hidden while recorded done
            return [t for t in self.read_tasks() if t.id not in hidden()], []
        from datetime import date

        class OnDay(date):
            @classmethod
            def today(cls):
                return DAY
        with patch("thirties_core.backlog.load_backlog", side_effect=load), patch("thirties_core.backlog.date", OnDay):
            manager.reload_backlog()  # a turn, before the sweep: still recorded done
            self.assertNotIn("Call the bank", [t.title for t in manager.brain.tasks])
            self.sweep(at("15:45"))  # the record is taken back; Joplin's file is unchanged
            manager.reload_backlog()  # the next turn
        self.assertIn("Call the bank", [t.title for t in manager.brain.tasks])
        self.assertIn("Call the bank", [t.title for t in manager.tools.services.tasks])


if __name__ == "__main__":
    import unittest
    unittest.main()
