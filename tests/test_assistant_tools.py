"""Tests for outcomes, review, memory, profile, history, statistics and any-day tools."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler, occupant
from thirties_core.assistant_tools import Services, rank_tasks
from thirties_core.astronomy import set_debug_time
from thirties_core.memory import ConversationStore, EnergyLog, MemoryStore, ProfileStore
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.stats import activity_key, fact_lines, unreviewed_spans

TASKS = [
    TaskItem(id="t_taxes", source_notebook="1. Tasks", title="File quarterly taxes", parent_note_title="Errands/Admin"),
    TaskItem(id="t_brad", source_notebook="1. Tasks", title="Message Brad about a weekend trip",
             parent_note_title="Errands/Admin", section="Probation", someday=True),
    TaskItem(id="t_bridge", source_notebook="3. Creative", title="Compose the bridge section",
             parent_note_title="Natalie Piece", deferred_count=2),
]


class ToolsTestCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("18:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.joplin = MagicMock()
        self.api = FakeJoplinAPI({"t_taxes": {"id": "t_taxes", "body": ""},
                                  "n_errands": {"id": "n_errands", "body": "# Errands\n- [ ] Call the bank\n"}})
        self.joplin.api_client = self.api
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "token"
        self.services = Services.for_db(self.sched.state_db, list(TASKS), self.joplin)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)


class TestOutcomes(ToolsTestCase):

    def test_done_with_reported_times_and_joplin_checkoff(self) -> None:
        self.t.execute("add_event", {"title": "Taxes", "start": "4:00 PM", "duration": "1 hour", "task_id": "t_taxes"})
        out = self.t.execute("log_outcome", {"event": "Taxes", "status": "done", "started": "4:15 PM", "finished": "5:45 PM"})
        self.assertIn("Marked Taxes done (4:15 PM – 5:45 PM, reported)", out)
        self.assertIn("checked it off in Joplin", out)
        self.assertEqual([u[0] for u in self.api.updates], ["t_taxes"])
        span = next(s for s in self.sched.build_day_plan(DAY)[0].spans if s.title == "Taxes")
        self.assertEqual((span.status, span.actual_start, span.time_source), (SpanStatus.DONE, at("16:15"), "reported"))
        # The item now sits at its real times (lists show the blocks it touched); the plan is kept for statistics
        self.assertEqual((span.start_dt, span.end_dt, span.planned_start, span.planned_end),
                         (at("16:15"), at("17:45"), at("16:00"), at("17:00")))
        self.assertEqual((span.actual_start, span.actual_end), (at("16:15"), at("17:45")))
        self.assertEqual(self.sched.state_db.get_task_history("t_taxes").completed_date, DAY.isoformat())

    def test_finish_time_only_is_estimated(self) -> None:
        self.t.execute("add_event", {"title": "Reading", "start": "4:00 PM", "duration": "1 hour"})
        out = self.t.execute("log_outcome", {"event": "Reading", "status": "done", "finished": "5:30 PM"})
        self.assertIn("4:00 PM – 5:30 PM, estimated", out)  # assumed it started on time and ran long
        self.t.execute("add_event", {"title": "Shower", "start": "3:00 PM", "duration": "30 min"})
        out = self.t.execute("log_outcome", {"event": "Shower", "status": "done", "finished": "5:50 PM"})
        self.assertIn("5:20 PM – 5:50 PM, estimated", out)  # not a 2h50m shower (10-20): it started late

    def test_done_with_no_time_is_when_it_was_said(self) -> None:
        # 10-25: "kitties are fed" at 10:44 was stored at the planned 10:00, and so was every tick in the Day page
        self.t.execute("add_event", {"title": "Feed kitties", "start": "5:00 PM", "duration": "5 min"})
        set_debug_time(at("17:44"))
        out = self.t.execute("log_outcome", {"event": "Feed kitties", "status": "done"})
        self.assertIn("5:39 PM – 5:44 PM, estimated", out)
        self.t.execute("add_event", {"title": "Lunch", "start": "12:00 PM", "duration": "30 min"})
        out = self.t.execute("log_outcome", {"event": "Lunch", "status": "done"})  # hours later: nobody knows when
        self.assertIn("12:00 PM – 12:30 PM, planned", out)

    def test_adding_whats_already_planned_is_a_move_or_a_second_session(self) -> None:
        # 10-27 to 10-30: re-adding instead of moving left ghost copies that nagged and rolled over
        self.t.execute("add_event", {"title": "So Voice! dev", "start": "3:30 PM", "duration": "1 hour"})
        out = self.t.execute("add_event", {"title": "so voice! dev", "start": "5:00 PM", "duration": "1 hour"})
        self.assertTrue(out.startswith("NOT_DONE") and "update_event" in out and "again=true" in out, out)
        out = self.t.execute("add_event", {"title": "So Voice! dev", "start": "8:00 PM", "duration": "1 hour",
                                           "again": True})
        self.assertTrue(out.startswith("Added"), out)
        self.assertEqual(len([s for s in self.t.day_plan.spans if s.title == "So Voice! dev"]), 2)

    def test_an_outcome_by_title_asks_when_yesterdays_copy_is_open(self) -> None:
        # 10-28: "clearing yesterday's check-in" marked today's Email back GitHub skipped
        self.t.execute("add_event", {"title": "Email back GitHub", "start": "7:30 PM", "duration": "30 min",
                                     "day": "yesterday"})
        self.t.execute("add_event", {"title": "Email back GitHub", "start": "3:00 PM", "duration": "30 min"})
        out = self.t.execute("log_outcome", {"event": "Email back GitHub", "status": "skipped"})
        self.assertTrue(out.startswith("NOT_DONE") and "Which one?" in out, out)
        old = next(s for s in self.t.plan_for("yesterday").spans if s.title == "Email back GitHub")
        out = self.t.execute("log_outcome", {"event": old.id, "status": "skipped", "day": "yesterday"})
        self.assertTrue(out.startswith("[") and "Marked Email back GitHub skipped" in out, out)
        today = next(s for s in self.t.day_plan.spans if s.title == "Email back GitHub")
        self.assertEqual(today.status, SpanStatus.PLANNED)

    def test_a_list_item_named_by_its_wording_marks_its_planned_copy(self) -> None:
        # 10-30 sim: ticked in Joplin by its list wording, the planned copy stayed open and was nagged for hours
        self.t.execute("add_event", {"title": "Taxes call", "start": "4:00 PM", "duration": "30 min",
                                     "task_id": "t_taxes"})
        task = next(t for t in self.services.tasks if t.id == "t_taxes")
        out = self.t.execute("log_outcome", {"event": task.title, "status": "done"})
        self.assertTrue(out.startswith("Marked Taxes call done"), out)
        self.assertEqual(next(s for s in self.t.day_plan.spans if s.title == "Taxes call").status, SpanStatus.DONE)

    def test_something_done_that_was_never_planned_is_recorded(self) -> None:
        # 10-30 sim: "refusing to log my litter box completion just because it wasn't pre-slotted"
        out = self.t.execute("log_outcome", {"event": "Emptied the litter boxes", "status": "done"})
        self.assertTrue(out.startswith("Recorded Emptied the litter boxes done (5:30 PM – 6:00 PM, estimated)"), out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Emptied the litter boxes")
        self.assertEqual(span.status, SpanStatus.DONE)

    def test_skipped_task_counts_a_deferral(self) -> None:
        self.t.execute("add_event", {"title": "Bridge", "start": "4:00 PM", "duration": "1 hour", "task_id": "t_bridge"})
        out = self.t.execute("log_outcome", {"event": "Bridge", "status": "skipped"})
        self.assertIn("put off 1 times", out)
        self.assertEqual(self.api.updates, [])

    def test_unscheduled_backlog_task_done(self) -> None:
        out = self.t.execute("log_outcome", {"event": "the taxes", "status": "done", "finished": "3:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE) and "File quarterly taxes" in out, out)  # near: named, not ticked
        tid = next(t.id for t in self.t.assistant.s.tasks if t.title == "File quarterly taxes")
        out = self.t.execute("log_outcome", {"event": tid, "status": "done", "finished": "3:00 PM"})
        self.assertIn("Marked 'File quarterly taxes' done", out)
        self.assertIn("logged it 2:30 PM – 3:00 PM", out)

    def test_outcomes_not_for_work_or_unknown_items(self) -> None:
        self.assertTrue(self.t.execute("log_outcome", {"event": "Work", "status": "done"}).startswith(NOT_DONE))
        self.assertTrue(self.t.execute("log_outcome", {"event": "zzz", "status": "skipped"}).startswith(NOT_DONE))
        self.assertTrue(self.t.execute("log_outcome", {"event": "zzz", "status": "done"}).startswith("Recorded"))
        self.assertTrue(self.t.execute("log_outcome", {"event": "Work", "status": "maybe"}).startswith(NOT_DONE))

    def test_review_day_lists_unreviewed_and_suggests_rollover(self) -> None:
        self.t.execute("add_event", {"title": "Bridge", "start": "4:00 PM", "duration": "1 hour", "task_id": "t_bridge"})
        self.t.execute("add_event", {"title": "Walk", "start": "9:00 PM", "duration": "30 min"})
        out = self.t.execute("review_day", {})
        self.assertIn("4:00 PM – 5:00 PM: Bridge", out)
        self.assertNotIn("Walk", out)  # hasn't happened yet
        self.assertIn("Bridge: tomorrow 7:00 AM (1h)", out)

    def test_energy(self) -> None:
        self.assertIn("4/5", self.t.execute("log_energy", {"level": "4"}))
        self.assertEqual(self.services.energy.entries()[0]["level"], 4)


class TestAnyDay(ToolsTestCase):

    def test_add_tomorrow_does_not_touch_today(self) -> None:
        out = self.t.execute("add_event", {"title": "Move NAS", "start": "9:00 AM", "duration": "1 hour", "day": "tomorrow"})
        self.assertTrue(out.startswith("[Friday, October 16] Added Move NAS"), out)
        self.assertNotIn("Move NAS", [s.title for s in self.t.day_plan.spans])
        self.assertEqual(occupant(self.sched.build_day_plan(DAY + timedelta(days=1))[0], "09:00", 1), "Move NAS")

    def test_week_overview(self) -> None:
        self.t.execute("add_event", {"title": "Piano", "start": "10:00 AM", "duration": "1 hour", "day": "Saturday"})
        out = self.t.execute("week_overview", {})
        lines = out.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[0].startswith("Thu Oct 15: work 8:30 AM–4:00 PM"), lines[0])
        sat = next(l for l in lines if l.startswith("Sat"))
        self.assertIn("no work; 1 planned", sat)
        self.assertIn("Piano", sat)

    def test_move_to_another_day(self) -> None:
        self.t.execute("add_event", {"title": "Email recruiter", "start": "7:00 PM", "duration": "30 min"})
        out = self.t.execute("update_event", {"event": "Email recruiter", "to_day": "tomorrow", "start": "9:00 AM"})
        self.assertIn("to Friday, October 16, 9:00 AM – 9:30 AM", out)
        self.assertNotIn("Email recruiter", [s.title for s in self.t.day_plan.spans])
        self.assertEqual(occupant(self.sched.build_day_plan(DAY + timedelta(days=1))[0], "09:00", 1), "Email recruiter")
        self.t.execute("add_event", {"title": "Call", "start": "4:00 PM", "duration": "30 min"})
        out = self.t.execute("update_event", {"event": "Call", "to_day": "Saturday"})
        self.assertIn("Saturday, October 17, 4:00 PM", out)

    def test_moving_a_skipped_item_keeps_the_record(self) -> None:
        self.t.execute("add_event", {"title": "Email recruiter", "start": "3:00 PM", "duration": "30 min"})
        self.t.execute("log_outcome", {"event": "Email recruiter", "status": "skipped"})
        out = self.t.execute("update_event", {"event": "Email recruiter", "to_day": "tomorrow", "start": "9:00 AM"})
        self.assertIn("today's record stays", out)
        today = next(s for s in self.sched.build_day_plan(DAY)[0].spans if s.title == "Email recruiter")
        tomorrow = next(s for s in self.sched.build_day_plan(DAY + timedelta(days=1))[0].spans if s.title == "Email recruiter")
        self.assertEqual((today.status, tomorrow.status), (SpanStatus.SKIPPED, SpanStatus.PLANNED))

    def test_list_another_day_and_weekend_has_no_work(self) -> None:
        saturday = self.t.execute("list_schedule", {"day": "Saturday"})
        self.assertIn("[Saturday, October 17]", saturday)
        self.assertNotIn("Work", saturday)
        self.assertIn("Work", self.t.execute("list_schedule", {"day": "Monday"}))

    def test_alongside_allows_concurrent_activities(self) -> None:
        self.t.execute("add_event", {"title": "Dinner", "start": "6:30 PM", "duration": "1 hour"})
        out = self.t.execute("add_event", {"title": "Audiobook", "start": "6:30 PM", "duration": "1 hour"})
        self.assertIn("alongside Dinner", out)  # items share time without being told (10-26)
        self.assertEqual(self.t.day_plan.display_title(self.t.day_plan.block_at(at("18:45"))), "Dinner + Audiobook")


class TestMemoryProfileHistory(ToolsTestCase):

    def test_remember_and_forget(self) -> None:
        out = self.t.execute("remember", {"note": "Probation items are maybes; don't push them"})
        note_id = out.split("note id ")[1].rstrip(").")
        self.assertEqual(len(self.services.memory.notes()), 1)
        self.t.execute("forget", {"note_id": note_id})
        self.assertEqual(self.services.memory.notes(), [])

    def test_profile_append_replace_and_markdown_roundtrip(self) -> None:
        self.t.execute("update_profile", {"section": "near-term", "text": "Finish Natalie's piece"})
        self.t.execute("update_profile", {"section": "Habits & routines", "text": "Cooks dinner around 6", "replace": True})
        md = self.services.profile.as_markdown()
        self.assertIn("## Near-term goals\n- Finish Natalie's piece", md)
        self.services.profile.save_markdown(md.replace("Finish Natalie's piece", "Finish Natalie's piece by Dec 1"))
        self.assertIn("by Dec 1", self.services.profile.sections()["Near-term goals"])
        self.assertTrue(self.t.execute("update_profile", {"section": "Hobbies?", "text": "x"}).startswith(NOT_DONE))

    def test_history_search_and_read(self) -> None:
        store = self.services.conversations
        old = store.create(DAY - timedelta(days=2))
        old.messages = [{"role": "user", "content": "I want to start exercising in the mornings"},
                        {"role": "assistant", "content": "Let's try a 30 minute walk at 7:30."}]
        store.save(old)
        self.assertIn("exercising", self.t.execute("search_history", {"query": "exercise mornings exercising"}))
        listing = self.t.execute("list_conversations", {})
        self.assertIn(old.id, listing)
        self.assertIn("30 minute walk", self.t.execute("read_conversation", {"conversation_id": old.id}))

    def test_backlog_search_marks_someday(self) -> None:
        out = self.t.execute("search_backlog", {"query": "brad weekend"})
        self.assertIn("someday/maybe", out)
        self.assertEqual(rank_tasks(TASKS, "natalie bridge")[0].id, "t_bridge")


class TestStatistics(unittest.TestCase):

    def spans(self):
        out = []
        for i in range(4):  # composing reliably takes 1.5x the plan
            start = at("17:00") - timedelta(days=i + 1)
            s = EventSpan(f"c{i}", "Composing", start, start + timedelta(hours=1), SpanCategory.ACTIVITY,
                          status=SpanStatus.DONE, actual_start=start, actual_end=start + timedelta(minutes=90),
                          time_source="reported")
            out.append(s)
        for i in range(3):  # mornings get skipped
            start = at("08:00") - timedelta(days=i + 1)
            out.append(EventSpan(f"m{i}", "Gym", start, start + timedelta(hours=1), SpanCategory.ACTIVITY,
                                 status=SpanStatus.SKIPPED))
        return out

    def test_facts(self) -> None:
        facts = fact_lines(self.spans(), at("12:00"), deferred=[("Compose the bridge section", 3)])
        text = "\n".join(facts)
        self.assertIn("4 were done", text)
        self.assertIn("'composing' usually takes about 1.5x the planned time", text)
        self.assertIn("evening get done 100%", text)
        self.assertIn("'Compose the bridge section' has been put off 3 times", text)

    def test_estimated_times_do_not_feed_durations(self) -> None:
        spans = self.spans()
        for s in spans:
            s.time_source = "estimated"
        self.assertNotIn("1.5x", "\n".join(fact_lines(spans, at("12:00"))))

    def test_unreviewed_and_keys(self) -> None:
        spans = self.spans() + [EventSpan("p", "Taxes", at("09:00"), at("10:00"), SpanCategory.TASK)]
        self.assertEqual([s.id for s in unreviewed_spans(spans, at("12:00"), at("07:00"))], ["p"])
        self.assertEqual(activity_key("Composing (Natalie piece)"), "composing natalie")


class TestStores(unittest.TestCase):

    def test_conversations_per_day(self) -> None:
        db = make_scheduler().state_db
        store = ConversationStore(db)
        self.assertIsNone(store.latest_for_day(DAY))
        a = store.create(DAY)
        a.messages = [{"role": "user", "content": "first conversation of the day, about dinner plans"}]
        store.save(a)
        b = store.create(DAY)
        self.assertEqual(store.latest_for_day(DAY).id, b.id)
        self.assertEqual([c.id for c in store.for_day(DAY)], [a.id, b.id])
        self.assertEqual(store.get(a.id).title, "first conversation of the day, about dinner plans")

    def test_energy_and_memory(self) -> None:
        db = make_scheduler().state_db
        EnergyLog(db).record(9)
        self.assertEqual(EnergyLog(db).entries()[0]["level"], 5)
        m = MemoryStore(db)
        note = m.add("x")
        self.assertTrue(m.update(note.id, "y"))
        self.assertEqual(m.notes()[0].text, "y")
        self.assertTrue(ProfileStore(db).is_empty())



class TestPlanningFromHistory(ToolsTestCase):

    def _history(self, title, minutes_each, days):
        for d in range(1, days + 1):
            start = at("10:00", -d)
            span = EventSpan(f"h{title}{d}", title, start, start + timedelta(minutes=60), SpanCategory.ACTIVITY)
            span.status, span.time_source = SpanStatus.DONE, "reported"
            span.actual_start, span.actual_end = start, start + timedelta(minutes=minutes_each)
            self.sched.state_db.upsert_span(span)

    def test_typical_duration_comes_from_reported_times(self):
        self._history("Composing", 95, 4)
        self.assertEqual(self.t.assistant.typical_duration("composing for Natalie"), 90)
        self.assertIsNone(self.t.assistant.typical_duration("Laundry"))
        self.assertIn("about 1h 30m", "\n".join(self.t.assistant.statistics_facts()))

    def test_capacity_heads_up_only_when_well_over(self):
        self._history("Reading", 120, 6)  # about two hours a day gets done
        self.t.execute("add_event", {"title": "Walk", "start": "6:30 PM", "duration": "1 hour"})
        self.assertEqual(self.t.assistant.capacity_note(), "")
        self.t.execute("add_event", {"title": "Painting", "start": "7:30 PM", "duration": "3 hours"})
        self.assertIn("about 4h of activities are planned", self.t.assistant.capacity_note())


if __name__ == "__main__":
    unittest.main()
