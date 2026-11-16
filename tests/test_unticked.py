"""A line Thirties ticked and the user unticked by hand before it was filed is not done after all (unticked.py):
the record of it being done is taken back, it's offered again, the day's item for it counts as time spent rather
than finished, and Thirties writes nothing to the notes. If Thirties recorded it done but its check mark never
arrived in the notes, the item is still held back. Invented lists, on a fake Joplin."""

from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.done_log import DoneLog
from thirties_core.joplin_writeback import WriteLog
from thirties_core.models import SpanStatus


class UntickCase(ListsTestCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "# Active\n- [ ] Call the bank\n- [ ] Buy stamps\n"
        self.log = DoneLog(self.sched.state_db)
        self.sweep()  # Thirties' first look

    def sweep(self, when=None, log=None):
        if when is not None:
            set_debug_time(when)
        return (log or self.log).sweep(self.t.assistant)

    def task(self, title):
        return next(t for t in self.services.tasks if t.title == title)

    def done_ids(self):
        return self.sched.state_db.completed_since(DAY - timedelta(days=7))

    def untick_by_hand(self, title):
        body = self.body("n_errands")
        self.assertIn(f"- [x] {title}", body)
        self.api.notes["n_errands"]["body"] = body.replace(f"- [x] {title}", f"- [ ] {title}")

    def writes(self):
        return len(WriteLog(self.sched.state_db).since(""))

    def bank_done_at_three(self):
        bank = self.task("Call the bank")
        self.t.execute("add_event", {"title": "Call the bank", "start": "3:00 PM", "duration": "30 min",
                                     "task_id": bank.id})
        span = next(s for s in self.t.day_plan.spans if s.title == "Call the bank")
        set_debug_time(at("15:40"))
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        return bank, span


class TestUntickedByHand(UntickCase):

    def test_ticked_then_unticked_by_hand_is_not_done_after_all(self):
        bank, span = self.bank_done_at_three()
        self.assertIn(bank.id, self.done_ids())
        self.assertEqual(span.status, SpanStatus.DONE)
        self.untick_by_hand("Call the bank")
        before, body = self.writes(), self.body("n_errands")
        out = self.sweep(at("15:45"))
        self.assertIn("Call the bank unticked by hand in Errands/Admin: not done after all", out)
        self.assertNotIn(bank.id, self.done_ids())  # offered again
        self.assertIn(bank.id, {t.id for t in self.t.assistant.s.tasks})
        self.assertEqual(span.status, SpanStatus.PARTIAL)  # time spent on it, not done
        self.assertEqual((span.actual_start, span.actual_end), (at("15:00"), at("15:40")))  # its times kept
        stored = next(s for s in self.sched.state_db.get_spans_between(at("07:00"), at("07:00", 1))
                      if s.id == span.id)
        self.assertEqual(stored.status, SpanStatus.PARTIAL)
        self.assertEqual(self.writes(), before)  # nothing written to Joplin, never ticked again
        self.assertEqual(self.body("n_errands"), body)
        self.sweep(at("16:30"))  # later sweeps: it stays open and isn't filed
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))
        self.assertEqual(self.sweep(at("17:00")), [])  # said once

    def test_the_backlog_offers_it_again(self):
        from datetime import date
        from unittest.mock import patch
        from thirties_core.backlog import load_backlog

        class OnDay(date):
            @classmethod
            def today(cls):
                return DAY

        def titles():
            with patch("thirties_core.backlog.date", OnDay):
                return [t.title for t in load_backlog(self.sched.config, self.services.joplin, self.sched)[0]]
        self.bank_done_at_three()
        self.untick_by_hand("Call the bank")
        self.assertNotIn("Call the bank", titles())  # hidden while it's recorded done
        self.sweep(at("15:45"))
        self.assertIn("Call the bank", titles())

    def test_settled_on_the_first_sweep_after_a_restart(self):
        bank, span = self.bank_done_at_three()
        self.untick_by_hand("Call the bank")  # while the app was closed
        self.sweep(at("15:45"), log=DoneLog(self.sched.state_db))  # a fresh start: only what's on the record
        self.assertNotIn(bank.id, self.done_ids())
        self.assertEqual(span.status, SpanStatus.PARTIAL)


class TestDoneStandsOtherwise(UntickCase):

    def test_a_done_whose_tick_never_reached_joplin_stays_hidden(self):
        stamps = self.task("Buy stamps")
        self.sched.state_db.record_task_completed(stamps.id, DAY)  # done, and Joplin never got the tick
        self.assertEqual(self.sweep(at("15:00")), [])
        self.assertIn(stamps.id, self.done_ids())
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))  # not ticked now either

    def test_still_ticked_is_still_done(self):
        bank, span = self.bank_done_at_three()
        self.sweep(at("15:45"))
        self.assertIn(bank.id, self.done_ids())
        self.assertEqual(span.status, SpanStatus.DONE)

    def test_another_copy_still_ticked_isnt_taken_as_unticked(self):
        self.api.notes["n_errands"]["body"] = "# Active\n- [ ] Water the moss\n- [ ] Water the moss\n"
        self.t.assistant.s.tasks = self.read_tasks()
        moss = next(t for t in self.t.assistant.s.tasks if t.title == "Water the moss")
        self.t.execute("log_outcome", {"event": moss.id, "status": "done"})
        self.assertIn("- [x] Water the moss", self.body("n_errands"))
        self.sweep(at("14:05"))
        self.assertIn(moss.id, self.done_ids())

    def test_thirties_own_undo_is_left_to_it(self):
        bank, _ = self.bank_done_at_three()
        self.t.execute("reopen_item", {"item": "Call the bank"})  # its own untick, recorded as such
        self.assertEqual(self.sweep(at("15:45")), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
