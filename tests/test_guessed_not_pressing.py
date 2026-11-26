"""A guessed urgency is never pressing: only the user's own word, a due date or the item's own words are.

The list sorter guesses how pressing each item is (now, soon, a horizon) so the week's options come in a sensible
order. That guess never makes an item pressing, "can't wait" or meant for today: the day's options don't call it
so, Python's own proposal leaves it off, the morning's carry-over doesn't add it and the lists the model reads
don't tag it. What the user said ("soon", their horizon), a due date, or "asap" in the item's own words still is.

Invented: someone who works late shifts at a pottery studio and paddles a canoe on days off."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_today import TITLES, TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.horizons import HorizonCatalog
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem

SHED = (("g_lids", "Glaze the teapot lids"), ("g_keel", "Patch the canoe keel"),
        ("g_bats", "Return the borrowed throwing bats"), ("g_shuttle", "Book the river shuttle"),
        ("g_wax", "Wax the kick wheel bearings"))


def shed_item(tid, title):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title="Workshop", parent_note_id="n_shed", list_role="list")


def load(db, tasks):
    """The tasks as the app reads them: role and urgency from the lists' catalog, then their horizons."""
    ListCatalog(db).apply(tasks)
    HorizonCatalog(db).apply(tasks, DAY)
    return tasks


class ShedCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:45"))
        TITLES.setdefault("n_shed", "Workshop")
        self.api.notes["n_shed"] = {"id": "n_shed", "body": "".join(f"- [ ] {t}\n" for _, t in SHED)}
        ListCatalog(self.sched.state_db).set_role("n_shed", "list", "Workshop")
        self.services.tasks.extend(shed_item(tid, title) for tid, title in SHED)
        self.db = self.sched.state_db
        self.a = self.t.assistant

    def task(self, tid):
        return next(t for t in self.services.tasks if t.id == tid)

    def urgency(self, tid, urgency, source, due=None):
        ListCatalog(self.db).set_urgency(tid, urgency, due, note_id="n_shed", title=self.task(tid).title,
                                         source=source)

    def ready(self):
        load(self.db, self.services.tasks)

    def options(self):
        return {c["title"]: c["why"] for c in self.a.proposals.candidates(self.t.day_plan)}

    def python_picks(self):
        return [i["title"] for i in self.a.proposals.default_items(self.t.day_plan)]


class TestTheDaysOptions(ShedCase):

    def test_a_guessed_soon_isnt_pressing(self):
        self.urgency("g_lids", "soon", "guess")
        self.ready()
        why = self.options()["Glaze the teapot lids"]
        self.assertNotIn("soon", why)
        self.assertNotIn("can't wait", why)
        self.assertIn("this week", why)
        self.assertNotIn("Glaze the teapot lids", self.python_picks())

    def test_a_guessed_now_or_day_isnt_pressing(self):
        self.urgency("g_shuttle", "now", "guess")
        HorizonCatalog(self.db).set_item("g_bats", "day", "n_shed", "Return the borrowed throwing bats",
                                         source="guess")
        self.ready()
        options = self.options()
        for title in ("Book the river shuttle", "Return the borrowed throwing bats"):
            self.assertNotIn("can't wait", options[title], title)
            self.assertNotIn("now", options[title], title)
            self.assertNotIn(title, self.python_picks())

    def test_a_guess_comes_after_what_is_pressing(self):
        self.urgency("g_lids", "soon", "guess")
        self.urgency("g_keel", "soon", "user")
        self.ready()
        order = list(self.options())
        self.assertLess(order.index("Patch the canoe keel"), order.index("Glaze the teapot lids"))

    def test_the_users_soon_is_still_pressing(self):
        self.urgency("g_keel", "soon", "user")
        self.ready()
        why = self.options()["Patch the canoe keel"]
        self.assertNotIn("could go", why)
        self.assertIn("Patch the canoe keel", self.python_picks())

    def test_the_users_horizon_wins_over_a_guessed_urgency(self):
        self.urgency("g_wax", "whenever", "guess")
        HorizonCatalog(self.db).set_item("g_wax", "day", "n_shed", "Wax the kick wheel bearings", source="user")
        self.ready()
        self.assertEqual(self.options()["Wax the kick wheel bearings"], "can't wait")
        self.assertIn("Wax the kick wheel bearings", self.python_picks())

    def test_due_tomorrow_keeps_its_deadline(self):
        tomorrow = DAY + timedelta(days=1)
        self.urgency("g_bats", "soon", "guess", due=tomorrow)
        self.task("g_bats").due = tomorrow.isoformat()
        self.ready()
        why = self.options()["Return the borrowed throwing bats"]
        self.assertIn("due", why)
        self.assertNotIn("could go", why)
        self.assertIn("Return the borrowed throwing bats", self.python_picks())

    def test_asap_in_its_own_words_is_pressing(self):
        self.services.tasks.append(shed_item("g_kiln", "Call about the kiln element asap"))
        self.urgency("g_kiln", "now", "guess")
        self.ready()
        why = self.options()["Call about the kiln element asap"]
        self.assertNotIn("could go", why)
        self.assertIn("Call about the kiln element asap", self.python_picks())


class TestTheCarryOverAndTheLists(ShedCase):

    def test_the_mornings_carry_over_adds_only_the_users_now(self):
        self.urgency("g_shuttle", "now", "guess")
        self.urgency("g_keel", "now", "user")
        self.ready()
        add = self.a.today_carryover()["add"]
        self.assertIn("g_keel", add)
        self.assertNotIn("g_shuttle", add)

    def test_a_guess_is_marked_only_on_the_item(self):
        self.urgency("g_lids", "soon", "guess")
        self.urgency("g_keel", "soon", "user")
        self.ready()
        self.assertTrue(self.task("g_lids").guessed)
        self.assertFalse(self.task("g_keel").guessed)
        self.assertFalse(self.task("g_wax").guessed)


class TestAMockMorning(unittest.TestCase):
    """The whole morning through the app's service with a mock model: the guessed-soon item isn't offered as
    pressing in DAY OPTIONS, isn't tagged soon in the lists, and doesn't go on today; the user's own soon is."""

    def setUp(self):
        self.when = at("09:45", 2)  # a Saturday: the studio shift is in the evening
        set_debug_time(self.when)
        self.addCleanup(set_debug_time, None)
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.day = DAY + timedelta(days=2)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        db = self.sched.state_db
        catalog = ListCatalog(db)
        catalog.set_role("n_shed", "list", "Workshop")
        catalog.set_urgency("g_lids", "soon", note_id="n_shed", title="Glaze the teapot lids", source="guess")
        catalog.set_urgency("g_keel", "soon", note_id="n_shed", title="Patch the canoe keel", source="user")
        tasks = load(db, [shed_item(tid, title) for tid, title in SHED])
        joplin = MagicMock()
        note = {"id": "n_shed", "title": "Workshop", "source_notebook": "1. Tasks",
                "body": "".join(f"- [ ] {t}\n" for _, t in SHED)}
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = tasks, [note]
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda *a: None, scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: self.day
        self.svc.bridge = MagicMock()
        self.m = self.svc.manager()
        self.m.tools.execute("add_event", {"title": "Studio shift", "start": "7:15 PM", "duration": "4 hours",
                                           "day": self.day.isoformat(), "category": "work"})

    def test_a_guessed_soon_stays_off_today(self):
        self.engine.set_next_response(reply("☀️ Good morning! The afternoon is open before the studio."))
        self.m.send_user_message("up and about")
        prompt = self.m.brain.build_system_prompt()
        self.assertIn("DAY OPTIONS", prompt)
        options = prompt.split("DAY OPTIONS", 1)[1].split("Free time:", 1)[0]
        lids = next(line for line in options.splitlines() if "Glaze the teapot lids" in line)
        self.assertNotIn("soon", lids)
        self.assertNotIn("can't wait", lids)
        keel = next(line for line in options.splitlines() if "Patch the canoe keel" in line)
        self.assertLess(options.index(keel), options.index("Could go this week, if"))
        self.assertLess(options.index("Could go this week, if"), options.index(lids))
        listed = [line for line in prompt.splitlines() if "'Glaze the teapot lids'" in line]
        self.assertTrue(listed)
        self.assertTrue(all("[wanted in the next few days]" not in line for line in listed))
        self.assertTrue(any("[wanted in the next few days]" in line for line in prompt.splitlines() if "'Patch the canoe keel'" in line))
        proposal = [p["title"] for p in self.m.tools.assistant.proposals.default_items(self.m.day_plan)]
        self.assertNotIn("Glaze the teapot lids", proposal)
        self.assertIn("Patch the canoe keel", proposal)
        on_plan = [s.title for s in self.m.day_plan.spans if s.start_dt.date() == self.day]
        self.assertNotIn("Glaze the teapot lids", on_plan)


if __name__ == "__main__":
    unittest.main()
