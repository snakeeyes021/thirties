"""Unmarked is unknown: what was left unmarked yesterday never goes on today's plan by itself.

One morning question covers it. Today gets it only with a due date of today or earlier, an urgency of the
user's own ("now", their horizon for today, "as soon as possible" in its words), or the user placing it. Being quick, a guessed urgency or horizon, or a day Python guessed for it earlier is never the reason. A
Today line that only labels an item (the heading above it) is never an item of its own.

Invented: a potter with a studio list and a canoe on days off."""

import json
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_today import TITLES, TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import DEADLINE, TODAY, WEEK, OpenPool, choose_horizon, sort_leftovers
from thirties_core.horizons import HorizonCatalog
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem

YESTERDAY = DAY - timedelta(days=1)
SATURDAY = DAY + timedelta(days=2)
STUDIO = (("t_permit", "Renew the kiln permit"), ("t_clay", "Order a bag of porcelain"),
          ("t_paddle", "Sand the spare paddle"), ("t_slip", "Mix a bucket of slip"))


def studio_item(tid, title, **kw):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title="Studio", parent_note_id="n_studio", list_role="list", **kw)


class StudioCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_studio", "Studio")
        self.api.notes["n_studio"] = {"id": "n_studio", "body": "".join(f"- [ ] {t}\n" for _, t in STUDIO)}
        ListCatalog(self.sched.state_db).set_role("n_studio", "list", "Studio")
        self.services.tasks.extend(studio_item(tid, title) for tid, title in STUDIO)
        self.db = self.sched.state_db
        self.a = self.t.assistant

    def task(self, tid):
        return next(t for t in self.services.tasks if t.id == tid)

    def keep(self, title, task_id, start, end, **extra):
        pool = OpenPool(self.db)
        pool.add([{"title": title, "task_id": task_id, "horizon": WEEK, "start": start.isoformat(),
                   "end": end.isoformat(), "when": "tomorrow"}], YESTERDAY - timedelta(days=1))
        if extra:
            pool.set(title, **extra)

    def options(self):
        return {c["title"]: c["why"] for c in self.a.proposals.candidates(self.t.day_plan)}


class TestTheFirstGuess(StudioCase):
    """Python's first guess: today only on a date or the user's own word."""

    def guess(self, tid, **kw):
        return choose_horizon(self.a, self.task(tid), self.task(tid).title, DAY, **kw)

    def test_quick_is_no_reason(self):
        self.assertEqual(self.guess("t_clay", said_minutes=10), WEEK)

    def test_a_guessed_horizon_or_urgency_is_no_reason(self):
        HorizonCatalog(self.db).set_item("t_clay", "day", source="guess")
        ListCatalog(self.db).set_urgency("t_paddle", "now", source="guess", title="Sand the spare paddle")
        self.assertEqual(self.guess("t_clay"), WEEK)
        self.assertEqual(self.guess("t_paddle"), WEEK)

    def test_due_today_or_overdue_or_the_users_word(self):
        self.task("t_permit").due = DAY.isoformat()
        self.task("t_slip").due = (DAY - timedelta(days=3)).isoformat()
        HorizonCatalog(self.db).set_item("t_clay", "day", source="user")
        ListCatalog(self.db).set_urgency("t_paddle", "now", source="user", title="Sand the spare paddle")
        for tid in ("t_permit", "t_slip", "t_clay", "t_paddle"):
            self.assertEqual(self.guess(tid), TODAY, tid)

    def test_due_tomorrow_waits_for_its_day(self):
        self.task("t_permit").due = (DAY + timedelta(days=1)).isoformat()
        self.assertEqual(self.guess("t_permit"), DEADLINE)


class TestTheDaysOptions(StudioCase):
    """What the day can draw on: a day Python guessed is never "meant for today"; this morning's unknowns wait."""

    def test_a_guessed_day_that_came_is_an_ordinary_option(self):
        self.keep("Sand the spare paddle", "t_paddle", DAY, DAY)
        why = self.options()["Sand the spare paddle"]
        self.assertNotIn("meant for", why)
        self.assertIn("this week", why)

    def test_a_guessed_day_that_went_by_isnt_overdue(self):
        self.keep("Sand the spare paddle", "t_paddle", DAY - timedelta(days=2), DAY - timedelta(days=2))
        self.assertNotIn("meant for", self.options()["Sand the spare paddle"])

    def test_the_users_day_is_meant_for_today(self):
        self.keep("Sand the spare paddle", "t_paddle", DAY, DAY, placed_by="user")
        self.assertIn("meant for", self.options()["Sand the spare paddle"])

    def test_this_mornings_unknowns_arent_offered(self):
        self.keep("Mix a bucket of slip", "t_slip", DAY + timedelta(days=1), DAY + timedelta(days=1))
        self.keep("Order a bag of porcelain", "t_clay", DAY + timedelta(days=1), DAY + timedelta(days=1))
        self.task("t_clay").due = DAY.isoformat()
        self.db.set_sync_meta(f"leftovers:{DAY.isoformat()}", json.dumps(
            {"pooled": ["Mix a bucket of slip", "Order a bag of porcelain"]}))
        options = self.options()
        self.assertNotIn("Mix a bucket of slip", options)
        self.assertIn("Order a bag of porcelain", options)  # due today: it may go on

    def test_a_python_proposal_leaves_them_off(self):
        self.keep("Sand the spare paddle", "t_paddle", DAY, DAY)
        self.db.set_sync_meta(f"leftovers:{DAY.isoformat()}", json.dumps({"pooled": ["Sand the spare paddle"]}))
        titles = [i["title"] for i in self.a.proposals.default_items(self.t.day_plan)]
        self.assertNotIn("Sand the spare paddle", titles)


class TestPuttingOnToday(StudioCase):

    def test_a_guessed_day_gone_by_isnt_put_on_today(self):
        old = DAY - timedelta(days=2)
        placed, _ = self.a.before.place_today([{"title": "Sand the spare paddle", "task_id": "t_paddle",
                                                "start": old.isoformat(), "end": old.isoformat()}])
        self.assertEqual(placed, [])
        placed, _ = self.a.before.place_today([{"title": "Sand the spare paddle", "task_id": "t_paddle",
                                                "start": old.isoformat(), "end": old.isoformat(),
                                                "placed_by": "user"}])
        self.assertEqual(placed, ["Sand the spare paddle"])

    def morning_with(self, *titles):
        for i, (tid, title) in enumerate(t for t in STUDIO if t[1] in titles):
            self.t.execute("add_event", {"title": title, "start": f"{2 + i}:00 PM", "duration": "30 min",
                                         "day": YESTERDAY.isoformat(), "task_id": tid, "category": "task"})
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")
        return self.t.execute("rollover_proposal", {})

    def test_the_morning_proposal_keeps_unknowns_off_today(self):
        self.morning_with("Order a bag of porcelain", "Sand the spare paddle")
        on_plan = [s.title for s in self.t.day_plan.spans if s.start_dt.date() == DAY]
        self.assertNotIn("Order a bag of porcelain", on_plan)
        self.assertNotIn("Sand the spare paddle", on_plan)

    def test_the_model_cant_keep_an_unknown_for_today(self):
        self.morning_with("Order a bag of porcelain", "Renew the kiln permit")
        self.task("t_permit").due = DAY.isoformat()
        self.t.user_message = "morning"
        try:
            self.t.assistant.ask_json = lambda system, user: {"answer": "STILL_NEEDED", "reason": "scripted"}
            self.t.execute("propose_leftovers", {"keep": [{"item": "Order a bag of porcelain", "when": "today",
                                                           "kind": "task"},
                                                          {"item": "Renew the kiln permit", "when": "today"}]})
        finally:
            self.t.user_message = None
        pool = OpenPool(self.db).all()
        # off today, and never tomorrow by itself: a later day this week (not_today.py)
        self.assertEqual(pool["order a bag of porcelain"]["start"], (DAY + timedelta(days=2)).isoformat())
        self.assertEqual(pool["renew the kiln permit"]["start"], DAY.isoformat())  # due today: it stays
        on_plan = [s.title for s in self.t.day_plan.spans if s.start_dt.date() == DAY]
        self.assertNotIn("Order a bag of porcelain", on_plan)

    def test_the_user_naming_it_puts_it_on_today(self):
        self.morning_with("Order a bag of porcelain")
        self.t.user_message = "up now, the porcelain order has to go out today"
        try:
            self.t.execute("propose_leftovers", {"keep": [{"item": "Order a bag of porcelain", "when": "today"}]})
        finally:
            self.t.user_message = None
        self.assertEqual(OpenPool(self.db).all()["order a bag of porcelain"]["start"], DAY.isoformat())


class TestASkipStaysOff(StudioCase):
    """Skipped yesterday by the user, and still on the Today note: it waits on its list, off today's options."""

    def test_not_offered_the_next_morning(self):
        from thirties_core.today import TodayLinks
        self.t.execute("add_event", {"title": "Sand the spare paddle", "start": "2:00 PM", "duration": "30 min",
                                     "day": YESTERDAY.isoformat(), "task_id": "t_paddle", "category": "task"})
        span = next(s for s in self.t.plan_for(YESTERDAY).spans if s.title == "Sand the spare paddle")
        self.t.execute("log_outcome", {"event": span.id, "status": "skipped", "day": YESTERDAY.isoformat()})
        self.api.notes["n_today"]["body"] = "- [ ] Sand the spare paddle\n"
        TodayLinks(self.db).link("Sand the spare paddle", self.task("t_paddle"))
        with self.db._get_connection() as conn:  # put on Today yesterday
            conn.execute("UPDATE today_links SET updated_at = ?", (at("10:00", -1).isoformat(),))
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")
        self.t.execute("rollover_proposal", {})
        self.assertNotIn("Sand the spare paddle", self.options())
        self.assertNotIn("Sand the spare paddle", [s.title for s in self.t.day_plan.spans])


class TestALabelIsntAnItem(StudioCase):
    """A Today line that only labels its item (the heading above it, cut short) is kept by the item's own name."""

    def test_kept_and_asked_by_the_items_name(self):
        from thirties_core.today import TodayLinks
        self.api.notes["n_today"]["body"] = "- [ ] Shop chores\n"
        self.services.tasks.append(TaskItem(id="tt_chores", source_notebook="1. Tasks", title="Shop chores",
                                            is_checkbox_item=True, parent_note_title="Today To-Do",
                                            parent_note_id="n_today", list_role="today"))
        TodayLinks(self.db).link("Shop chores", self.task("t_permit"))
        with self.db._get_connection() as conn:  # put on Today yesterday
            conn.execute("UPDATE today_links SET updated_at = ?", (at("10:00", -1).isoformat(),))
        self.db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")
        out = sort_leftovers(self.a, DAY)
        self.assertNotIn("Shop chores", out["pooled"])
        self.assertNotIn("Shop chores", out["ask"])
        self.assertIn("Renew the kiln permit", out["pooled"])
        self.assertIn("Shop chores", out["off_today"])  # off Today: the item waits on its own list


class TestAMockMorning(unittest.TestCase):
    """The whole morning through the app's service with a mock model. Yesterday's unmarked one-offs (a quick one,
    one with a guessed horizon for the day) end up off today's plan and out of the day's options; one that an
    earlier morning kept for today by Python's own guess is an ordinary option, never "meant for today", and Python's
    own proposal doesn't put it on."""

    def setUp(self):
        self.when = at("09:30", 2)  # a Saturday: a free day, room for anything
        set_debug_time(self.when)
        self.addCleanup(set_debug_time, None)
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        tasks = [studio_item(tid, title) for tid, title in STUDIO]
        db = self.sched.state_db
        HorizonCatalog(db).set_item("t_clay", "day", source="guess")
        friday = SATURDAY - timedelta(days=1)
        OpenPool(db).add([{"title": "Mix a bucket of slip", "task_id": "t_slip", "horizon": WEEK, "when": "tomorrow",
                           "start": SATURDAY.isoformat(), "end": SATURDAY.isoformat(), "why": "tomorrow",
                           "proposed_on": friday.isoformat()}], friday - timedelta(days=1))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = tasks, []
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda *a: None, scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: SATURDAY
        self.svc.bridge = MagicMock()
        self.m = self.svc.manager()
        for i, (tid, title) in enumerate(STUDIO[1:3]):
            self.m.tools.execute("add_event", {"title": title, "start": f"{2 + i}:00 PM", "duration": "10 minutes",
                                               "day": friday.isoformat(), "task_id": tid, "category": "task"})
        self.m.tools.execute("set_item_facts", {"event": "Sand the spare paddle", "length": "10 minutes"})

    def test_yesterdays_unknowns_stay_off_today(self):
        self.engine.set_next_response(reply("☀️ Good morning! The afternoon is open."))
        self.m.send_user_message("morning, I'm up")
        on_plan = [s.title for s in self.m.day_plan.spans if s.start_dt.date() == SATURDAY]
        for title in ("Order a bag of porcelain", "Sand the spare paddle", "Mix a bucket of slip"):
            self.assertNotIn(title, on_plan)
        prompt = self.m.brain.build_system_prompt()
        self.assertIn("DAY OPTIONS", prompt)
        options = prompt.split("DAY OPTIONS", 1)[1].split("Free time:", 1)[0]
        for title in ("Order a bag of porcelain", "Sand the spare paddle"):
            self.assertNotIn(title, options)
        slip = next(line for line in options.splitlines() if "Mix a bucket of slip" in line)
        self.assertNotIn("meant for", slip)
        proposal = self.m.tools.assistant.proposals.default_items(self.m.day_plan)
        self.assertNotIn("Mix a bucket of slip", [p["title"] for p in proposal])


if __name__ == "__main__":
    unittest.main()
