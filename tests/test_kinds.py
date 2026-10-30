"""Kinds of items (once / ongoing / daily), things going moot, and the morning rollover that follows from them
(0.2 items 7, 9 and 10; frictions R4 and R5)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.kinds import DAILY, ONCE, ONGOING, is_meal, kind_by_wording, kind_of, moot_after
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem
from thirties_core.stats import outcome_stats, session_lines
from thirties_core.today import TodayLinks

Y = (DAY - timedelta(days=1)).isoformat()


class TestReadingTheKind(unittest.TestCase):

    def test_the_obvious_cases_by_wording(self):
        for title in ("Breakfast", "Lunch", "Dinner", "Cats' dinner", "Feed kitties", "Brush teeth", "Shower",
                      "Vitamins", "Caffeinate"):
            self.assertEqual(kind_by_wording(title), DAILY, title)
        for title in ("Torquio dev", "Thirties session", "Work on the album", "Redfox work"):
            self.assertEqual(kind_by_wording(title), ONGOING, title)
        for title in ("Email back GitHub", "Clean broken glass", "Dinner with Sam", "Work", "Sleep"):
            self.assertIsNone(kind_by_wording(title), title)

    def test_the_list_role_wins_over_wording_and_the_users_word_wins_over_both(self):
        ticket = TaskItem(id="t", source_notebook="Dev", title="Fix the sync bug", list_role="backlog")
        chore = TaskItem(id="c", source_notebook="Home", title="Clean the shower", list_role="list")
        step = TaskItem(id="s", source_notebook="Home", title="PT", list_role="routine")
        self.assertEqual(kind_of(ticket.title, ticket), ONGOING)
        self.assertEqual(kind_of(chore.title, chore), ONCE)  # "shower" in a list item doesn't make it daily
        self.assertEqual(kind_of(step.title, step), DAILY)
        self.assertEqual(kind_of("PT", routine_steps={"pt"}), DAILY)
        self.assertEqual(kind_of("Learn Rust", chore, stored="ongoing"), ONGOING)
        self.assertEqual(kind_of("Something new"), ONCE)

    def test_meals_are_the_users_own(self):
        self.assertTrue(is_meal("Lunch"))
        self.assertFalse(is_meal("Lunch with Dana"))
        self.assertFalse(is_meal("Cats' dinner"))


class TestMoot(unittest.TestCase):

    def span(self, title, start, end, day=0):
        return EventSpan(title.lower(), title, at(start, day), at(end, day), SpanCategory.ACTIVITY)

    def test_a_meal_goes_moot_at_the_next_meal(self):
        # 10-23 21:31: "I skipped lunch. But I had dinner so it is moot"
        lunch, dinner = self.span("Lunch", "12:00", "12:30"), self.span("Dinner", "18:00", "19:00")
        end = at("07:00", 1)
        self.assertEqual(moot_after(lunch, DAILY, end, [lunch, dinner]), at("18:00"))
        self.assertEqual(moot_after(dinner, DAILY, end, [lunch, dinner]), end)  # the last one: at the day's end

    def test_daily_and_ongoing_go_moot_at_the_end_of_the_day_and_a_fixed_item_after_its_time(self):
        end = at("07:00", 1)
        self.assertEqual(moot_after(self.span("Feed kitties", "09:00", "09:30"), DAILY, end), end)
        self.assertEqual(moot_after(self.span("Torquio dev", "13:00", "15:00"), ONGOING, end), end)
        self.assertIsNone(moot_after(self.span("Clean broken glass", "13:00", "13:30"), ONCE, end))
        dentist = self.span("Dentist", "15:00", "16:00")
        self.assertEqual(moot_after(dentist, ONCE, end, fixed=True), at("18:00"))


class KindsTestCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_dev"] = {"id": "n_dev", "body": "- [ ] Fix the sync bug\n"}
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [ ] Feed kitties\n- [ ] Shower\n- [ ] PT\n"}
        TITLES.update({"n_dev": "Torquio", "n_routine": "Morning Routine"})
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_dev", "backlog", "Torquio")
        catalog.set_role("n_routine", "routine", "Morning Routine")
        self.services.tasks = catalog.apply(self.services.tasks + [item("t_bug", "Fix the sync bug", "n_dev")])

    def add(self, title, start, duration="30 min", day=None):
        args = {"title": title, "start": start, "duration": duration}
        if day:
            args["day"] = day
        out = self.t.execute("add_event", args)
        self.assertTrue(out.startswith("Added") or out.startswith("["), out)
        plan = self.t.plan_for(day) if day else self.t.day_plan
        return next(s for s in reversed(plan.spans) if s.title == title)


class TestDoneFollowsTheKind(KindsTestCase):

    def test_done_on_ongoing_project_time_is_a_session_and_the_item_stays_open(self):
        self.t.execute("add_event", {"title": "Torquio", "start": "1:00 PM", "duration": "1 hour", "task_id": "t_bug"})
        span = next(s for s in self.t.day_plan.spans if s.title == "Torquio")
        self.assertIn("- [ ] Fix the sync bug", self.body("n_today"))
        out = self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        self.assertIn("worked on it: Fix the sync bug stays open", out)
        self.assertIn("- [ ] Fix the sync bug", self.body("n_dev"))  # never ticked: it goes on
        self.assertIn("- [x] Fix the sync bug", self.body("n_today"))  # today's session is done
        self.assertEqual(span.status, SpanStatus.DONE)

    def test_finished_for_good_ticks_it(self):
        self.t.execute("add_event", {"title": "Torquio", "start": "1:00 PM", "duration": "1 hour", "task_id": "t_bug"})
        span = next(s for s in self.t.day_plan.spans if s.title == "Torquio")
        self.t.execute("log_outcome", {"event": span.id, "status": "done", "finished_for_good": "true"})
        self.assertIn("- [x] Fix the sync bug", self.body("n_dev"))

    def test_a_hand_tick_on_today_doesnt_close_an_ongoing_item(self):
        self.t.execute("add_to_today", {"items": ["Fix the sync bug", "Call the bank"]})
        self.api.notes["n_today"]["body"] = "- [x] Fix the sync bug\n- [x] Call the bank\n"
        self.t.assistant.sync_today()
        self.assertIn("- [ ] Fix the sync bug", self.body("n_dev"))
        self.assertIn("- [x] Call the bank", self.body("n_errands"))

    def test_an_ongoing_list_item_done_outside_the_plan_is_logged_as_a_session(self):
        out = self.t.execute("log_outcome", {"event": "Fix the sync bug", "status": "done"})
        self.assertIn("stays open", out)
        self.assertIn("logged it", out)
        self.assertIn("- [ ] Fix the sync bug", self.body("n_dev"))

    def test_a_skipped_daily_item_isnt_put_off(self):
        kitties = self.add("Feed kitties", "11:00 AM")
        kitties.task_id = "t_kitties"
        out = self.t.execute("log_outcome", {"event": kitties.id, "status": "skipped"})
        self.assertNotIn("put off", out)
        bank = self.add("Call the bank", "12:00 PM")
        out = self.t.execute("log_outcome", {"event": bank.id, "status": "skipped"})
        self.assertIn("put off 1 times", out)

    def test_kind_said_by_the_user_is_kept_and_wins(self):
        self.assertEqual(self.t.assistant.kind_of("Call the bank"), ONCE)
        out = self.t.execute("set_item_facts", {"event": "Call the bank", "kind": "daily"})
        self.assertIn("daily", out)
        self.assertEqual(self.t.assistant.kind_of("Call the bank"), DAILY)
        self.assertEqual(self.t.assistant.kind_of("Torquio dev"), ONGOING)
        self.assertEqual(self.t.assistant.kind_of("Feed kitties"), DAILY)  # a routine step

    def test_statistics_tell_ongoing_time_as_sessions(self):
        spans = [EventSpan(f"s{i}", "Torquio dev", at("13:00", -i), at("15:00", -i), SpanCategory.ACTIVITY,
                           status=SpanStatus.DONE) for i in range(1, 4)]
        self.assertEqual(session_lines(spans, lambda s: True), ["'torquio dev' (ongoing): 3 sessions, about 6h in all."])
        unknown = EventSpan("u", "Lunch", at("12:00", -1), at("12:30", -1), SpanCategory.ACTIVITY,
                            status=SpanStatus.UNKNOWN)
        stats = outcome_stats(spans + [unknown], at("10:00"))
        self.assertEqual((stats.reviewed, stats.unreviewed), (3, 0))  # unknown is neither reviewed nor waiting


class TestGoingMoot(KindsTestCase):

    def test_lunch_is_moot_once_dinner_begins_and_is_stored_as_unknown(self):
        lunch = self.add("Lunch", "12:00 PM")
        self.add("Dinner", "6:00 PM", "1 hour")
        self.assertFalse(self.t.assistant.is_moot(lunch, at("17:59")))
        self.assertTrue(self.t.assistant.is_moot(lunch, at("18:00")))
        changed = self.t.assistant.settle_moot(now=at("18:05"))
        self.assertEqual([s.title for s in changed], ["Lunch"])
        stored = next(s for s in self.sched.build_day_plan(DAY)[0].spans if s.id == lunch.id)
        self.assertEqual(stored.status, SpanStatus.UNKNOWN)
        self.assertNotIn("Lunch", self.t.execute("review_day", {}))  # never asked about again

    def test_a_moot_item_can_still_be_reported(self):
        lunch = self.add("Lunch", "12:00 PM")
        self.add("Dinner", "6:00 PM", "1 hour")
        self.t.assistant.settle_moot(now=at("18:05"))
        out = self.t.execute("log_outcome", {"event": lunch.id, "status": "done", "started": "1:00 PM",
                                             "finished": "1:30 PM"})
        self.assertIn("Marked Lunch done", out)
        self.assertTrue(self.t.execute("log_outcome", {"event": lunch.id, "status": "unknown"}).startswith("NOT_DONE"))

    def test_the_background_service_settles_moot_items_before_asking(self):
        set_debug_time(at("09:00"))
        self.sched.config.assistant.message_level = "medium"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        sent = []
        svc = BackgroundService(self.sched.config, lambda t, b, k: sent.append(b), scheduler=self.sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: DAY
        self.sched.state_db.upsert_span(EventSpan("l", "Lunch", at("12:00"), at("12:30"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("d", "Dinner", at("18:00"), at("19:00"), SpanCategory.ACTIVITY))
        set_debug_time(at("22:20"))
        svc.moot(at("22:20"))
        svc.wrap_up(at("22:20"))
        self.assertEqual(sent, ["🌙 Before bed: did Dinner happen? Just say which (or none)."])  # not lunch (10-23)


class TestTheMorningRollover(KindsTestCase):
    """10-18 to 10-30 (R4): only what should roll over is offered, and the question is plain."""

    def test_only_one_off_things_roll_over(self):
        for title, start in (("Breakfast", "9:00 AM"), ("Feed kitties", "9:30 AM"), ("Torquio dev", "1:00 PM"),
                             ("Clean broken glass", "7:00 PM"), ("Cats' dinner", "5:00 PM"), ("PT", "10:00 AM")):
            self.add(title, start, day=Y)
        dentist = self.add("Dentist appointment", "3:00 PM", day=Y)
        self.assertTrue(self.t._is_fixed(dentist))
        done = self.add("Email back GitHub", "8:00 PM", day=Y)
        self.t.execute("log_outcome", {"event": done.id, "status": "done", "day": Y})
        titles = [p["title"] for p in self.t.assistant.rollover_plan()]
        self.assertEqual(titles, ["Clean broken glass"])
        yesterday = self.sched.build_day_plan(DAY - timedelta(days=1))[0]
        unknown = sorted(s.title for s in yesterday.spans if s.status == SpanStatus.UNKNOWN)
        self.assertEqual(unknown, ["Breakfast", "Cats' dinner", "Dentist appointment", "Feed kitties", "PT",
                                   "Torquio dev"])  # moot, stored as unknown: not skipped, not put off

    def test_it_keeps_its_time_when_that_is_free(self):
        self.add("Clean broken glass", "9:00 PM", day=Y)
        plan = self.t.assistant.rollover_plan()
        self.assertEqual(plan[0]["start"], at("21:00").isoformat())

    def test_today_lines_for_daily_things_and_project_time_arent_kept(self):
        self.api.notes["n_today"]["body"] = "- [ ] Call the bank\n- [ ] Dinner\n- [ ] Torquio dev\n- [ ] Fix the sync bug\n"
        TodayLinks(self.sched.state_db).link("Fix the sync bug", item("t_bug", "Fix the sync bug", "n_dev"))
        self.assertEqual(self.t.assistant.today_carryover()["keep"], ["Call the bank"])

    def test_the_question_is_plain(self):
        self.add("Clean broken glass", "9:00 PM", day=Y)
        out = self.t.execute("rollover_proposal", {})
        self.assertTrue(out.startswith("From yesterday, still open:\n- 9:00 PM – Clean broken glass\n"), out)
        self.assertTrue(out.endswith("Should I plan these today at those times? Say which to leave out."), out)
        self.assertNotIn("like that", out)


if __name__ == "__main__":
    unittest.main()
