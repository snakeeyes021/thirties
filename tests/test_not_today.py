"""Not today (not_today.py): "skip" means "not today", never "tomorrow".

A skipped to-do leaves today's plan and list, isn't on the next day by itself, and comes back on a later day this
week that Python picks by fit; a day the user names wins. A skipped meal, routine step or appointment simply ends.
A move to tomorrow the user didn't ask for goes to a later day this week; only drop words stop an item coming back.

Fixtures are invented: someone who works a late shift at a pottery studio and goes canoeing. The fixed day is a
Thursday: two days on is Saturday, and the week ends on Sunday.
"""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import DROPPED, OpenPool, settle
from thirties_core.day_lists import AheadLists, onto_today
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import PlanTools

TOMORROW, SATURDAY, SUNDAY = (DAY + timedelta(days=n) for n in (1, 2, 3))
STUDIO = "- [ ] Trim the test mugs\n- [ ] Mix a celadon glaze\n"
TITLES = {"n_today": "Today To-Do", "n_studio": "Studio errands", "n_kiln": "Kiln check"}


def studio_item(tid, title, due=""):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=TITLES["n_studio"], parent_note_id="n_studio", due=due)


class NotTodayBase(unittest.TestCase):
    """The studio worker's lists: Today, a studio errands list and a kiln routine, in a fake Joplin."""

    def setUp(self):
        set_debug_time(at("09:45"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n_studio": {"id": "n_studio", "body": STUDIO},
                                  "n_kiln": {"id": "n_kiln", "body": "- [ ] Vent the kiln\n- [ ] Log the firing\n"}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES.get(nid, ""), "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_studio", "list"), ("n_kiln", "routine")):
            catalog.set_role(nid, role, TITLES[nid])
        self.services = Services.for_db(self.sched.state_db, catalog.apply(
            [studio_item("t_mugs", "Trim the test mugs"), studio_item("t_glaze", "Mix a celadon glaze")]), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)
        self.a = self.t.assistant

    def say(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def body(self, note):
        return self.api.notes[note]["body"]

    def planned(self, title, plan=None):
        plan = plan or self.t.day_plan
        return [s for s in plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def on_today_at(self, title, time):
        self.say("put it on today", "add_event", {"title": title, "start": time, "duration": "40 min",
                                                  **({"task_id": "t_mugs"} if title == "Trim the test mugs" else {})})
        span = self.planned(title)[0]
        if f"- [ ] {title}" not in self.body("n_today"):
            self.api.notes["n_today"]["body"] += f"- [ ] {title}\n"
        return span

    def morning(self, day, hhmm="07:50"):
        """That day's morning: the leftovers sorted, the day's list filled; the candidates for its plan."""
        set_debug_time(at(hhmm, (day - DAY).days))
        self.t.day_plan = self.t.plan_for(day)
        settle(self.a, day)
        onto_today(self.a, day)
        return [c["title"] for c in self.a.proposals.candidates(self.t.day_plan)]

    def ahead(self, day):
        return [r["title"] for r in AheadLists(self.sched.state_db).for_day(day)]

    def assert_home_untouched(self):
        self.assertEqual(self.body("n_studio"), STUDIO)
        self.assertNotIn("[x]", self.body("n_today"))


class SkippedToDoTest(NotTodayBase):
    def test_a_skipped_list_item_isnt_on_tomorrow_and_comes_back_later_this_week(self):
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        out = self.say("not getting to the mug trimming at all today", "log_outcome",
                       {"event": span.id, "status": "skipped"})
        self.assertIn("Saturday, later this week", out)
        self.assertNotIn("Trim the test mugs", self.body("n_today"))  # off today's list at once
        self.assertEqual(self.planned("Trim the test mugs"), [])  # and off today's plan
        self.assertEqual(self.ahead(TOMORROW), [])
        self.assertEqual(self.ahead(SATURDAY), ["Trim the test mugs"])
        self.assertNotIn("Trim the test mugs", self.morning(TOMORROW))  # never the very next day by itself
        self.assertNotIn("Trim the test mugs", self.body("n_today"))
        self.assertIn("Trim the test mugs", self.morning(SATURDAY))  # back on a later day this week
        self.assertIn("- [ ] Trim the test mugs", self.body("n_today"))
        self.assert_home_untouched()

    def test_an_item_only_on_today_waits_for_a_later_day_too(self):
        span = self.on_today_at("Wax the canoe paddles", "2:20 PM")
        out = self.say("the paddles will have to wait, skipping", "log_outcome",
                       {"event": span.id, "status": "skipped"})
        self.assertIn("Saturday", out)
        self.assertNotIn("Wax the canoe paddles", self.body("n_today"))
        self.assertEqual(OpenPool(self.sched.state_db).all()["wax the canoe paddles"]["start"], SATURDAY.isoformat())
        self.morning(TOMORROW)
        self.assertNotIn("Wax the canoe paddles", self.body("n_today"))
        self.morning(SATURDAY)
        self.assertIn("- [ ] Wax the canoe paddles", self.body("n_today"))

    def test_skipped_after_its_time_went_by_it_still_waits_for_a_later_day(self):
        span = self.on_today_at("Trim the test mugs", "10:20 AM")
        set_debug_time(at("16:40"))
        out = self.say("the mug trimming never happened", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("Saturday", out)
        self.assertEqual(self.ahead(TOMORROW), [])
        self.assertEqual(self.ahead(SATURDAY), ["Trim the test mugs"])

    def test_a_today_only_item_skipped_after_its_time_never_comes_back_today(self):
        span = self.on_today_at("Wax the canoe paddles", "9:45 AM")  # a Today-only line: urgency "now"
        set_debug_time(at("16:40"))
        out = self.say("count the paddle waxing as skipped", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertNotIn("tonight", out)
        self.assertEqual(self.planned("Wax the canoe paddles"), [])  # not back on today, at any hour
        self.assertNotIn("Wax the canoe paddles", self.body("n_today"))
        self.assertEqual(self.ahead(TOMORROW), [])
        self.assertEqual(self.ahead(SATURDAY), ["Wax the canoe paddles"])

    def test_the_apps_skip_button_never_puts_it_back_on_today_either(self):
        span = self.on_today_at("Wax the canoe paddles", "9:45 AM")
        set_debug_time(at("16:40"))
        self.t.by_hand = True
        try:
            self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})
        finally:
            self.t.by_hand = False
        self.assertEqual(self.planned("Wax the canoe paddles"), [])
        self.assertEqual(self.ahead(SATURDAY), ["Wax the canoe paddles"])

    def test_didnt_happen_without_a_skip_keeps_the_evening_rule(self):
        span = self.on_today_at("Wax the canoe paddles", "9:45 AM")
        set_debug_time(at("16:40"))
        out = self.say("the paddle waxing never got done", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("tonight", out)  # it can't wait and there's room: on tonight, as before
        self.assertEqual(len(self.planned("Wax the canoe paddles")), 1)

    def test_a_day_the_user_names_wins(self):
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        out = self.say("the mugs aren't happening now; tomorrow suits them better", "log_outcome",
                       {"event": span.id, "status": "skipped"})
        self.assertIn("tomorrow", out)
        self.assertEqual(self.ahead(TOMORROW), ["Trim the test mugs"])
        self.assertIn("Trim the test mugs", self.morning(TOMORROW))  # on tomorrow, offered a time there
        self.assertIn("- [ ] Trim the test mugs", self.body("n_today"))
        self.assert_home_untouched()

    def test_a_weekday_the_user_names_wins(self):
        span = self.on_today_at("Wax the canoe paddles", "2:20 PM")
        self.say("paddles: Sunday, not today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(self.ahead(SATURDAY), [])
        self.assertEqual(self.ahead(SUNDAY), ["Wax the canoe paddles"])


class SkippedEndsTest(NotTodayBase):
    def test_a_skipped_meal_simply_ends(self):
        span = self.on_today_at("Riverside brunch", "9:45 AM")
        self.say("we're giving the riverside brunch a miss", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertNotIn("Riverside brunch", self.body("n_today"))
        self.assertNotIn("riverside brunch", OpenPool(self.sched.state_db).all())  # it waits nowhere
        for day in (TOMORROW, SATURDAY, SUNDAY):
            self.assertEqual(self.ahead(day), [])
            self.assertNotIn("Riverside brunch", self.morning(day))

    def test_a_skipped_routine_step_simply_ends(self):
        self.say("kiln check at 2:20", "put_routine", {"routine": "Kiln check", "parts": [
            {"start": "2:20 PM", "items": ["Vent the kiln", "Log the firing"]}]})
        vent = self.planned("Vent the kiln")[0]
        self.say("venting: not today", "log_outcome", {"event": vent.id, "status": "skipped"})
        self.assertEqual(OpenPool(self.sched.state_db).all(), {})
        self.assertEqual(self.ahead(SATURDAY) + self.ahead(TOMORROW), [])
        self.assertEqual(self.body("n_kiln"), "- [ ] Vent the kiln\n- [ ] Log the firing\n")  # routine untouched


class TomorrowUnaskedTest(NotTodayBase):
    def test_a_move_to_tomorrow_with_no_day_said_goes_later_this_week(self):
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        out = self.say("can't face the mugs this afternoon, shift them off today", "update_event",
                       {"event": span.id, "to_day": "tomorrow"})
        self.assertIn("Saturday", out)
        self.assertEqual(self.planned("Trim the test mugs", self.t.plan_for(TOMORROW)), [])
        self.assertEqual(len(self.planned("Trim the test mugs", self.t.plan_for(SATURDAY))), 1)

    def test_a_move_to_a_named_day_goes_there(self):
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        self.say("mugs to Sunday please", "update_event", {"event": span.id, "to_day": "Sunday"})
        self.assertEqual(len(self.planned("Trim the test mugs", self.t.plan_for(SUNDAY))), 1)
        span = self.on_today_at("Wax the canoe paddles", "4:40 PM")
        self.say("paddles can go to tomorrow", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertEqual(len(self.planned("Wax the canoe paddles", self.t.plan_for(TOMORROW))), 1)

    def test_if_theres_time_tomorrow_with_no_day_said_goes_later_this_week(self):
        self.say("paddles if there's time", "add_to_today", {"items": ["Wax the canoe paddles"]})
        self.api.notes["n_today"]["body"] = "## If there's time\n- [ ] Wax the canoe paddles\n"
        out = self.say("not the paddles today", "if_there_is_time", {"item": "Wax the canoe paddles",
                                                                      "action": "tomorrow"})
        self.assertIn("Saturday", out)
        self.assertEqual(self.ahead(TOMORROW), [])
        self.assertEqual(self.ahead(SATURDAY), ["Wax the canoe paddles"])

    def test_due_today_keeps_its_protection(self):
        self.services.tasks = [t for t in self.services.tasks if t.id != "t_glaze"] + [
            studio_item("t_glaze", "Mix a celadon glaze", due=DAY.isoformat())]
        self.say("glaze at 2:20", "add_event", {"title": "Mix a celadon glaze", "start": "2:20 PM",
                                                "duration": "40 min", "task_id": "t_glaze"})
        span = self.planned("Mix a celadon glaze")[0]
        out = self.say("shift the glaze off today", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertIn("due today", out)
        self.assertEqual(len(self.planned("Mix a celadon glaze")), 1)  # it stays today


class DropTest(NotTodayBase):
    def test_drop_words_with_a_skip_stop_it_coming_back(self):
        span = self.on_today_at("Wax the canoe paddles", "2:20 PM")
        out = self.say("drop the paddle waxing for good, skip it", "log_outcome",
                       {"event": span.id, "status": "skipped"})
        self.assertIn("dropped", out)
        self.assertEqual(OpenPool(self.sched.state_db).all()["wax the canoe paddles"]["horizon"], DROPPED)
        for day in (TOMORROW, SATURDAY, SUNDAY):
            self.assertEqual(self.ahead(day), [])
            self.morning(day)
            self.assertNotIn("Wax the canoe paddles", self.body("n_today"))

    def test_dropped_after_it_was_set_aside_it_doesnt_come_back(self):
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        self.say("mugs: not today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(self.ahead(SATURDAY), ["Trim the test mugs"])
        self.say("actually drop the mugs, I'm not doing them", "place_leftover",
                 {"item": "Trim the test mugs", "horizon": "drop"})
        self.morning(SATURDAY)
        self.assertNotIn("Trim the test mugs", self.body("n_today"))
        self.assert_home_untouched()


class ConversationTest(NotTodayBase):
    """The whole turn with a scripted model (no real calls): the model skips, Python picks the day."""

    def test_a_skip_in_conversation_waits_for_a_later_day(self):
        from tests.test_conversation import reply
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        span = self.on_today_at("Trim the test mugs", "2:20 PM")
        engine = MockInferenceEngine()
        m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                joplin_engine=self.services.joplin, inference_engine=engine)
        engine.responses = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "log_outcome", "arguments": {"event": span.id, "status": "skipped"}}]},
            reply("Done, the mugs are off today."), reply("Done, the mugs are off today.")]
        m.send_user_message("the mug trimming won't fit around my shift today")
        results = [str(x.get("content")) for x in m.brain.messages if x.get("role") == "tool"]
        self.assertTrue(any("Saturday" in r for r in results), results)
        self.assertEqual(self.ahead(TOMORROW), [])
        self.assertEqual(self.ahead(SATURDAY), ["Trim the test mugs"])
        self.assertNotIn("Trim the test mugs", self.body("n_today"))


class DayWordsTest(unittest.TestCase):
    def test_named_days(self):
        from thirties_core.not_today import named_window
        pt = MagicMock(user_message=None)
        pt.today.return_value = DAY
        pt.scheduler.config.general.week_start = "mon"
        for said, want in (("tomorrow suits them better", (TOMORROW, TOMORROW)), ("paddles: Sunday", (SUNDAY, SUNDAY)),
                           ("over the weekend", (SATURDAY, SUNDAY)), ("day-after-tomorrow is fine", (SATURDAY, SATURDAY)),
                           ("push it back a day", (TOMORROW, TOMORROW)), ("I sat by the kiln", None),
                           ("half of it, 1/2", None),
                           ("not today", None), ("", None)):
            self.assertEqual(named_window(pt, said), want, said)


if __name__ == "__main__":
    unittest.main()
