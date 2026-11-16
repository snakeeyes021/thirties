"""A nested list item is named with where it sits ("Utility accounts › Harbor Water") wherever Thirties names it to
the user, while its stored title and its list line stay as written (item_path.py); the morning's reply lists the day
once, with no what's-next slice under it (morning_reply.py). Invented lists; DAY is Thu Oct 15."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, line_with, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.item_path import label, leaf, parents, resolve, span_label
from thirties_core.joplin_engine import checklist_tasks
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import PlanTools

NOTE = """# Utility accounts
- [ ] Harbor Water
- [ ] Northgate Power
# Moving out
- [ ] Gas account
    - [ ] final reading
- [ ] Broadband account
    - [ ] final reading
# Garden
- [ ] Raised beds
    - [ ] North bed
        - [ ] order soil
"""
LOOSE = "- [ ] Return the library books\n"


def nested_tasks():
    tasks = checklist_tasks("n_party", "Moving house", "Home", NOTE, [])
    tasks += checklist_tasks("n_errands", "Errands", "Home", LOOSE, [])
    for t in tasks:
        t.list_role, t.urgency = "list", "now"
    return tasks


def find(tasks, title, under=""):
    by_id = {t.id: t for t in tasks}
    return next(t for t in tasks if t.title == title and (not under or by_id[t.parent_task_id].title == under))


class TestLabel(unittest.TestCase):

    def setUp(self):
        self.tasks = nested_tasks()

    def test_heading_or_parent_line_then_the_item(self):
        self.assertEqual(label(find(self.tasks, "Harbor Water"), self.tasks), "Utility accounts › Harbor Water")
        self.assertEqual(label(find(self.tasks, "final reading", "Gas account"), self.tasks),
                         "Moving out › Gas account › final reading")
        self.assertEqual(label(find(self.tasks, "final reading", "Broadband account"), self.tasks),
                         "Moving out › Broadband account › final reading")

    def test_at_most_two_parents_the_nearest_ones(self):
        self.assertEqual(label(find(self.tasks, "order soil"), self.tasks), "Raised beds › North bed › order soil")

    def test_an_item_with_no_heading_or_parent_is_its_own_title(self):
        books = find(self.tasks, "Return the library books")
        self.assertEqual(parents(books, self.tasks), [])
        self.assertEqual(label(books, self.tasks), "Return the library books")

    def test_routine_steps_and_todays_headings_add_nothing(self):
        steps = checklist_tasks("n_r", "Mornings", "Home", "# Early\n- [ ] Stretch\n", [])
        today = checklist_tasks("n_t", "Today", "Home", "# If there's time\n- [ ] Pack lunch\n    - [ ] fruit\n", [])
        steps[0].list_role = "routine"
        for t in today:
            t.list_role = "today"
        self.assertEqual(label(steps[0], steps), "Stretch")
        self.assertEqual(label(today[0], today), "Pack lunch")  # the Today note's headings sort the day
        self.assertEqual(label(today[1], today), "Pack lunch › fruit")  # its parent line still says what it is

    def test_display_only_the_title_stays_and_a_label_said_back_finds_the_item(self):
        novak = find(self.tasks, "final reading", "Broadband account")
        self.assertEqual(novak.title, "final reading")
        self.assertIs(resolve("Moving out › Broadband account › final reading", self.tasks), novak)
        self.assertIs(resolve("moving out ›  broadband account › Final reading", self.tasks), novak)
        self.assertIsNone(resolve("final reading", self.tasks))  # not a label: the usual lookup takes it
        self.assertEqual(leaf("Utility accounts › Harbor Water"), "Harbor Water")

    def test_a_plan_entry_for_the_item_is_named_the_same_way(self):
        lee = find(self.tasks, "final reading", "Gas account")
        span = EventSpan("s1", "final reading", at("17:00"), at("17:30"), SpanCategory.TASK, task_id=lee.id)
        self.assertEqual(span_label(span, self.tasks), "Moving out › Gas account › final reading")
        piece = EventSpan("s2", "Gas account: final reading", at("17:00"), at("17:30"), SpanCategory.TASK,
                          task_id=lee.id)  # a project's piece, as placed by its item
        self.assertEqual(span_label(piece, self.tasks), "Moving out › Gas account › final reading")
        renamed = EventSpan("s3", "Phone the Lees", at("17:00"), at("17:30"), SpanCategory.TASK, task_id=lee.id)
        self.assertEqual(span_label(renamed, self.tasks), "Phone the Lees")  # the user's own words stand


class PlanCase(unittest.TestCase):
    """Today at 4:40 PM, with two nested items and a plain one on the plan."""

    def setUp(self):
        set_debug_time(at("16:40"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.tasks = nested_tasks()
        self.lee = find(self.tasks, "final reading", "Gas account")
        self.novak = find(self.tasks, "final reading", "Broadband account")
        self.rivera = find(self.tasks, "Harbor Water")
        for sid, task, start, end in (("lee", self.lee, "17:00", "17:30"), ("novak", self.novak, "17:30", "18:00"),
                                      ("rivera", self.rivera, "18:00", "18:30")):
            self.sched.state_db.upsert_span(EventSpan(sid, task.title, at(start), at(end), SpanCategory.TASK,
                                                      task_id=task.id))
        self.sched.state_db.upsert_span(EventSpan("walk", "Evening walk", at("19:00"), at("19:30"),
                                                  SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db, self.tasks))


class TestWhereItsNamed(PlanCase):

    def test_whats_next(self):
        from thirties_core import whats_next
        text = whats_next.summary(self.t, at("16:40"), "rest")
        line_with(text, "5:00 PM", "Moving out › Gas account › final reading")
        line_with(text, "Moving out › Broadband account › final reading")
        line_with(text, "Utility accounts › Harbor Water")
        line_with(text, "Evening walk")

    def test_the_schedule_and_whats_behind(self):
        set_debug_time(at("18:40"))
        out = self.t.execute("show_schedule", {})
        line_with(out, "Utility accounts › Harbor Water")
        behind = out.split("Not marked yet:", 1)[1]
        self.assertIn("Moving out › Gas account › final reading", behind)
        self.assertIn("Moving out › Broadband account › final reading", behind)

    def test_what_the_model_reads(self):
        text = self.t.schedule_text()
        line_with(text, "Moving out › Gas account › final reading", "id=lee")
        out = self.t.execute("add_event", {"title": "Northgate Power", "start": "8:00 PM", "duration": "30 minutes",
                                           "task_id": find(self.tasks, "Northgate Power").id, "category": "task"})
        self.assertIn("Utility accounts › Northgate Power", out)

    def test_the_same_words_on_two_list_lines_are_two_items(self):
        self.t.day_plan.spans = [s for s in self.t.day_plan.spans if s.id != "novak"]
        out = self.t.execute("add_event", {"title": "final reading", "start": "8:00 PM", "duration": "30 minutes",
                                           "task_id": self.novak.id, "category": "task"})
        self.assertFalse(out.startswith("NOT_DONE"), out)  # not "already planned": that's the Gas account's
        self.assertIn("Moving out › Broadband account › final reading", out)
        again = self.t.execute("add_event", {"title": "final reading", "start": "9:00 PM", "duration": "30 minutes",
                                             "task_id": self.novak.id, "category": "task"})
        self.assertIn("already planned", again)  # the same item twice still isn't a second copy

    def test_ticking_by_the_label_marks_the_real_item_and_keeps_its_title(self):
        out = self.t.execute("log_outcome", {"event": "Moving out › Broadband account › final reading",
                                             "status": "done"})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        spans = {s.id: s for s in self.t.day_plan.spans}
        self.assertEqual(spans["novak"].status, SpanStatus.DONE)
        self.assertEqual(spans["lee"].status, SpanStatus.PLANNED)
        self.assertEqual(spans["novak"].title, "final reading")  # stored as it was
        self.assertIs(self.t.assistant._exact_task("Moving out › Gas account › final reading"), self.lee)

    def test_the_mornings_list(self):
        from thirties_core.day_lists import planned_ahead_text
        text = planned_ahead_text(self.t.assistant)
        line_with(text, "5:00 PM", "Moving out › Gas account › final reading")
        line_with(text, "6:00 PM", "Utility accounts › Harbor Water")

    def test_the_days_options_and_the_placed_lines(self):
        okafor, soil = find(self.tasks, "Northgate Power"), find(self.tasks, "order soil")
        text = self.t.assistant.proposals.options_text()
        line_with(text, "Utility accounts › Northgate Power", f"task_id={okafor.id}")
        line_with(text, "Raised beds › North bed › order soil", f"task_id={soil.id}")
        self.t.day_plan.spans = [s for s in self.t.day_plan.spans if not s.task_id]  # the evening is free
        out = self.t.execute("propose_day", {"items": [
            {"item": okafor.id, "start": "5:00 PM", "duration": "30 minutes"},
            {"item": soil.id, "start": "5:30 PM", "duration": "30 minutes"}]})
        line_with(out, "5:00 PM", "Utility accounts › Northgate Power")
        line_with(out, "5:30 PM", "Raised beds › North bed › order soil")
        placed = {s.task_id: s.title for s in self.t.day_plan.spans if s.task_id}
        self.assertEqual(placed[okafor.id], "Northgate Power")  # stored as its own line
        self.assertEqual(placed[soil.id], "North bed: order soil")  # a piece, by its item, as before


class TestNotices(unittest.TestCase):
    """The background's check-ins and start notices say the label; what they track stays the item's own line."""

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"
        self.tasks = nested_tasks()
        self.rivera = find(self.tasks, "Harbor Water")
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = self.tasks, []
        self.sent = []
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.sched.state_db.upsert_span(EventSpan("rv", "Harbor Water", at("17:00"), at("17:30"),
                                                  SpanCategory.TASK, task_id=self.rivera.id))

    def test_start_and_check_in(self):
        self.svc.starts(at("17:00"))
        self.assertIn("Utility accounts › Harbor Water", self.sent[-1][1])
        self.svc.check_ins(at("17:45"))
        self.assertIn("did you get to Utility accounts › Harbor Water", self.sent[-1][1])

    def test_the_plate_before_the_user_is_up(self):
        self.assertEqual(self.svc._plate(self.svc.manager().day_plan), ["Utility accounts › Harbor Water"])


class TestBeforeToday(PlanCase):

    def test_rows_read_with_the_label_and_act_on_the_item(self):
        from thirties_core.carry import OpenPool
        OpenPool(self.sched.state_db).add([{"title": self.rivera.title, "task_id": self.rivera.id,
                                            "horizon": "week", "when": "this week"}], DAY - timedelta(days=1))
        rows = self.t.assistant.before.rows()
        row = next(r for r in rows["keep"] + rows["drop"] if r["title"] == "Harbor Water")
        self.assertEqual(row["label"], "Utility accounts › Harbor Water")


class TestThePromptsLists(PlanCase):

    def test_the_backlog_names_nested_items(self):
        from thirties_core.brains.frontier import FrontierBrain
        notes = [{"id": "n_party", "title": "Moving house", "source_notebook": "Home", "body": NOTE}]
        text = FrontierBrain(self.t, None, tasks=self.tasks, active_notes=notes).build_backlog_summary()
        line_with(text, f"[ID: {self.novak.id}]", "named: Moving out › Broadband account › final reading")
        line_with(text, f"[ID: {self.rivera.id}]", "named: Utility accounts › Harbor Water")
        line_with(text, "'Gas account'", "named: Moving out › Gas account")


class TestPeriodProposal(unittest.TestCase):
    """The new week's offer: what comes from the month's plan under one heading, said once; nested items named."""

    def setUp(self):
        set_debug_time(at("10:00", 4))  # Monday Oct 19: a new week
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.tasks = nested_tasks()
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=4))
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db, self.tasks))
        from thirties_core.horizons import HorizonPlanner, parse_period
        self.hp = HorizonPlanner(self.t)
        month = parse_period("this month", DAY + timedelta(days=4))
        for title, under in (("Harbor Water", ""), ("final reading", "Gas account")):
            self.hp.pin(find(self.tasks, title, under), month)

    def test_where_they_come_from_is_said_once(self):
        text = self.hp.offer_session()
        lines = text.splitlines()
        self.assertEqual(text.count("From this month's plan:"), 1, text)
        self.assertNotIn("from the plan for", text)
        at_heading = lines.index("From this month's plan:")
        under = lines[at_heading + 1:-1]
        self.assertIn("Utility accounts › Harbor Water", under)
        self.assertIn("Moving out › Gas account › final reading", under)
        self.assertFalse(any(line.startswith("- ") for line in lines))  # plain lines
        self.assertTrue(lines[-1].startswith("Want this week"))


class TestTheMorningReply(unittest.TestCase):
    """The reply that builds the day lists it once: no what's-next slice under it; the replies after it keep it."""

    def setUp(self):
        set_debug_time(at("09:25"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        self.sched.state_db.upsert_span(EventSpan("pots", "Repot the fern", at("10:00"), at("10:30"),
                                                  SpanCategory.ACTIVITY))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: None, scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=self.engine, sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.m = self.svc.manager()

    def say(self, *replies, said="ok"):
        from tests.test_conversation import reply
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.m.send_user_message(said)

    def test_no_slice_under_the_morning_then_as_usual(self):
        from thirties_core import morning
        out = self.say('CALL add_event {"title": "Fold laundry", "start": "11:00 AM", "duration": "30 minutes"}',
                       "Morning! Today: Repot the fern at 10, Fold laundry at 11.", said="morning")
        self.assertTrue(morning.is_done(self.sched.state_db, DAY))
        self.assertNotIn("The rest of today:", out)
        self.assertNotIn("Now:", out)
        self.assertEqual(out.count("Fold laundry"), 1, out)
        set_debug_time(at("09:40"))
        out = self.say('CALL add_event {"title": "Call the vet", "start": "11:30 AM", "duration": "30 minutes"}',
                       "Added the vet call.", said="add a vet call at 11:30")
        self.assertIn("The rest of today:", out)  # a later change gets what's next, as before


if __name__ == "__main__":
    unittest.main()
