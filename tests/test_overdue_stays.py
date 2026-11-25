"""An overdue item (due before today, not done) is held on today the same way as one due today (due_today.py):
Thirties' own planning keeps it, a vague word keeps it and says it's overdue, a clear request naming the item and a
day moves it and says so. Every way off today is run twice, for an overdue item and for one due today, and an item
with no due date near still moves freely. Invented fixtures: someone who works late shifts at a pottery studio and
paddles on weekends."""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import DAY, at
from tests.test_if_time import Base
from tests.test_on_site import InPersonBase
from thirties_core.carry import OpenPool
from thirties_core.day_lists import AheadLists, pencilled_in
from thirties_core.due_today import deadline_words, held_due, moved_line, stays_line
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE

TOMORROW = DAY + timedelta(days=1)
KILN, PADDLES, TILES = "Renew the kiln permit", "Return the canoe paddles", "Glaze the test tiles"
STUDIO = "- [ ] Renew the kiln permit\n- [ ] Return the canoe paddles\n- [ ] Glaze the test tiles\n"


def studio_item(tid, title):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title="Studio and river", parent_note_id="n_studio")


class StudioBase(Base):
    """Today's plan with a studio-and-river list. ``offset``: how many days from today the held item is due."""
    offset = 0

    def setUp(self):
        super().setUp()
        self.a = self.t.assistant
        self.api.notes["n_studio"] = {"id": "n_studio", "title": "Studio and river", "body": STUDIO}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_studio", "list", "Studio and river")
        self.services.tasks = catalog.apply(list(self.services.tasks) + [
            studio_item("t_kiln", KILN), studio_item("t_paddles", PADDLES), studio_item("t_tiles", TILES)])

    def due(self, title, task_id="", offset=None):
        """Due ``offset`` days from today (the class's own by default), the way the lists keep it."""
        day = DAY + timedelta(days=self.offset if offset is None else offset)
        ListCatalog(self.sched.state_db).set_urgency(task_id or f"line:{title}", "soon", due=day, title=title)
        for t in self.services.tasks:
            if t.id == task_id:
                t.due = day.isoformat()
        return day

    def words(self):
        """How the answers name the deadline: "due today" or "overdue (it was due Monday)"."""
        return deadline_words(DAY + timedelta(days=self.offset), DAY)

    def was(self):
        return "overdue" if self.offset < 0 else "due today"

    def planned(self, title, plan=None):
        return [s for s in (plan or self.t.day_plan).spans if s.title == title and s.status == SpanStatus.PLANNED]

    def kiln_today(self):
        self.due(KILN, "t_kiln")
        self.say("renew the kiln permit today", "add_to_today", {"items": [KILN]})
        return self.planned(KILN)[0]


class HeldPaths:
    """Every way off today, for an item the class makes due today or overdue."""

    def test_if_theres_time_vague_keeps_it_clear_moves_it(self):
        self.due(PADDLES, "t_paddles")
        self.say("the canoe paddles if there's time", "add_to_today", {"items": [PADDLES]})
        for said, action in (("not today for the paddles", "tomorrow"), ("paddles later this week", "week")):
            out = self.say(said, "if_there_is_time", {"item": PADDLES, "action": action})
            self.assertTrue(out.startswith(NOT_DONE), out)
            self.assertIn(f"{PADDLES} is {self.words()}, so it stays on today", out)
        self.assertIn(PADDLES, self.body("n_today"))
        self.assertEqual(AheadLists(self.sched.state_db).for_day(TOMORROW), [])
        out = self.say("move the canoe paddles to tomorrow", "if_there_is_time", {"item": PADDLES,
                                                                                 "action": "tomorrow"})
        self.assertIn(f"{PADDLES} was {self.was()}; it's on tomorrow now.", out)
        self.assertNotIn(PADDLES, self.body("n_today"))

    def test_if_theres_time_on_a_timed_line(self):
        self.kiln_today()
        out = self.say("not today for the kiln permit", "if_there_is_time", {"item": KILN, "action": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertTrue(self.planned(KILN))
        out = self.say("move the kiln permit to tomorrow", "if_there_is_time", {"item": KILN, "action": "tomorrow"})
        self.assertIn(f"{KILN} was {self.was()}; it's on tomorrow now.", out)
        self.assertEqual(self.planned(KILN), [])

    def test_a_skip_stands_and_says_the_deadline(self):
        span = self.kiln_today()
        out = self.say("skip the kiln permit today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn(f"{KILN} is {self.words()}.", out)  # a fact for the model, never a hold
        self.assertNotIn(KILN, self.body("n_today"))
        self.assertNotIn(KILN, pencilled_in(self.a))

    def test_move_to_another_day(self):
        span = self.kiln_today()
        out = self.say("the kiln permit can wait", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(f"{KILN} is {self.words()}, so it stays on today", out)
        self.assertTrue(self.planned(KILN))
        out = self.say("move the kiln permit to tomorrow", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertIn(f"{KILN} was {self.was()}; it's on tomorrow now.", out)
        self.assertEqual(self.planned(KILN), [])
        self.assertTrue(self.planned(KILN, self.t.plan_for(TOMORROW)))

    def test_onto_a_later_days_list(self):
        self.kiln_today()
        out = self.say("the kiln permit, maybe later", "add_to_today", {"items": [KILN], "day": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(AheadLists(self.sched.state_db).for_day(TOMORROW), [])
        self.assertIn(f"- [ ] {KILN}", self.body("n_today"))
        out = self.say("put the kiln permit on tomorrow's list", "add_to_today", {"items": [KILN], "day": "tomorrow"})
        self.assertIn(f"{KILN} was {self.was()}; it's on tomorrow now.", out)

    def test_remove_and_clear(self):
        span = self.kiln_today()
        out = self.say("skip the kiln permit today", "remove_event", {"event": span.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn(f"{KILN} is {self.words()}.", out)  # a fact for the model, never a hold
        self.assertNotIn(KILN, self.body("n_today"))
        self.due("Wax the canoe hull")  # a line that lives only on Today
        self.api.notes["n_today"]["body"] += "- [ ] Wax the canoe hull\n"
        out = self.say("skip the canoe hull today", "remove_event", {"event": "Wax the canoe hull"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn(f"Wax the canoe hull is {self.words()}.", out)
        self.assertNotIn("Wax the canoe hull", self.body("n_today"))

    def test_pin_to_a_later_period(self):
        self.due(KILN, "t_kiln")
        out = self.say("the kiln permit can wait", "pin_to_period", {"task": KILN, "when": "next week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(f"{KILN} is {self.words()}, so it stays on today", out)
        out = self.say("pin the kiln permit to next week", "pin_to_period", {"task": KILN, "when": "next week"})
        self.assertIn(f"{KILN} was {self.was()}; it's on ", out)

    def test_placing_a_leftover(self):
        due = self.due(PADDLES, "t_paddles")
        OpenPool(self.sched.state_db).add([{"title": PADDLES, "task_id": "t_paddles", "horizon": "week",
                                            "due": due.isoformat()}], DAY - timedelta(days=1))
        out = self.say("the paddles this week", "place_leftover", {"item": PADDLES, "horizon": "this week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(f"{PADDLES} is {self.words()}, so it stays on today", out)
        out = self.say("move the canoe paddles to tomorrow", "place_leftover", {"item": PADDLES,
                                                                               "horizon": "tomorrow"})
        self.assertIn(f"{PADDLES} was {self.was()}; it's on tomorrow now.", out)

    def test_carry_over_keeps_it_today(self):
        from thirties_core.carry import TODAY, choose_horizon
        self.due(PADDLES, "t_paddles")
        task = next(t for t in self.services.tasks if t.id == "t_paddles")
        self.assertEqual(choose_horizon(self.a, task, task.title, DAY), TODAY)

    def test_thirties_own_leftover_proposal_keeps_it_today(self):
        self.t.execute("add_event", {"title": PADDLES, "start": "7:00 PM", "duration": "30 min",
                                     "task_id": "t_paddles", "day": (DAY - timedelta(days=1)).isoformat()})
        self.due(PADDLES, "t_paddles")
        self.a.start_the_day()
        self.t.execute("rollover_proposal", {})
        keep = [{"item": p["title"], "when": "this week"} for p in self.a.before.rows()["keep"]]
        self.assertIn(PADDLES, [k["item"] for k in keep])
        self.t.execute("propose_leftovers", {"keep": keep})
        self.assertEqual(OpenPool(self.sched.state_db).all()[PADDLES.lower()]["end"], DAY.isoformat())
        out = self.t.execute("propose_leftovers", {"keep": [k for k in keep if k["item"] != PADDLES],
                                                   "drop": [{"item": PADDLES}]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(f"{PADDLES} is {self.words()}: keep it today", out)

    def test_a_rested_item_can_still_be_proposed(self):
        from thirties_core.backoff import MissStreaks
        streaks = MissStreaks(self.sched.state_db)
        for back in (3, 2):
            streaks.record_day(DAY - timedelta(days=back), [{"title": TILES, "task_id": "t_tiles"}])
        out = self.say("plan tonight around the shift", "propose_day", {"items": [{"item": TILES, "start": "6:00 PM"}]})
        self.assertTrue(out.startswith(NOT_DONE), out)  # missed lately: it rests
        self.due(TILES, "t_tiles")
        out = self.say("plan tonight around the shift", "propose_day", {"items": [{"item": TILES, "start": "6:00 PM"}]})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_redraw_with_no_room_keeps_it(self):
        from thirties_core.astronomy import set_debug_time
        set_debug_time(at("21:00"))
        self.t.execute("add_event", {"title": "Load the canoe trailer", "start": "9:00 PM", "duration": "90 min"})
        self.t.execute("add_event", {"title": KILN, "start": "10:30 PM", "duration": "60 min"})
        self.due(KILN)
        out = self.say("redraw tonight", "redraw", {})
        self.assertIn(f"{KILN} doesn't fit before bedtime, but it's {self.words()}, so it stays on today's plan", out)
        self.assertTrue(self.planned(KILN))
        self.assertNotIn(KILN.lower(), OpenPool(self.sched.state_db).all())


class OverdueTest(HeldPaths, StudioBase):
    offset = -3  # due on Monday; today is Thursday

    def test_the_wording_names_the_day_it_was_due(self):
        self.assertEqual(self.words(), "overdue (it was due Monday)")
        self.assertEqual(stays_line(KILN, DAY - timedelta(days=3), DAY).split(". ")[0],
                         f"{KILN} is overdue (it was due Monday), so it stays on today")
        self.assertEqual(deadline_words(DAY - timedelta(days=12), DAY), "overdue (it was due Oct 3)")
        self.assertEqual(moved_line(KILN, TOMORROW, DAY, DAY - timedelta(days=3)),
                         f"{KILN} was overdue; it's on tomorrow now.")
        self.assertEqual(moved_line(KILN, TOMORROW, DAY, DAY), f"{KILN} was due today; it's on tomorrow now.")

    def test_done_since_it_was_due_holds_nothing(self):
        self.due(KILN, "t_kiln")
        self.assertEqual(held_due(self.t, KILN, "t_kiln"), DAY - timedelta(days=3))
        self.sched.state_db.record_task_completed("t_kiln", DAY - timedelta(days=1))
        self.assertIsNone(held_due(self.t, KILN, "t_kiln"))

    def test_done_on_todays_plan_holds_nothing(self):
        span = self.kiln_today()
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        self.assertIsNone(held_due(self.t, KILN, "t_kiln"))


class DueTodayTest(HeldPaths, StudioBase):
    offset = 0


class NotDueTest(StudioBase):
    """Due later, or with no due date: it moves on any word, as before."""

    def setUp(self):
        super().setUp()
        self.due(KILN, "t_kiln", offset=5)

    def test_held_only_on_or_before_today(self):
        self.assertIsNone(held_due(self.t, KILN, "t_kiln"))
        self.assertIsNone(held_due(self.t, TILES, "t_tiles"))

    def test_a_vague_word_moves_it(self):
        span = self.say("renew the kiln permit today", "add_to_today", {"items": [KILN]}) and self.planned(KILN)[0]
        out = self.say("the kiln permit can wait", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("overdue", out)
        self.assertNotIn("due today", out)
        self.assertEqual(self.planned(KILN), [])

    def test_if_theres_time_vague_moves_it(self):
        self.say("glaze the test tiles if there's time", "add_to_today", {"items": [TILES]})
        out = self.say("not today for the tiles", "if_there_is_time", {"item": TILES, "action": "tomorrow"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn(TILES, self.body("n_today"))

    def test_a_skip_takes_it_off_todays_list(self):
        self.say("glaze the test tiles today", "add_to_today", {"items": [TILES]})
        span = self.planned(TILES)[0]
        out = self.say("skip the tiles today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertNotIn("stays on today's list", out)
        self.assertNotIn(f"- [ ] {TILES}", self.body("n_today"))

    def test_a_redraw_with_no_room_takes_it_off(self):
        from thirties_core.astronomy import set_debug_time
        set_debug_time(at("21:00"))
        self.t.execute("add_event", {"title": "Load the canoe trailer", "start": "9:00 PM", "duration": "90 min"})
        self.t.execute("add_event", {"title": TILES, "start": "10:30 PM", "duration": "60 min"})
        out = self.say("redraw tonight", "redraw", {})
        self.assertIn("Doesn't fit before bedtime", out)
        self.assertEqual(self.planned(TILES), [])


class KeepADroppedLeftoverTest(StudioBase):
    """A dropped leftover kept for a vague later time: the due check comes before anything is written, so a
    refused keep leaves every note in Joplin as it was."""
    offset = -2

    def setUp(self):
        super().setUp()
        due = self.due(PADDLES, "t_paddles")
        self.pool = OpenPool(self.sched.state_db)
        self.pool.add([{"title": PADDLES, "task_id": "t_paddles", "horizon": "week", "when": "this week",
                        "due": due.isoformat(), "why": "the club lent spares", "pending_drop": True,
                        "proposed_on": DAY.isoformat()}], DAY - timedelta(days=1))
        self.say("archive the paddles for good", "leftover_action", {"item": PADDLES,
                                                                                 "action": "archive"})
        self.assertNotIn(PADDLES, self.body("n_studio"))  # filed away, so a keep would put it back

    def snapshot(self):
        return {k: v.get("body") for k, v in self.api.notes.items()}

    def test_refused_keep_writes_nothing(self):
        before, writes = self.snapshot(), len(self.api.updates)
        out = self.say("keep the paddles for later", "leftover_action", {"item": PADDLES, "action": "keep",
                                                                         "when": "this week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(f"{PADDLES} is {self.words()}, so it stays on today", out)
        self.assertEqual(len(self.api.updates), writes)
        self.assertEqual(self.snapshot(), before)

    def test_a_clear_keep_puts_it_back_and_says_so(self):
        out = self.say("keep the canoe paddles for next week", "leftover_action", {"item": PADDLES, "action": "keep",
                                                                                  "when": "next week"})
        self.assertIn(f"{PADDLES} was overdue; it's on ", out)
        self.assertIn(f"- [ ] {PADDLES}", self.body("n_studio"))


class OnSiteTest(InPersonBase):
    """An on-site day with no free time left outside work: a held item stays where it was, during work."""

    def setUp(self):
        super().setUp()
        p = patch("thirties_core.if_time._waking_end", side_effect=lambda plan: plan.day_start + timedelta(hours=11))
        p.start()
        self.addCleanup(p.stop)
        self.t.execute("add_event", {"title": "Wheel-throwing class", "start": "4:00 PM", "duration": "2 hours",
                                     "fixed": True})

    def run_with(self, offset):
        ListCatalog(self.sched.state_db).set_urgency("line:hull", "soon", due=DAY + timedelta(days=offset),
                                                     title="Wax the canoe hull")
        span = self.put("Wax the canoe hull", at("10:00"), 120)
        out = self.say("in person at the studio all shift", "work_on_site", {"on_site": True})
        self.assertEqual((span.start_dt, span.status), (at("10:00"), SpanStatus.PLANNED))
        self.assertEqual([s for s in self.t.plan_for(TOMORROW).spans if s.title == "Wax the canoe hull"], [])
        return out

    def test_overdue_stays(self):
        out = self.run_with(-4)
        self.assertIn("Wax the canoe hull: overdue (it was due Sunday) and no free time outside work is left, so "
                      "it stays at 10:00 AM, during work.", out)

    def test_due_today_stays(self):
        out = self.run_with(0)
        self.assertIn("Wax the canoe hull: due today and no free time outside work is left, so it stays at "
                      "10:00 AM, during work.", out)


if __name__ == "__main__":
    unittest.main()
