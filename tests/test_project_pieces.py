"""A project's pieces (project_pieces.py): time past a piece's end counts, what it displaced isn't missed, a
project's pieces go back to back, and an item with open pieces is offered as its first one. An invented project
list ("Build the garden shed"); DAY is Thu Oct 15."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_today import TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.backoff import MissStreaks
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.project_pieces import displaced_ids, overruns

SHED = ("- [ ] Order the timber\n"
        "- [ ] Paint the shed\n"
        "    - [ ] Primer coat\n"
        "    - [ ] Top coat\n"
        "- [ ] Fit the door\n")


def piece(tid, title, parent=None):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title="Build the garden shed", parent_note_id="n_shed", parent_task_id=parent)


class ProjectCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_shed"] = {"id": "n_shed", "title": "Build the garden shed", "body": SHED}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_shed", "project", "Build the garden shed")
        self.services.tasks += [piece("t_timber", "Order the timber"), piece("t_paint", "Paint the shed"),
                                piece("t_primer", "Primer coat", "t_paint"), piece("t_top", "Top coat", "t_paint"),
                                piece("t_door", "Fit the door")]
        for tid in ("t_timber", "t_paint", "t_door"):
            catalog.set_urgency(tid, "soon")
        self.services.tasks[:] = catalog.apply(self.services.tasks)

    def put(self, title, start, minutes=30, task_id=None, **extra):
        args = {"title": title, "start": start, "duration": f"{minutes} min", **extra}
        if task_id:
            args.update(task_id=task_id, category="task")
        out = self.t.execute("add_event", args)
        self.assertFalse(out.startswith("NOT_DONE"), out)
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def at_(self, title):
        s = next(s for s in self.t.day_plan.spans if s.title == title)
        return s.start_dt


class TestOverruns(ProjectCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("15:45"))
        self.timber = self.put("Order the timber", "2:00 PM", 60, "t_timber")
        self.door = self.put("Fit the door", "3:00 PM", 30, "t_door")

    def test_time_past_the_end_counts_for_the_piece_and_its_project(self):
        out = self.t.execute("log_outcome", {"event": self.timber.id, "status": "done", "started": "2:00 PM",
                                             "finished": "3:40 PM"})
        self.assertIn("40m past its planned end counts as time on Order the timber and on Build the garden shed",
                      out)
        rec = overruns(self.sched.state_db, DAY)[0]
        self.assertEqual((rec["piece"], rec["minutes"], rec["project"]),
                         ("Order the timber", 40, "Build the garden shed"))
        self.assertEqual([d["title"] for d in rec["displaced"]], ["Fit the door"])
        timber = next(s for s in self.t.day_plan.spans if s.title == "Order the timber")
        self.assertEqual((timber.actual_start, timber.actual_end), (at("14:00"), at("15:40")))  # its real times

    def test_what_it_displaced_moves_later_today_and_is_never_a_miss(self):
        self.t.execute("log_outcome", {"event": self.timber.id, "status": "done", "started": "2:00 PM",
                                       "finished": "3:40 PM"})
        door = next(s for s in self.t.day_plan.spans if s.title == "Fit the door")
        self.assertEqual(door.status, SpanStatus.PLANNED)  # not skipped, not missed
        self.assertGreaterEqual(door.start_dt, at("15:40"))  # after the overrun, where it fits
        self.assertIn(door.id, displaced_ids(self.sched.state_db, DAY))
        from thirties_core.carry import settle
        set_debug_time(at("09:00", 1))  # it was never done, and the day ended
        settle(self.t.assistant, DAY + timedelta(days=1))
        self.assertIsNone(MissStreaks(self.sched.state_db).get("Fit the door"))  # not counted for the back-off

    def test_with_no_room_today_it_waits_first_for_tomorrow(self):
        from thirties_core.carry import OpenPool
        self.put("Pottery class", "4:00 PM", 7 * 60, fixed=True)  # the rest of the evening is taken
        out = self.t.execute("log_outcome", {"event": self.timber.id, "status": "done", "started": "2:00 PM",
                                             "finished": "3:40 PM"})
        self.assertIn("Fit the door, first thing in tomorrow's plan", out)
        self.assertNotIn("Fit the door", [s.title for s in self.t.day_plan.spans])
        rec = OpenPool(self.sched.state_db).all()["fit the door"]
        self.assertEqual(rec["start"], (DAY + timedelta(days=1)).isoformat())

    def test_an_unrelated_item_displaced_isnt_missed_either(self):
        self.t.execute("remove_event", {"event": self.door.id})
        bank = self.put("Call the bank", "3:00 PM", 30, "t_bank")
        out = self.t.execute("log_outcome", {"event": self.timber.id, "status": "done", "started": "2:00 PM",
                                             "finished": "3:30 PM"})
        self.assertIn("Not missed, just moved: Call the bank", out)
        self.assertIn(bank.id, displaced_ids(self.sched.state_db, DAY))

    def test_a_done_with_no_time_said_isnt_an_overrun(self):
        self.t.execute("log_outcome", {"event": self.timber.id, "status": "done"})
        self.assertEqual(overruns(self.sched.state_db, DAY), [])
        self.assertEqual(self.at_("Fit the door"), at("15:00"))


class TestBackToBack(ProjectCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))

    def test_around_a_fixed_item_and_a_meal(self):
        from thirties_core.project_pieces import keep_together
        self.put("Lunch", "12:00 PM", 30)
        self.put("Order the timber", "11:30 AM", 30, "t_timber")
        self.put("Call the bank", "12:30 PM", 30, "t_bank")
        self.put("Fit the door", "1:30 PM", 30, "t_door")
        self.put("Dentist", "2:00 PM", 30, fixed=True)
        moved = keep_together(self.t)
        self.assertIn("Fit the door", moved)
        self.assertEqual(self.at_("Order the timber"), at("11:30"))
        self.assertEqual(self.at_("Lunch"), at("12:00"))  # a meal keeps its time, inside the stretch
        self.assertEqual(self.at_("Fit the door"), at("12:30"))  # straight after, past lunch
        self.assertEqual(self.at_("Call the bank"), at("13:00"))  # unrelated: after the stretch
        self.assertEqual(self.at_("Dentist"), at("14:00"))  # fixed: where it was

    def test_the_morning_proposal_keeps_them_together(self):
        # from 4:30: the half hour after work is left as room (breathing.py)
        out = self.t.execute("propose_day", {"items": [
            {"item": "t_timber", "start": "4:30 PM", "duration": "30 minutes"},
            {"item": "t_bank", "start": "5:00 PM", "duration": "30 minutes"},
            {"item": "t_door", "start": "6:30 PM", "duration": "30 minutes"}]})
        self.assertIn("4:30 PM – Order the timber\n- 5:00 PM – Fit the door\n- 5:30 PM – Call the bank", out)

    def test_redraw_keeps_them_together(self):
        self.put("Order the timber", "1:00 PM", 30, "t_timber")
        self.put("Call the bank", "1:30 PM", 30, "t_bank")
        self.put("Fit the door", "2:00 PM", 30, "t_door")
        self.t.execute("redraw", {})
        self.assertEqual(self.at_("Fit the door") - self.at_("Order the timber"), timedelta(minutes=30))
        self.assertGreater(self.at_("Call the bank"), self.at_("Fit the door"))


class TestPieces(ProjectCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))

    def test_an_item_with_open_pieces_is_offered_as_its_first_one(self):
        cands = {c["title"]: c for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertIn("Paint the shed: Primer coat", cands)
        self.assertEqual(cands["Paint the shed: Primer coat"]["task"].id, "t_primer")
        self.assertNotIn("Paint the shed", cands)
        self.put("Paint the shed: Primer coat", "1:00 PM", 30, "t_primer")  # on the plan: the next piece is offered
        cands = {c["title"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertIn("Paint the shed: Top coat", cands)

    def test_the_last_piece_ticked_leaves_the_item_open(self):
        primer = self.put("Paint the shed: Primer coat", "10:00 AM", 30, "t_primer")
        top = self.put("Paint the shed: Top coat", "10:30 AM", 30, "t_top")
        set_debug_time(at("11:05"))
        self.t.execute("log_outcome", {"event": primer.id, "status": "done"})
        self.assertIn("    - [x] Primer coat", self.body("n_shed"))
        self.assertIn("- [ ] Paint the shed", self.body("n_shed"))  # its other piece is open
        out = self.t.execute("log_outcome", {"event": top.id, "status": "done"})
        self.assertIn("    - [x] Top coat", self.body("n_shed"))
        self.assertIn("- [ ] Paint the shed", self.body("n_shed"))  # done only when the user ticks it
        self.assertIn("Paint the shed itself stays open", out)


class SteadyCase(ProjectCase):
    """A project's items are offered steadily, as small single pieces (sub-items too), not only
    when one is pressing; any work on the project counts."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))
        catalog = ListCatalog(self.sched.state_db)
        for tid in ("t_timber", "t_paint", "t_door"):
            catalog.set_urgency(tid, "whenever")  # nothing in it is pressing
        self.services.tasks[:] = catalog.apply(self.services.tasks)

    def cands(self):
        return self.t.assistant.proposals.candidates(self.t.day_plan)

    def record(self, *days_and_items):
        import json
        from thirties_core.project_pieces import OVERRUNS_KEY
        self.sched.state_db.set_sync_meta(OVERRUNS_KEY, json.dumps([
            {"day": (DAY - timedelta(days=back)).isoformat(), "piece": title, "task_id": tid,
             "project": "Build the garden shed", "minutes": minutes, "displaced": []}
            for back, title, tid, minutes in days_and_items]))


class TestSteadyPieces(SteadyCase):

    def test_one_piece_of_the_project_every_day_with_nothing_pressing(self):
        shed = [c for c in self.cands() if c["where"] == "Build the garden shed"]
        self.assertEqual([c["title"] for c in shed], ["Order the timber"])  # one small piece, the note's first
        self.assertIn("keeps Build the garden shed moving", shed[0]["why"])

    def test_the_next_piece_once_one_is_on_the_plan_or_done(self):
        self.sched.state_db.record_task_completed("t_timber", DAY - timedelta(days=1))
        self.assertEqual([c["title"] for c in self.cands() if c["where"] == "Build the garden shed"],
                         ["Paint the shed: Primer coat"])  # a sub-item, as its own piece

    def test_a_place_is_kept_for_it_when_the_day_is_full_of_pressing_things(self):
        catalog = ListCatalog(self.sched.state_db)
        extra = [TaskItem(id=f"t_e{i}", source_notebook="1. Tasks", title=f"Errand {i}", is_checkbox_item=True,
                          parent_note_title="Errands/Admin", parent_note_id="n_errands") for i in range(12)]
        self.services.tasks += extra
        for t in extra:
            catalog.set_urgency(t.id, "now")
        self.services.tasks[:] = catalog.apply(self.services.tasks)
        cands = self.cands()
        self.assertLessEqual(len(cands), 8)
        self.assertIn("Order the timber", [c["title"] for c in cands])

    def test_none_when_a_piece_of_it_is_already_on_the_day(self):
        self.put("Fit the door", "1:00 PM", 30, "t_door")
        self.assertEqual([c for c in self.cands() if c["where"] == "Build the garden shed"], [])

    def test_resting_after_misses_it_offers_the_next_item(self):
        streaks = MissStreaks(self.sched.state_db)
        for back in (3, 2, 1):
            streaks.record_day(DAY - timedelta(days=back), [{"title": "Order the timber", "task_id": "t_timber"}])
        titles = [c["title"] for c in self.cands() if c["where"] == "Build the garden shed"]
        self.assertEqual(titles, ["Paint the shed: Primer coat"])  # the next item instead

    def test_someday_items_are_never_offered(self):
        for t in self.services.tasks:
            if t.parent_note_id == "n_shed":
                t.someday = True
        self.assertEqual([c for c in self.cands() if c["where"] == "Build the garden shed"], [])


class TestMomentum(SteadyCase):
    """Time run past a piece's end two of the last three days is momentum: that part leads the project's
    next stretch and gets more time."""

    def test_two_overrun_days_lead_with_that_part_and_more_time(self):
        self.record((1, "Paint the shed: Primer coat", "t_primer", 40), (2, "Paint the shed: Primer coat", "t_primer", 20))
        shed = [c for c in self.cands() if c["where"] == "Build the garden shed"]
        self.assertEqual([c["title"] for c in shed], ["Paint the shed: Primer coat"])  # before Order the timber
        self.assertEqual(shed[0]["minutes"], 30 + 30)  # usual half hour, plus the overruns' average in thirties
        self.assertIn("on a roll", shed[0]["why"])

    def test_one_overrun_is_not_momentum(self):
        self.record((1, "Paint the shed: Primer coat", "t_primer", 40))
        self.assertEqual([c["title"] for c in self.cands() if c["where"] == "Build the garden shed"],
                         ["Order the timber"])

    def test_old_overruns_are_not_momentum(self):
        self.record((4, "Fit the door", "t_door", 40), (5, "Fit the door", "t_door", 40))
        self.assertEqual([c["title"] for c in self.cands() if c["where"] == "Build the garden shed"],
                         ["Order the timber"])

    def test_any_piece_of_the_same_item_counts(self):
        from thirties_core.project_pieces import momentum
        self.record((1, "Paint the shed: Primer coat", "t_primer", 30), (3, "Paint the shed: Top coat", "t_top", 90))
        hot = momentum(self.t.assistant, DAY)
        self.assertEqual(list(hot), ["t_paint"])
        self.assertEqual((hot["t_paint"]["days"], hot["t_paint"]["minutes"]), (2, 60))

    def test_the_part_with_momentum_leads_the_stretch(self):
        from thirties_core.project_pieces import keep_together
        self.record((1, "Fit the door", "t_door", 30), (2, "Fit the door", "t_door", 30))
        self.put("Order the timber", "1:00 PM", 30, "t_timber")
        self.put("Call the bank", "1:30 PM", 30, "t_bank")
        self.put("Fit the door", "2:00 PM", 30, "t_door")
        keep_together(self.t)
        self.assertEqual(self.at_("Fit the door"), at("13:00"))  # leads the shed stretch, where it began
        self.assertEqual(self.at_("Order the timber"), at("13:30"))
        self.assertEqual(self.at_("Call the bank"), at("14:00"))  # unrelated: after it

    def test_more_time_is_capped(self):
        from thirties_core.project_pieces import MOMENTUM_MAX_EXTRA, momentum_minutes
        self.assertEqual(momentum_minutes(30, 400), 30 + MOMENTUM_MAX_EXTRA)
        self.assertEqual(momentum_minutes(60, 10), 90)


if __name__ == "__main__":
    unittest.main()
