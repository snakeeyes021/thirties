"""The day's list: the Today note, linked to the lists its items came from."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.today import TodayLinks

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
        out = self.t.execute("add_to_today", {"items": ["call the bank", "Clip cat nails", "Buy stamps for the trip"]})
        self.assertEqual(out, "Added to today's list: Call the bank, Clip cat nails, Buy stamps for the trip.")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n- [ ] Clip cat nails\n- [ ] Buy stamps for the trip\n")
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
        out = self.t.execute("add_task", {"title": "Torquio: check the machine id", "list": "errands",
                                          "start": "7:00 PM", "schedule_as": "Tell Claude re: machine id"})
        self.assertIn("Added 'Torquio: check the machine id' to Errands/Admin. Added Tell Claude re: machine id", out)
        self.assertIn("(also added to today's list)", out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Tell Claude re: machine id")
        self.assertEqual(span.duration_minutes, 30)
        self.assertTrue(any(t.id == span.task_id and t.parent_note_id == "n_errands" for t in self.services.tasks))
        self.assertIn("- [ ] Torquio: check the machine id", self.body("n_today"))
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        self.assertIn("- [x] Torquio: check the machine id", self.body("n_errands"))

    def test_only_the_same_wording_links_never_a_near_match(self):
        self.services.tasks.append(TaskItem(id="media:letterboxd:andre", source_notebook="Letterboxd",
                                            title="Watch: My Dinner with Andre (1981)"))
        self.services.tasks.append(item("t_vanity", "Near vanity boxes"))
        out = self.t.execute("add_to_today", {"items": ["Dinner"]})
        self.assertEqual(out, "Added to today's list: Dinner.")  # 10-22: it added the film
        self.assertIsNone(TodayLinks(self.sched.state_db).home_of("Dinner"))
        out = self.t.execute("log_outcome", {"event": "Kitty litter boxes", "status": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # 10-18: it ticked "Near vanity boxes" in Joplin
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
        self.api.notes["n_today"]["body"] = "- [x] Call the bank\n- [ ] Clip cat nails\n"
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [x] Shower\n- [ ] PT\n"}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")
        notes = self.services.joplin.fetch_active_notes()
        text = FrontierBrain(self.t, None, tasks=self.services.tasks, active_notes=notes).build_backlog_summary()
        self.assertIn("Today's list (Today To-Do):\n- [done] Call the bank\n- Clip cat nails\n"
                      "Routine (Morning Routine):\n- [done] Shower\n- PT", text)


class TestWhatsLeft(TodayTestCase):

    def test_the_slm_answers_from_the_plan_and_todays_list(self):
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.parse import Action
        self.t.execute("add_to_today", {"items": ["Call the bank", "Clip cat nails"]})
        self.t.execute("add_event", {"title": "Shower", "start": "9:00 AM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Call the bank", "start": "2:00 PM", "duration": "30 min",
                                     "task_id": "t_bank"})
        brain = SlmBrain(self.t)
        brain._message = "back. what's still left for today?"
        out, _ = brain._do_question(Action("question", {"topic": "what's still left for today"}))
        self.assertEqual(out, "Still planned today:\n- 2:00 PM – Call the bank\n"
                              "On today's list, no time yet: Clip cat nails.\n"
                              "Not marked yet:\n- Shower")  # at the bottom, by block, no times


class TestSlmShortcuts(TodayTestCase):

    def test_gemma_puts_things_on_todays_list_and_records_urgency(self):
        from thirties_core.brains.slm import SlmBrain
        brain = SlmBrain(self.t, tasks=self.services.tasks)
        self.assertEqual(brain.respond("add call the bank and clip cat nails to today's list"),
                         "Added to today's list: Call the bank, Clip cat nails.")
        self.assertTrue(brain.respond("buy stamps isn't urgent").startswith("Noted: 'Buy stamps' is whenever"))
        self.assertTrue(brain.respond("call the bank has to happen by friday").startswith(
            "Noted: 'Call the bank' is by"))
        self.assertIsNone(brain._list_shortcut("yeah put that on today"))  # answering the morning offer

    def test_gemma_lays_out_a_list_of_things_without_times(self):
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.parse import Action
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [ ] Feed kitties\n- [ ] Shower\n- [ ] PT\n"}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")
        self.t.execute("remove_event", {"event": "Work"})  # a day off
        brain = SlmBrain(self.t, tasks=self.services.tasks)
        out = brain._run_actions([Action("add", {"title": "morning routine", "duration": "30 minutes"}),
                                  Action("add", {"title": "Thirties dev", "duration": "1 hour"}),
                                  Action("add", {"title": "lunch"}), Action("add", {"title": "walk", "duration": "1 hour"})],
                                 "put down my morning routine, thirties dev, lunch and a walk, just figure it out")
        self.assertTrue(out.startswith("Here's the day:"), out)
        titles = {s.title: s for s in self.t.day_plan.spans}
        self.assertTrue({"Feed kitties", "Shower", "PT", "Thirties dev", "Lunch", "Walk"} <= set(titles))  # routine as items
        self.assertEqual(titles["Feed kitties"].start_dt.strftime("%H:%M"), "10:00")
        self.assertEqual(titles["PT"].start_dt.strftime("%H:%M"), "10:20")  # short things share their block, in order
        self.assertGreaterEqual(titles["Lunch"].start_dt.hour, 11)                       # lunch at lunchtime
        from datetime import timedelta
        self.assertGreaterEqual(titles["Walk"].start_dt - titles["Thirties dev"].end_dt, timedelta(minutes=30))  # room


class TestRoutinesOnToday(TodayTestCase):
    """10-26/27: "schedule my morning routine" wrote a made-up "Morning Routine steps" line into the routine note and
    put only that on Today; the plan and the Today note had drifted apart."""

    STEPS = ["Feed kitties", "Caffeinate", "Water bottle", "Breakfast (w/ vitamins)", "Throne", "Shower", "PT"]

    def setUp(self):
        super().setUp()
        self.routine = "".join(f"- [x] {s}\n" for s in self.STEPS)  # the user ticks it there; it's a template
        self.api.notes["n_routine"] = {"id": "n_routine", "body": self.routine}
        ListCatalog(self.sched.state_db).set_role("n_routine", "routine", "Morning Routine")

    def test_a_routine_goes_on_the_plan_as_its_steps_and_on_today_as_one_group(self):
        out = self.t.execute("put_routine", {"routine": "morning routine", "parts": [
            {"start": "10:00 AM", "items": ["Feed kitties", "Caffeinate", "Water bottle", "Breakfast (w/ vitamins)"]},
            {"start": "3:00 PM", "items": ["Throne", "Shower", "PT"]}]})
        self.assertTrue(out.startswith("Put Morning Routine on the plan: 10:00 AM: Feed kitties, Caffeinate, Water "
                                       "bottle, Breakfast (w/ vitamins); 3:00 PM: Throne, Shower, PT."), out)
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
        self.t.execute("add_event", {"title": "Torquio dev", "start": "8:00 PM", "duration": "1 hour",
                                     "day": "tomorrow"})
        self.assertEqual(self.body("n_today"), "- [ ] Dinner\n- [ ] Morning Routine\n    - [ ] Shower\n")

    def test_a_done_step_ticks_todays_copy_and_the_group_leaves_the_next_morning(self):
        self.t.execute("put_routine", {"routine": "Morning Routine", "start": "10:00 AM"})
        shower = next(s for s in self.t.day_plan.spans if s.title == "Shower")
        self.t.execute("log_outcome", {"event": shower.id, "status": "done"})
        self.assertIn("    - [x] Shower\n", self.body("n_today"))
        self.api.notes["n_today"]["body"] += "- [ ] Call the bank\n"
        self.assertEqual(self.t.assistant.today_carryover()["keep"], ["Call the bank"])  # routines never carry over
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "")
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n")


class TestMorning(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Call the bank", "Clip cat nails"]})
        self.api.notes["n_today"]["body"] = "- [x] Feed the cats\n" + self.body("n_today")
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [x] Shower\n- [x] PT\n"}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_routine", "routine", "Morning Routine")
        catalog.set_urgency("t_stamps", "now")
        self.services.tasks = catalog.apply(self.services.tasks)

    def test_routines_start_unticked_and_whats_done_leaves_today(self):
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [ ] Shower\n- [ ] PT\n")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n- [ ] Clip cat nails\n")
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
        self.assertEqual(self.body("n_routine"), "- [ ] Shower\n- [ ] PT\n")
        self.assertFalse(self.t.assistant.morning_pending())

    def test_without_write_back_the_rollover_still_applies(self):
        self.t.assistant.start_the_day()
        self.t.execute("rollover_proposal", {})
        self.sched.config.joplin.write_back = False
        out = self.t.execute("apply_rollover", {})
        self.assertIn("Couldn't put Buy stamps on today's list", out)
        self.assertEqual(self.t.execute("apply_rollover", {}), "Yesterday's items were already rolled over.")

    def test_daily_things_and_project_time_dont_carry_over(self):
        from datetime import timedelta
        from tests.fixtures import DAY
        y = DAY - timedelta(days=1)
        for title, start in (("Shower", "9:00 PM"), ("Thirties dev", "8:00 PM"), ("Return the library books", "7:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min", "day": y.isoformat()})
        self.api.notes["n_dev"] = {"id": "n_dev", "body": "- [ ] Fix the sync bug\n"}
        TITLES["n_dev"] = "Thirties"
        ListCatalog(self.sched.state_db).set_role("n_dev", "backlog", "Thirties")
        self.api.notes["n_routine"]["body"] = "- [ ] Shower\n- [ ] PT\n"
        titles = [p["title"] for p in self.t.assistant.rollover_plan()]
        self.assertEqual(titles, ["Return the library books"])

    def test_an_unanswered_morning_offer_expires(self):
        self.t.execute("rollover_proposal", {})
        self.assertTrue(self.t.assistant.rollover_pending())
        set_debug_time(at("15:00"))  # five hours later
        self.assertFalse(self.t.assistant.rollover_pending())

    def test_whats_recorded_done_is_never_offered_again(self):
        from datetime import timedelta
        from thirties_core.joplin_engine import item_id
        self.t.assistant.start_the_day()
        self.sched.state_db.record_task_completed(item_id("n_today", "Clip cat nails"), DAY - timedelta(days=1))
        self.api.up = False  # the tick never reached Joplin (Zein's text, 10-21 to 10-24)
        out = self.t.execute("rollover_proposal", {})
        self.assertNotIn("Clip cat nails", out)
        self.assertIn("Call the bank", out)
        y = DAY - timedelta(days=1)
        self.t.execute("add_event", {"title": "Read the text", "start": "7:00 PM", "duration": "30 min",
                                     "day": y.isoformat()})
        self.t.execute("add_event", {"title": "Read the text", "start": "8:00 PM", "duration": "30 min",
                                     "day": y.isoformat()})
        later = [s for s in self.t.plan_for(y).spans if s.title == "Read the text"][-1]
        out = self.t.execute("log_outcome", {"event": later.id, "status": "done", "day": y.isoformat()})
        self.assertNotIn(NOT_DONE, out)  # done as the later copy: the earlier one isn't still open
        self.assertNotIn("Read the text", [p["title"] for p in self.t.assistant.rollover_plan()])

    def test_the_morning_proposal_keeps_drops_and_adds(self):
        self.t.assistant.start_the_day()
        out = self.t.execute("rollover_proposal", {})
        self.assertEqual(out, "From yesterday, still open:\n- Call the bank\n- Clip cat nails\n"
                              "Also pressing: Buy stamps (now).\nWant today like that?")
        out = self.t.execute("apply_rollover", {"skip": ["bank", "nails"]})
        self.assertIn("Back to your lists: Call the bank.", out)
        self.assertIn("Added to today's list: Buy stamps.", out)
        self.assertIn("with no other list to go back to: Clip cat nails", out)
        self.assertEqual(self.body("n_today"), "- [ ] Buy stamps\n")
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))  # still on its home list


if __name__ == "__main__":
    unittest.main()
