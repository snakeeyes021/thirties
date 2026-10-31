"""The plan change log: every change to an item, when, before and after, which tool and which message (plan_log)."""

import unittest
from datetime import datetime, timedelta, timezone

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core import plan_log
from thirties_core.astronomy import set_debug_time
from thirties_core.calendar_engine import CalendarEvent
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import PlanTools


def span(sid, title, start, end):
    return EventSpan(sid, title, at(start), at(end), SpanCategory.TASK)


class PlanLogCase(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("10:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.db = self.sched.state_db
        self.db.upsert_span(span("walk", "Walk", "11:00", "11:30"))
        self.db.upsert_span(span("taxes", "Taxes", "14:00", "15:00"))

    def tools(self):
        plan, pending = self.sched.build_day_plan(DAY)
        return PlanTools(plan, pending, self.sched)

    def rows(self, sid):
        return plan_log.changes_for(self.db, sid)


class TestRecording(PlanLogCase):
    def test_a_move_by_a_message_records_before_after_tool_and_message(self):
        tools = self.tools()
        tools.user_message = "move my walk to noon"
        tools.execute("update_event", {"event": "Walk", "start": "12:00 PM"})
        row = self.rows("walk")[-1]
        self.assertEqual(row["change"], "moved")
        self.assertEqual(row["tool"], "update_event")
        self.assertEqual(row["message"], "move my walk to noon")
        self.assertEqual((row["before"]["start_dt"][11:16], row["after"]["start_dt"][11:16]), ("11:00", "12:00"))
        self.assertIn('the user\'s message "move my walk to noon" (update_event)', plan_log.cause_text(row))

    def test_a_resize_and_a_tick_on_the_day_page(self):
        tools = self.tools()
        with plan_log.caused_by("Day page"):
            tools.execute("update_event", {"event": "Taxes", "end": "3:30 PM"})
            tools.execute("log_outcome", {"event": "Taxes", "status": "skipped"})
        resized, skipped = self.rows("taxes")[-2:]
        self.assertEqual(resized["change"], "resized")
        self.assertEqual(plan_log.cause_text(resized), "Day page drag")
        self.assertIn("marked skipped", skipped["change"])
        self.assertEqual(plan_log.cause_text(skipped), "Day page tick")

    def test_no_message_and_no_client_is_background(self):
        stored = self.db.get_spans_between(at("13:00"), at("16:00"))[0]
        stored.status = SpanStatus.SKIPPED
        self.db.upsert_span(stored)
        row = self.rows("taxes")[-1]
        self.assertEqual(row["change"], "marked skipped")
        self.assertTrue(plan_log.cause_text(row).startswith("background"))

    def test_an_unchanged_save_writes_nothing(self):
        before = len(self.rows("walk"))
        self.db.upsert_span(span("walk", "Walk", "11:00", "11:30"))
        self.assertEqual(len(self.rows("walk")), before)

    def test_remove_and_the_outermost_tool_is_named_first(self):
        tools = self.tools()
        with plan_log.tool_call("redraw", "just figure it out"):
            tools.execute("remove_event", {"event": "Walk"})
        row = self.rows("walk")[-1]
        self.assertEqual(row["change"], "removed")
        self.assertEqual(row["tool"], "redraw > remove_event")
        self.assertEqual(row["message"], "just figure it out")

    def test_the_turns_message_is_the_cause_through_the_conversation(self):
        plan, pending = self.sched.build_day_plan(DAY)
        engine = MockInferenceEngine([reply('Moved.\nCALL update_event {"event": "Walk", "start": "1:00 PM"}'),
                                      reply("Moved.")])
        manager = ConversationManager(plan, [], pending, self.sched, inference_engine=engine, active_notes=[])
        manager.send_user_message("walk at 1 instead")
        row = self.rows("walk")[-1]
        self.assertEqual(row["change"], "moved")
        self.assertIn("walk at 1 instead", row["message"])
        out = manager.execute_tool("item_history", {"item": "Walk"})
        self.assertIn("Walk moved (11:00 AM – 11:30 AM → 1:00 PM – 1:30 PM)", out)
        self.assertIn("walk at 1 instead", out)

    def test_a_synced_change_is_from_another_device(self):
        record = {"id": "walk", "data": span("walk", "Walk", "16:00", "16:30").to_dict(),
                  "updated_at": "2999-01-01T00:00:00+00:00", "deleted": False}
        self.assertTrue(self.db.merge_span_record(record))
        row = self.rows("walk")[-1]
        self.assertEqual((row["change"], plan_log.cause_text(row)), ("moved", "another device"))

    def test_work_and_sleep_set_up_for_the_day(self):
        self.tools()
        rows = [r for r in plan_log.changes_for(self.db, f"routine-{DAY.isoformat()}-work")]
        self.assertEqual([(r["change"], r["via"]) for r in rows], [("added", "day set-up")])

    def test_a_calendar_events_outcome_is_logged(self):
        ev = CalendarEvent(id="dentist", summary="Dentist", start_dt=at("15:00"), end_dt=at("16:00"))
        sched = make_scheduler([ev])
        plan, pending = sched.build_day_plan(DAY)
        tools = PlanTools(plan, pending, sched)
        tools.execute("log_outcome", {"event": "Dentist", "status": "done"})
        row = plan_log.changes_for(sched.state_db, "cal:dentist")[-1]
        self.assertIn("marked done", row["change"])
        self.assertEqual(row["tool"], "log_outcome")


class TestReading(PlanLogCase):
    def test_item_history_says_why_it_was_skipped(self):
        tools = self.tools()
        tools.user_message = "skip the taxes, not today"
        tools.execute("log_outcome", {"event": "Taxes", "status": "skipped"})
        tools.user_message = None
        out = tools.execute("item_history", {"item": "Taxes"})
        self.assertIn("Taxes marked skipped (planned → skipped)", out)
        self.assertIn('"skip the taxes, not today" (log_outcome)', out)
        self.assertNotIn("say you don't know", out)

    def test_old_rows_are_pruned_after_sixty_days(self):
        old = (datetime.now(timezone.utc) - timedelta(days=plan_log.KEEP_DAYS + 1)).isoformat()
        with self.db._get_connection() as conn:
            conn.execute("UPDATE plan_changes SET at = ? WHERE span_id = 'walk'", (old,))
        plan_log._ready.discard(str(self.db.db_path))  # a new process
        self.assertEqual(self.rows("walk"), [])
        self.assertTrue(self.rows("taxes"))

    def test_found_by_title_on_the_day_when_the_id_changed(self):
        self.db.delete_span("walk")
        self.db.upsert_span(span("walk2", "Walk", "12:00", "12:30"))
        changes = [r["change"] for r in plan_log.changes_for(self.db, "walk2", "Walk", DAY.isoformat())]
        self.assertEqual(changes, ["added", "removed", "added"])


if __name__ == "__main__":
    unittest.main()
