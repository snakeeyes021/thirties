"""The day's list: the Today note, linked to the lists its items came from."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.today import TodayLinks
from thirties_core.assistant_tools import _checkbox_id

TITLES = {"n_today": "Today To-Do", "n_errands": "Errands/Admin", "n_routine": "Morning Routine"}


def item(tid, title, note="n_errands"):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=TITLES[note], parent_note_id=note)


class TodayTestCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n_errands": {"id": "n_errands", "body": "- [ ] Call the bank\n- [ ] Buy stamps\n"}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES[nid], "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_today", "today", "Today To-Do")
        catalog.set_role("n_errands", "list", "Errands/Admin")
        self.services = Services.for_db(self.sched.state_db, catalog.apply(
            [item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps")]), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)

    def body(self, note):
        return self.api.notes[note]["body"]


class TestAddingToToday(TodayTestCase):

    def test_list_items_go_on_linked_and_new_things_go_on_as_they_are(self):
        out = self.t.execute("add_to_today", {"items": ["call the bank", "Rinse the tank filter", "Buy stamps for the trip"]})
        self.assertEqual(out, "Added to today's list: Call the bank, Rinse the tank filter, Buy stamps for the trip.")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n- [ ] Rinse the tank filter\n- [ ] Buy stamps for the trip\n")
        links = TodayLinks(self.sched.state_db)
        self.assertEqual(links.home_of("Call the bank")["home_id"], "t_bank")
        self.assertIsNone(links.home_of("Buy stamps for the trip"))  # only similar to a list item: new
        self.assertEqual(self.t.execute("add_to_today", {"items": "Call the bank"}), "Already on it: Call the bank.")

    def test_scheduling_a_list_item_for_today_puts_it_on_todays_list(self):
        out = self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": "t_bank"})
        self.assertIn("(also added to today's list)", out)
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        out = self.t.execute("add_event", {"title": "Stamps", "start": "3:00 PM", "duration": "30 min",
                                           "task_id": "t_stamps", "day": "tomorrow"})
        self.assertNotIn("today's list", out)  # tomorrow's things aren't on today's list

    def test_adding_to_a_list_with_a_time_schedules_it_linked(self):
        # 10-24: add_task and add_event in one reply left the plan item unlinked, so ticking it did nothing in Joplin
        out = self.t.execute("add_task", {"title": "Garden app: check the build id", "list": "errands",
                                          "start": "7:00 PM", "schedule_as": "Ask the vendor about the build id"})
        self.assertIn("Added 'Garden app: check the build id' to Errands/Admin. Added Ask the vendor about the build id", out)
        self.assertIn("(also added to today's list)", out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Ask the vendor about the build id")
        self.assertEqual(span.duration_minutes, 30)
        self.assertTrue(any(t.id == span.task_id and t.parent_note_id == "n_errands" for t in self.services.tasks))
        self.assertIn("- [ ] Garden app: check the build id", self.body("n_today"))
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        self.assertIn("- [x] Garden app: check the build id", self.body("n_errands"))

    def test_only_the_same_wording_links_never_a_near_match(self):
        self.services.tasks.append(TaskItem(id="media:letterboxd:orchard", source_notebook="Letterboxd",
                                            title="Watch: Dinner at the Glass Orchard (1979)"))
        self.services.tasks.append(item("t_shelf", "Near shelf boxes"))
        out = self.t.execute("add_to_today", {"items": ["Dinner"]})
        self.assertEqual(out, "Added to today's list: Dinner.")  # 10-22: it added the film
        self.assertIsNone(TodayLinks(self.sched.state_db).home_of("Dinner"))
        out = self.t.execute("log_outcome", {"event": "Shelf storage boxes", "status": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # 10-18: it ticked a near match in Joplin
        self.assertIn("- [ ] Call the bank\n- [ ] Buy stamps", self.body("n_errands"))
        self.assertEqual(self.api.updates, [u for u in self.api.updates if u[0] == "n_today"])

    def test_by_task_id_too(self):
        self.assertEqual(self.t.execute("add_to_today", {"items": ["t_bank"]}), "Added to today's list: Call the bank.")
        self.assertEqual(TodayLinks(self.sched.state_db).home_of("Call the bank")["home_id"], "t_bank")

    def test_without_a_today_note(self):
        ListCatalog(self.sched.state_db).set_role("n_today", "list", "Today To-Do")
        self.assertTrue(self.t.execute("add_to_today", {"items": ["x"]}).startswith(NOT_DONE))


class TestTickingTwins(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Call the bank"]})

    def test_done_on_today_ticks_the_home_item(self):
        today_item = next(t for t in self.services.tasks if t.parent_note_id == "n_today")
        self.t.assistant._complete_backlog_task(today_item)
        self.assertIn("- [x] Call the bank", self.body("n_today"))
        self.assertIn("- [x] Call the bank", self.body("n_errands"))
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))

    def test_done_at_home_ticks_the_today_item(self):
        home = next(t for t in self.services.tasks if t.id == "t_bank")
        self.t.assistant._complete_backlog_task(home)
        self.assertIn("- [x] Call the bank", self.body("n_errands"))
        self.assertEqual(self.body("n_today"), "- [x] Call the bank\n")


class TestHandEdits(TodayTestCase):

    def test_ticked_by_hand_on_either_side(self):
        self.t.execute("add_to_today", {"items": ["Call the bank", "Buy stamps"]})
        self.api.notes["n_today"]["body"] = "- [x] Call the bank\n- [ ] Buy stamps\n"
        self.api.notes["n_errands"]["body"] = "- [ ] Call the bank\n- [x] Buy stamps\n"
        self.assertEqual(self.t.assistant.sync_today(), [])
        self.assertEqual(self.body("n_errands"), "- [x] Call the bank\n- [x] Buy stamps\n")
        self.assertEqual(self.body("n_today"), "- [x] Call the bank\n- [x] Buy stamps\n")

    def test_a_scheduled_item_taken_off_today_is_mentioned_once(self):
        self.t.execute("add_event", {"title": "Call the bank", "start": "3:00 PM", "duration": "30 min",
                                     "task_id": "t_bank"})
        self.api.notes["n_today"]["body"] = ""
        self.assertEqual(self.t.assistant.sync_today(),
                         ["You took Call the bank off today's list; it's still planned at 3:00 PM. "
                          "Should I take it off the plan too?"])
        self.assertEqual(self.t.assistant.sync_today(), [])


class TestBrains(TodayTestCase):

    def test_gemini_sees_todays_list_and_routines(self):
        from thirties_core.brains.frontier import FrontierBrain
        self.t.execute("add_to_today", {"items": ["Call the bank"]})
        self.api.notes["n_today"]["body"] = "- [x] Call the bank\n- [ ] Rinse the tank filter\n"
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [x] Shower\n- [ ] Journal\n"}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")
        notes = self.services.joplin.fetch_active_notes()
        text = FrontierBrain(self.t, None, tasks=self.services.tasks, active_notes=notes).build_backlog_summary()
        self.assertIn("Today's list (Today To-Do):\n- [done] Call the bank\n- Rinse the tank filter\n"
                      "Routine (Morning Routine):\n- [done] Shower\n- Journal", text)


class TestWhatsLeft(TodayTestCase):

    def test_the_slm_answers_from_the_plan_and_todays_list(self):
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.parse import Action
        self.t.execute("add_to_today", {"items": ["Call the bank", "Rinse the tank filter"]})
        self.t.execute("add_event", {"title": "Shower", "start": "9:00 AM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Call the bank", "start": "2:00 PM", "duration": "30 min",
                                     "task_id": "t_bank"})
        brain = SlmBrain(self.t)
        brain._message = "back. what's still left for today?"
        out, _ = brain._do_question(Action("question", {"topic": "what's still left for today"}))
        self.assertEqual(out, "Not marked yet:\n- Shower\n\n"  # first (C2), one per line, no times
                              "Still planned today:\n- 2:00 PM – Call the bank\n"
                              "On today's list, no time yet: Rinse the tank filter.")


class TestSlmShortcuts(TodayTestCase):

    def test_gemma_puts_things_on_todays_list_and_records_urgency(self):
        from thirties_core.brains.slm import SlmBrain
        brain = SlmBrain(self.t, tasks=self.services.tasks)
        self.assertEqual(brain.respond("add call the bank and rinse the tank filter to today's list"),
                         "Added to today's list: Call the bank, Rinse the tank filter.")
        self.assertTrue(brain.respond("buy stamps isn't urgent").startswith("Noted: 'Buy stamps' is whenever"))
        self.assertTrue(brain.respond("call the bank has to happen by friday").startswith(
            "Noted: 'Call the bank' is by"))
        self.assertIsNone(brain._list_shortcut("yeah put that on today"))  # answering the morning offer

    def test_gemma_lays_out_a_list_of_things_without_times(self):
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.parse import Action
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [ ] Feed the fish\n- [ ] Shower\n- [ ] Journal\n"}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")
        self.t.execute("remove_event", {"event": "Work"})  # a day off
        brain = SlmBrain(self.t, tasks=self.services.tasks)
        out = brain._run_actions([Action("add", {"title": "morning routine", "duration": "30 minutes"}),
                                  Action("add", {"title": "Recipe app dev", "duration": "1 hour"}),
                                  Action("add", {"title": "lunch"}), Action("add", {"title": "walk", "duration": "1 hour"})],
                                 "put down my morning routine, recipe app dev, lunch and a walk, just figure it out")
        self.assertTrue(out.startswith("Here's the day:"), out)
        titles = {s.title: s for s in self.t.day_plan.spans}
        self.assertTrue({"Feed the fish", "Shower", "Journal", "Recipe app dev", "Lunch", "Walk"} <= set(titles))  # routine as items
        self.assertEqual(titles["Feed the fish"].start_dt.strftime("%H:%M"), "10:00")
        self.assertEqual(titles["Journal"].start_dt.strftime("%H:%M"), "10:20")  # short things share their block, in order
        self.assertGreaterEqual(titles["Lunch"].start_dt.hour, 11)                       # lunch at lunchtime
        from datetime import timedelta
        self.assertGreaterEqual(titles["Walk"].start_dt - titles["Recipe app dev"].end_dt, timedelta(minutes=30))  # room


class TestRoutinesOnToday(TodayTestCase):
    """10-26/27: asked to put the routine on the plan, it wrote a made-up "Morning Routine steps" line into the routine note and
    put only that on Today; the plan and the Today note had drifted apart."""

    STEPS = ["Feed the fish", "Make coffee", "Pack lunch", "Breakfast (w/ juice)", "Make bed", "Shower", "Journal"]

    def setUp(self):
        super().setUp()
        self.routine = "".join(f"- [x] {s}\n" for s in self.STEPS)  # the user ticks it there; it's a template
        self.api.notes["n_routine"] = {"id": "n_routine", "body": self.routine}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "done")  # this morning's reset ran

    def test_a_routine_goes_on_the_plan_as_its_steps_and_on_today_as_one_group(self):
        out = self.t.execute("put_routine", {"routine": "morning routine", "parts": [
            {"start": "10:00 AM", "items": ["Feed the fish", "Make coffee", "Pack lunch", "Breakfast (w/ juice)"]},
            {"start": "3:00 PM", "items": ["Make bed", "Shower", "Journal"]}]})
        self.assertTrue(out.startswith("Put Morning Routine on the plan: 10:00 AM: Feed the fish, Make coffee, Pack "
                                       "lunch, Breakfast (w/ juice); 3:00 PM: Make bed, Shower, Journal."), out)
        steps = [s for s in self.t.day_plan.spans if s.title in self.STEPS]
        self.assertEqual(len(steps), 7)
        self.assertTrue(all(s.task_id for s in steps))
        shower = next(s for s in steps if s.title == "Shower")
        self.assertEqual((shower.start_dt, shower.end_dt), (at("15:10"), at("15:20")))
        self.assertEqual(self.body("n_today"), "- [ ] Morning Routine\n" + "".join(f"    - [ ] {s}\n" for s in self.STEPS))
        self.assertEqual(self.body("n_routine"), self.routine)  # the template is never written to
        again = self.t.execute("put_routine", {"routine": "Morning Routine", "start": "6:00 PM"})
        self.assertEqual(again, "Morning Routine is already on the plan.")

    def test_scheduling_a_routine_is_never_a_new_line_in_it(self):
        out = self.t.execute("add_task", {"title": "Morning Routine steps", "list": "Morning Routine",
                                          "start": "4:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE) and "put_routine" in out, out)
        self.assertEqual(self.body("n_routine"), self.routine)

    def test_everything_on_todays_plan_is_on_today(self):
        self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM", "duration": "1 hour"})
        self.t.execute("add_event", {"title": "Shower", "start": "3:00 PM", "duration": "30 min"})  # a routine step
        self.t.execute("add_event", {"title": "Garden app dev", "start": "8:00 PM", "duration": "1 hour",
                                     "day": "tomorrow"})
        self.assertEqual(self.body("n_today"), "- [ ] Dinner\n- [ ] Morning Routine\n    - [ ] Shower\n")

    def test_the_routine_note_is_never_written_however_a_step_is_ticked(self):
        self.api.notes["n_routine"]["body"] = self.routine.replace("[x]", "[ ]")  # this morning: unticked
        self.services.tasks += [TaskItem(id=_checkbox_id("n_routine", s), source_notebook="1. Tasks", title=s,
                                         is_checkbox_item=True, parent_note_title="Morning Routine",
                                         parent_note_id="n_routine", list_role="routine") for s in self.STEPS]
        template = self.body("n_routine")
        self.t.execute("put_routine", {"routine": "Morning Routine", "start": "10:00 AM"})
        coffee = next(s for s in self.t.day_plan.spans if s.title == "Make coffee")
        self.t.execute("log_outcome", {"event": coffee.id, "status": "done"})  # said in chat
        self.api.notes["n_today"]["body"] = self.body("n_today").replace("- [ ] Shower", "- [x] Shower")  # by hand
        self.t.assistant.sync_today()
        self.assertEqual(self.body("n_routine"), template)
        self.assertIn("    - [x] Make coffee\n", self.body("n_today"))

    def test_an_extra_step_done_is_ticked_on_today(self):
        # 10-30 sim: Brush teeth (not in the routine note) was done, but its Today line stayed open
        self.t.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "10:00 AM", "items": ["Shower", "Brush teeth"]}]})
        teeth = next(s for s in self.t.day_plan.spans if s.title == "Brush teeth")
        self.t.execute("log_outcome", {"event": teeth.id, "status": "done"})
        self.assertIn("    - [x] Brush teeth\n", self.body("n_today"))

    def test_on_a_dark_morning_the_coming_day_is_the_one_on_today(self):
        set_debug_time(at("06:40"))  # before sunrise: the calendar day is still yesterday's diurnal day
        self.assertFalse(self.t.today() == DAY)
        self.t.execute("add_event", {"title": "Lunch", "start": "12:00 PM", "duration": "30 min"})
        self.assertIn("- [ ] Lunch", self.body("n_today"))

    def test_planned_before_sunrise_the_group_survives_the_morning_reset(self):
        # 10-30 (sim): routine put on Today at 6:32 on a dark morning, then taken off at sunrise as "yesterday's"
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "")
        self.api.notes["n_today"]["body"] = "- [ ] Morning Routine\n    - [ ] Shower\n- [x] Old done thing\n"  # yesterday
        set_debug_time(at("06:30"))
        self.t.execute("put_routine", {"routine": "Morning Routine", "start": "6:30 AM", "parts": [
            {"start": "6:30 AM", "items": ["Feed the fish", "Brush teeth"]}]})
        set_debug_time(at("08:00"))
        self.t.assistant.start_the_day()  # the sunrise duty: already done for today, so nothing more
        self.assertEqual(self.body("n_today"), "- [ ] Morning Routine\n    - [ ] Feed the fish\n    - [ ] Brush teeth\n")
        self.assertNotIn("Brush teeth", self.body("n_routine"))  # an extra step is for today, not the template

    def test_a_done_step_ticks_todays_copy_and_the_group_leaves_the_next_morning(self):
        self.t.execute("put_routine", {"routine": "Morning Routine", "start": "10:00 AM"})
        shower = next(s for s in self.t.day_plan.spans if s.title == "Shower")
        self.t.execute("log_outcome", {"event": shower.id, "status": "done"})
        self.assertIn("    - [x] Shower\n", self.body("n_today"))
        self.api.notes["n_today"]["body"] += "- [ ] Call the bank\n"
        self.assertEqual(self.t.assistant.today_carryover()["keep"], ["Call the bank"])  # routines never carry over
        self.api.notes["n_today"]["body"] += "- [ ] Wind the clock\n"  # done on the plan, its line never ticked (10-25)
        self.t.execute("add_event", {"title": "Wind the clock", "start": "8:00 AM", "duration": "30 min"})
        self.sched.state_db.upsert_span(next(s for s in self.t.day_plan.spans if s.title == "Wind the clock"))
        span = next(s for s in self.t.day_plan.spans if s.title == "Wind the clock")
        span.status = SpanStatus.DONE
        self.sched.state_db.upsert_span(span)
        self.assertEqual(self.t.assistant.today_carryover()["keep"], ["Call the bank"])
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "")
        self.t.assistant.start_the_day()
        # routines never carry over; the bank went on after the day began, so it's today's (carry.py)
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n")


from datetime import timedelta


class TestMorning(TodayTestCase):
    """Yesterday's unmarked items (carry.py; 11-05): unmarked means unknown; some things never roll; a
    one-off to-do is asked about once, in one question; not done, it waits on its list for a day it fits; the
    Today note holds today's plan only."""

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Call the bank", "Rinse the tank filter"]})
        self.api.notes["n_today"]["body"] = "- [x] Feed the fish\n" + self.body("n_today")
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [x] Shower\n- [x] Journal\n"}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_routine", "routine", "Morning Routine")
        catalog.set_urgency("t_stamps", "now")
        self.services.tasks = catalog.apply(self.services.tasks)
        with self.sched.state_db._get_connection() as conn:  # they went on the list on an earlier day
            conn.execute("UPDATE today_links SET updated_at = '2026-01-01T12:00:00+00:00'")
        from tests.fixtures import DAY
        self.sched.state_db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")

    def yesterday(self, title, start, **extra):
        from datetime import timedelta
        from tests.fixtures import DAY
        args = {"title": title, "start": start, "duration": "30 min", "day": (DAY - timedelta(days=1)).isoformat()}
        self.t.execute("add_event", {**args, **extra})

    def test_the_morning_reset_is_meant_for_this_day_only(self):
        """If Joplin can't take the morning's changes now, they never run on a later day."""
        from datetime import datetime
        from thirties_core.joplin_writeback import WriteLog
        self.t.assistant.start_the_day()
        reset = [w["op"] for w in WriteLog(self.sched.state_db).since()
                 if w["op"]["op"] in ("untick_all", "clear_ticked", "remove")]
        self.assertEqual({o["op"] for o in reset}, {"untick_all", "clear_ticked", "remove"})
        for o in reset:
            self.assertEqual(datetime.fromisoformat(o["valid_until"]), self.t.day_plan.day_end)

    def test_routines_start_unticked_and_leftovers_leave_today(self):
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [ ] Shower\n- [ ] Journal\n")
        self.assertEqual(self.body("n_today"), "- [ ] Rinse the tank filter\n")  # it has no other list: it stays
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))  # waits on its own list
        self.sched.config.joplin.reset_routines = False
        self.api.notes["n_routine"]["body"] = "- [x] Shower\n"
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [x] Shower\n")  # not allowed: left alone

    def test_the_morning_reset_waits_for_joplin_instead_of_queuing(self):
        self.api.up = False
        self.t.assistant.start_the_day()
        self.assertTrue(self.t.assistant.morning_pending())
        self.assertEqual(self.sched.state_db.outbox_pending(), [])  # never queued: it would undo later ticks
        self.api.up = True
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [ ] Shower\n- [ ] Journal\n")
        self.assertFalse(self.t.assistant.morning_pending())

    def test_unmarked_items_are_asked_about_once_in_one_question(self):
        from tests.fixtures import DAY
        self.yesterday("Buy stamps", "7:00 PM")  # planned, on a list, no outcome
        self.t.assistant.start_the_day()
        out = self.t.execute("rollover_proposal", {})
        # 11-06: in effect by itself; today's keep is on today's schedule already, and the morning says so
        self.assertEqual(out, "Kept 3 from before (1 on today's plan): see Before today.")
        rows = {r["title"]: r for r in self.t.assistant.before.rows()["keep"]}
        self.assertIsNotNone(rows["Buy stamps"]["at"])  # placed
        self.assertEqual(rows["Call the bank"]["when"], "this week")
        self.assertIn("Buy stamps", [s.title for s in self.t.day_plan.spans])
        self.assertIn("The rest wait where proposed.", self.t.execute("apply_rollover", {}))
        # the next morning, still unmarked: never asked again
        self.sched.state_db.set_sync_meta(f"leftovers:{DAY.isoformat()}", "")
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "")
        self.assertTrue(self.t.execute("rollover_proposal", {}).startswith("Nothing from yesterday"))

    def test_not_done_waits_in_the_pool_and_is_offered_only_as_an_option(self):
        from thirties_core.carry import OpenPool
        from tests.fixtures import DAY
        self.t.assistant.start_the_day()
        self.assertIn("call the bank", OpenPool(self.sched.state_db).all(DAY))
        cands = {c["title"]: c["why"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertTrue(cands["Call the bank"].startswith("left open since "), cands)
        self.assertFalse(cands["Buy stamps"].startswith("left open"))  # urgent things still come first
        self.assertNotIn("Call the bank", [s.title for s in self.t.day_plan.spans])

    def test_a_left_open_item_waits_at_the_horizon_that_fits_it(self):
        # 11-05: a missed item is planned again at a horizon (tomorrow, this week, later), as a person would
        from datetime import timedelta
        from thirties_core.carry import choose_horizon
        from thirties_core.models import TaskItem
        from tests.fixtures import DAY
        a = self.t.assistant

        def task(title, **kw):
            return TaskItem(id="t_" + title[:4], source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                            parent_note_title="Errands/Admin", parent_note_id="n_errands", **kw)
        self.assertEqual(choose_horizon(a, task("Renew passport", urgency="now"), "Renew passport", DAY), "today")
        self.assertEqual(choose_horizon(a, task("Pay the bill", due=(DAY + timedelta(days=1)).isoformat()),
                                        "Pay the bill", DAY), "today")  # due very soon
        self.assertEqual(choose_horizon(a, task("Text Kim"), "Text Kim", DAY, said_minutes=5), "today")  # quick
        self.assertEqual(choose_horizon(a, task("File taxes", due=(DAY + timedelta(days=20)).isoformat()),
                                        "File taxes", DAY), "deadline")
        self.assertEqual(choose_horizon(a, task("Learn the harp", someday=True), "Learn the harp", DAY), "someday")
        self.assertEqual(choose_horizon(a, task("Clean the garage"), "Clean the garage", DAY), "week")  # unsure
        self.assertEqual(choose_horizon(a, None, "Garden podcast", DAY), "week")

    def test_the_day_draws_from_the_pool_by_horizon(self):
        from thirties_core.carry import OpenPool
        from tests.fixtures import DAY
        self.t.assistant.start_the_day()
        out = self.t.execute("rollover_proposal", {})
        self.assertEqual(out, "Kept 2 from before (0 on today's plan): see Before today.")  # a notice
        cands = {c["title"]: c["why"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertTrue(cands["Call the bank"].endswith("; this week"), cands)
        self.assertIn("someday", self.t.execute("place_leftover", {"item": "Call the bank", "horizon": "someday"}))
        cands = {c["title"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertNotIn("Call the bank", cands)  # someday: not in daily plans unless asked
        self.t.execute("place_leftover", {"item": "call the bank", "horizon": "today"})
        self.assertIn("Call the bank", [s.title for s in self.t.day_plan.spans])  # today: on the schedule at once
        OpenPool(self.sched.state_db).add([{"title": "Call the bank", "horizon": "week"}], DAY)
        self.assertEqual(OpenPool(self.sched.state_db).all()["call the bank"]["horizon"], "today")  # the user's word

    def test_today_holds_for_the_day_after_then_its_this_weeks(self):
        from datetime import timedelta
        from thirties_core.carry import OpenPool
        from tests.fixtures import DAY
        pool = OpenPool(self.sched.state_db)
        pool.add([{"title": "Call the bank", "task_id": "t_bank", "horizon": "today"}], DAY - timedelta(days=3))
        why = {c["title"]: c["why"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertNotIn("meant for today", why["Call the bank"])  # its urgency still ranks it

    def test_the_model_revises_the_proposal_and_a_yes_drops_what_it_dropped(self):
        # 11-05: the model proposes (keep at a natural horizon, or drop), the user confirms
        from thirties_core.carry import OpenPool
        from tests.fixtures import DAY
        self.yesterday("Watch the comet", "9:00 PM")
        self.t.assistant.start_the_day()
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("propose_leftovers", {
            "keep": [{"item": "Call the bank", "when": "tomorrow", "why": "the branch is closed today"},
                     {"item": "rinse the tank filter", "when": "this weekend"}],
            "drop": [{"item": "Watch the comet", "why": "it was last night"}]})
        self.assertIn("Keep:\nCall the bank – tomorrow\nRinse the tank filter – this weekend\nDrop:\nWatch the comet", out)
        pool = OpenPool(self.sched.state_db).all()
        self.assertEqual(pool["call the bank"]["start"], (DAY + timedelta(days=1)).isoformat())  # waits there now
        self.assertTrue(pool["watch the comet"]["pending_drop"])  # dropped only on a yes
        cands = {c["title"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)}
        self.assertNotIn("Call the bank", cands)  # tomorrow: not offered today
        self.assertIn("Dropped: Watch the comet.", self.t.execute("apply_rollover", {}))
        self.assertEqual(OpenPool(self.sched.state_db).all()["watch the comet"]["horizon"], "dropped")
        with self.assertRaises(Exception):
            self.t.assistant.propose_leftovers(keep=[{"item": "Call the bank", "when": "this week"}])  # all of them

    def test_place_leftover_takes_natural_words(self):
        from thirties_core.carry import OpenPool
        from tests.fixtures import DAY
        self.t.assistant.start_the_day()
        self.t.execute("rollover_proposal", {})
        self.assertIn("next month", self.t.execute("place_leftover", {"item": "Call the bank", "horizon": "next month"}))
        rec = OpenPool(self.sched.state_db).all()["call the bank"]
        self.assertEqual(rec["start"][:7], (DAY.replace(day=28) + timedelta(days=7)).isoformat()[:7])
        self.assertIn("Dropped", self.t.execute("place_leftover", {"item": "Rinse the tank filter", "horizon": "drop"}))

    def test_things_that_never_roll_simply_end(self):
        self.yesterday("Lunch", "12:00 PM")                     # a meal
        self.yesterday("Dentist", "3:00 PM", fixed=True)         # fixed to its time
        self.yesterday("Walk around the block", "8:00 PM")       # an ongoing activity, not a to-do
        self.yesterday("Recipe app dev", "9:00 PM")                # time on a project
        self.api.notes["n_today"]["body"] = ""
        self.t.assistant.start_the_day()
        from thirties_core.carry import settle
        from tests.fixtures import DAY
        out = settle(self.t.assistant, DAY)
        self.assertEqual(out["ask"], [])
        for title in ("Lunch", "Dentist", "Walk around the block", "Recipe app dev"):
            self.assertIn(title, out["ended"])
        self.assertTrue(self.t.execute("rollover_proposal", {}).startswith("Nothing from yesterday"))

    def test_put_on_today_today_isnt_a_leftover(self):
        # the morning reset can run late (Joplin was closed at wake-up): what went on Today today stays
        with self.sched.state_db._get_connection() as conn:
            conn.execute("UPDATE today_links SET updated_at = '2099-01-01T12:00:00+00:00'")
        self.t.assistant.start_the_day()
        self.assertIn("Call the bank", self.body("n_today"))

    def test_daily_things_and_project_time_dont_carry_over(self):
        from datetime import timedelta
        from tests.fixtures import DAY
        y = DAY - timedelta(days=1)
        for title, start in (("Shower", "9:00 PM"), ("Recipe app dev", "8:00 PM"), ("Return the library books", "7:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min", "day": y.isoformat()})
        self.api.notes["n_dev"] = {"id": "n_dev", "body": "- [ ] Fix the sync bug\n"}
        TITLES["n_dev"] = "Thirties"
        ListCatalog(self.sched.state_db).set_role("n_dev", "backlog", "Thirties")
        self.api.notes["n_routine"]["body"] = "- [ ] Shower\n- [ ] Journal\n"
        titles = [p["title"] for p in self.t.assistant.rollover_plan()]
        self.assertEqual(titles, ["Return the library books"])

    def test_an_unanswered_morning_offer_expires(self):
        self.t.execute("rollover_proposal", {})
        self.assertTrue(self.t.assistant.rollover_pending())
        set_debug_time(at("15:00"))  # five hours later
        self.assertFalse(self.t.assistant.rollover_pending())

    def test_put_on_today_before_a_late_reset_isnt_a_leftover(self):
        # Joplin was closed at wake-up, so the reset runs later; something added today in between stays
        from tests.fixtures import DAY
        self.t.execute("add_to_today", {"items": ["Return the parcel"]})
        self.t.assistant.start_the_day()
        self.assertIn("Return the parcel", self.body("n_today"))
        from thirties_core.carry import settle
        self.assertNotIn("Return the parcel", settle(self.t.assistant, DAY)["ask"])

    def test_whats_recorded_done_is_never_asked_about(self):
        from datetime import timedelta
        from thirties_core.joplin_engine import item_id
        from tests.fixtures import DAY
        self.sched.state_db.record_task_completed(item_id("n_today", "Rinse the tank filter"), DAY - timedelta(days=1))
        self.api.up = False  # the tick never reached Joplin (10-21 to 10-24)
        self.t.assistant.start_the_day()
        self.t.execute("rollover_proposal", {})
        keep = [r["title"] for r in self.t.assistant.before.rows()["keep"]]
        self.assertNotIn("Rinse the tank filter", keep)
        self.assertIn("Call the bank", keep)

    def test_a_group_is_asked_about_by_its_name(self):
        self.api.notes["n_today"]["body"] += "- [ ] Garden app\n    - [ ] Check the build\n    - [ ] Rerun the tests\n"
        self.t.assistant.start_the_day()
        self.t.execute("rollover_proposal", {})
        keep = [r["title"] for r in self.t.assistant.before.rows()["keep"]]
        self.assertIn("Garden app", keep)
        self.assertNotIn("Rerun the tests", keep)


