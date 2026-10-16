"""Unit tests for thirties_core.sync_engine."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler, occupant
from tests.test_scheduler import _legacy_plan_dict
from thirties_core.config import ThirtiesConfig
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.plan_tools import PlanTools
from thirties_core.scheduler import DayMeta, StateDatabase, TaskHistoryRecord
from thirties_core.sync_engine import (
    LEGACY_SYNC_SCHEMA,
    SYNC_SCHEMA,
    SyncEngine,
    decode_payload,
    encode_payload,
)


class TestSyncEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.sqlite"
        self.state_db = StateDatabase(self.db_path)
        self.config = ThirtiesConfig()
        self.config.sync.device_id = "test-device-1"
        self.config.sync.mode, self.config.sync.enabled = "joplin", True

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_encode_and_decode_payload(self) -> None:
        sample_payload = {
            "$schema": SYNC_SCHEMA,
            "device_id": "test-device-1",
            "timestamp": "2026-10-10T12:00:00Z",
            "version": 1,
            "schedules": {"2026-10-10": {"plan_json": "{}", "is_finalized": True}},
            "analytics": {"task_history": {}},
        }
        encoded = encode_payload(sample_payload)
        self.assertIn("<!-- THIRTIES_SYNC_PAYLOAD_START -->", encoded)
        self.assertIn("<!-- THIRTIES_SYNC_PAYLOAD_END -->", encoded)

        decoded = decode_payload(encoded)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["$schema"], SYNC_SCHEMA)
        self.assertEqual(decoded["device_id"], "test-device-1")
        self.assertIn("2026-10-10", decoded["schedules"])

    def test_decode_payload_fallback_code_fence(self) -> None:
        body = """# Notes
Here is the sync data:
```json
{"$schema": "thirties.sync.v1", "device_id": "fallback-device"}
```
"""
        decoded = decode_payload(body)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["device_id"], "fallback-device")

    def test_export_sync_payload(self) -> None:
        self.state_db.record_task_allocation("task-abc", date(2026, 10, 10), count=2)
        self.state_db.record_task_deferred("task-abc")
        self.state_db.upsert_span(EventSpan("g", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY))
        self.state_db.delete_span("g")
        self.state_db.save_day_meta(DayMeta(DAY.isoformat(), is_finalized=True))
        self.state_db.set_event_decision("e1", "attend")

        payload = SyncEngine(config=self.config, state_db=self.state_db).export_sync_payload()

        self.assertEqual(payload["$schema"], SYNC_SCHEMA)
        self.assertEqual(payload["device_id"], "test-device-1")
        self.assertEqual([(r["id"], r["deleted"]) for r in payload["spans"]], [("g", True)])
        self.assertTrue(payload["day_meta"][0]["is_finalized"])
        self.assertEqual(payload["event_decisions"][0]["event_id"], "e1")
        self.assertEqual(payload["analytics"]["task_history"]["task-abc"]["deferred_count"], 1)

    def _sync_from(self, source_db: StateDatabase, target_db: StateDatabase) -> None:
        """Push from one device's database and pull into another's through a fake Joplin note."""
        note = {}
        joplin = MagicMock()
        joplin.upsert_sync_note.side_effect = lambda **kw: (note.update(body=kw["note_body"]), (True, "ok"))[1]
        joplin.get_sync_note_body.side_effect = lambda **kw: {"body": note.get("body", "")}
        SyncEngine(config=self.config, state_db=source_db, joplin_engine=joplin).push()
        ok, msg = SyncEngine(config=self.config, state_db=target_db, joplin_engine=joplin).pull()
        self.assertTrue(ok, msg)

    def test_two_devices_converge(self) -> None:
        laptop, desktop = make_scheduler(), make_scheduler()
        laptop_plan, _ = laptop.build_day_plan(DAY)
        desktop.build_day_plan(DAY)  # both devices materialized the same default day

        tools = PlanTools(laptop_plan, [], laptop)
        tools.execute("update_event", {"event": "Work", "end": "4:30 PM"})
        tools.execute("add_event", {"title": "Gym", "start": "5:00 PM", "duration": "1 hour"})
        self._sync_from(laptop.state_db, desktop.state_db)

        desktop_plan, _ = desktop.build_day_plan(DAY)
        self.assertEqual(occupant(desktop_plan, "16:00"), "Work")
        self.assertEqual(occupant(desktop_plan, "17:00"), "Gym")
        self.assertEqual(sum(s.category == SpanCategory.WORK for s in desktop_plan.spans), 1)

        # A deletion on the desktop reaches the laptop, and stale data can't bring it back
        PlanTools(desktop_plan, [], desktop).execute("remove_event", {"event": "Work"})
        self._sync_from(desktop.state_db, laptop.state_db)
        self.assertIsNone(occupant(laptop.build_day_plan(DAY)[0], "10:00"))
        self._sync_from(laptop.state_db, desktop.state_db)
        self.assertIsNone(occupant(desktop.build_day_plan(DAY)[0], "10:00"))

    def test_assistant_state_syncs(self) -> None:
        from thirties_core.memory import ConversationStore, MemoryStore, ProfileStore
        laptop, desktop = make_scheduler(), make_scheduler()
        conv = ConversationStore(laptop.state_db).create(DAY)
        conv.messages = [{"role": "user", "content": "plan my evening"}]
        ConversationStore(laptop.state_db).save(conv)
        ProfileStore(laptop.state_db).append("Near-term goals", "Finish Natalie's piece by December")
        MemoryStore(laptop.state_db).add("Probation items are maybes")
        self._sync_from(laptop.state_db, desktop.state_db)
        self.assertEqual(ConversationStore(desktop.state_db).latest_for_day(DAY).messages[0]["content"], "plan my evening")
        self.assertIn("Natalie", ProfileStore(desktop.state_db).sections()["Near-term goals"])
        self.assertEqual([n.text for n in MemoryStore(desktop.state_db).notes()], ["Probation items are maybes"])

    def test_day_off_beats_a_device_that_never_saw_the_day(self) -> None:
        laptop, desktop = make_scheduler(), make_scheduler()
        laptop_plan, _ = laptop.build_day_plan(DAY)
        PlanTools(laptop_plan, [], laptop).execute("remove_event", {"event": "Work"})
        desktop.build_day_plan(DAY)  # desktop materializes default work later in wall time
        self._sync_from(desktop.state_db, laptop.state_db)
        self.assertIsNone(occupant(laptop.build_day_plan(DAY)[0], "10:00"))

    def test_legacy_v1_payload_imports_untouched_days(self) -> None:
        payload = {
            "$schema": LEGACY_SYNC_SCHEMA, "device_id": "old-laptop", "timestamp": "2026-10-10T15:30:00Z",
            "schedules": {DAY.isoformat(): {"plan_json": json.dumps(_legacy_plan_dict()), "is_finalized": 1}},
        }
        joplin = MagicMock()
        joplin.get_sync_note_body.return_value = {"body": encode_payload(payload)}
        ok, _ = SyncEngine(config=self.config, state_db=self.state_db, joplin_engine=joplin).pull()
        self.assertTrue(ok)
        titles = {s.title for s in self.state_db.get_spans_between(at("07:00"), at("07:00", 1))}
        self.assertEqual(titles, {"Work", "Sleep", "Composing"})

    def test_push_sync(self) -> None:
        mock_joplin = MagicMock()
        mock_joplin.upsert_sync_note.return_value = (True, "Created sync note 'Thirties_State_Sync' (id: 123)")

        engine = SyncEngine(config=self.config, state_db=self.state_db, joplin_engine=mock_joplin)
        ok, msg = engine.push()

        self.assertTrue(ok)
        self.assertIn("Created sync note", msg)
        mock_joplin.upsert_sync_note.assert_called_once()
        args, kwargs = mock_joplin.upsert_sync_note.call_args
        self.assertEqual(kwargs["notebook_name"], "Archive")
        self.assertEqual(kwargs["subfolder_name"], "Thirties_System")
        self.assertEqual(kwargs["note_title"], "Thirties_State_Sync")

        # Verify last_push_timestamp recorded
        last_push = self.state_db.get_sync_meta("last_push_timestamp")
        self.assertIsNotNone(last_push)

    def test_pull_and_merge_sync(self) -> None:
        # Local state: task-1 with deferred_count=1, allocated_thirties=2
        self.state_db.upsert_task_history(
            TaskHistoryRecord(
                task_id="task-1",
                first_scheduled_date="2026-10-08",
                last_scheduled_date="2026-10-09",
                deferred_count=1,
                allocated_thirties=2,
            )
        )

        # Remote payload from Device B:
        # task-1 has deferred_count=3, allocated_thirties=4 (higher)
        # task-2 is a brand new task
        # 2026-10-11 is a new schedule
        remote_payload = {
            "$schema": SYNC_SCHEMA,
            "device_id": "device-laptop-b",
            "timestamp": "2026-10-10T15:30:00Z",
            "version": 2,
            "spans": [{
                "id": "g",
                "data": EventSpan("g", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY).to_dict(),
                "updated_at": "2026-10-10T15:00:00+00:00",
                "deleted": False,
            }],
            "day_meta": [{"plan_date": DAY.isoformat(), "is_finalized": True, "notes": "",
                          "routines_applied": True, "updated_at": "2026-10-10T15:00:00+00:00"}],
            "analytics": {
                "task_history": {
                    "task-1": {
                        "task_id": "task-1",
                        "first_scheduled_date": "2026-10-07",  # earlier
                        "last_scheduled_date": "2026-10-10",   # later
                        "deferred_count": 3,                   # higher
                        "allocated_thirties": 4,               # higher
                        "completed_date": None,
                    },
                    "task-2": {
                        "task_id": "task-2",
                        "first_scheduled_date": "2026-10-10",
                        "last_scheduled_date": "2026-10-10",
                        "deferred_count": 0,
                        "allocated_thirties": 1,
                        "completed_date": "2026-10-10",
                    },
                }
            },
            "conversations": {
                "recent_summaries": [
                    {"date": "2026-10-10", "summary": "User completed task-2."}
                ]
            },
        }

        mock_joplin = MagicMock()
        mock_joplin.get_sync_note_body.return_value = {
            "id": "note_sync_123",
            "title": "Thirties_State_Sync",
            "body": encode_payload(remote_payload),
            "updated_time": 1791671000000,
        }

        engine = SyncEngine(config=self.config, state_db=self.state_db, joplin_engine=mock_joplin)
        ok, msg = engine.pull()

        self.assertTrue(ok)
        self.assertIn("device-laptop-b", msg)

        # Verify task-1 merged with union max
        t1 = self.state_db.get_task_history("task-1")
        self.assertIsNotNone(t1)
        self.assertEqual(t1.deferred_count, 3)
        self.assertEqual(t1.allocated_thirties, 4)
        self.assertEqual(t1.first_scheduled_date, "2026-10-07")
        self.assertEqual(t1.last_scheduled_date, "2026-10-10")

        # Verify task-2 added
        t2 = self.state_db.get_task_history("task-2")
        self.assertIsNotNone(t2)
        self.assertEqual(t2.completed_date, "2026-10-10")

        # Verify schedule state merged
        self.assertEqual([sp.title for sp in self.state_db.get_spans_between(at("17:00"), at("18:00"))], ["Gym"])
        self.assertTrue(self.state_db.get_day_meta(DAY).is_finalized)

        # Verify conversation summaries merged
        summaries_json = self.state_db.get_sync_meta("recent_conversation_summaries")
        self.assertIsNotNone(summaries_json)
        self.assertIn("User completed task-2", summaries_json)



class TestFolderSync(unittest.TestCase):

    def _device(self, name, folder):
        from thirties_core.scheduler import StateDatabase
        cfg = ThirtiesConfig()
        cfg.sync.mode, cfg.sync.enabled, cfg.sync.folder, cfg.sync.device_id = "folder", True, folder, name
        db = StateDatabase(Path(tempfile.mkdtemp()) / "state.sqlite")
        return SyncEngine(config=cfg, state_db=db, joplin_engine=MagicMock()), db

    def test_two_computers_share_a_folder_without_clobbering(self):
        from datetime import datetime, timezone
        from thirties_core.models import EventSpan, SpanCategory
        folder = tempfile.mkdtemp()
        laptop, ldb = self._device("laptop", folder)
        desk, ddb = self._device("desk", folder)
        t = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)
        ldb.upsert_span(EventSpan("a", "Dinner", t, t.replace(hour=19), SpanCategory.ACTIVITY))
        ddb.upsert_span(EventSpan("b", "Gym", t.replace(hour=20), t.replace(hour=21), SpanCategory.ACTIVITY))
        self.assertTrue(laptop.sync()[0])
        self.assertTrue(desk.sync()[0])
        self.assertTrue(laptop.sync()[0])
        for db in (ldb, ddb):
            self.assertEqual(sorted(r["id"] for r in db.get_span_records() if not r.get("deleted")), ["a", "b"])
        self.assertEqual(sorted(p.name for p in (Path(folder) / "Thirties sync").glob("*.json")),
                         ["desk.json", "laptop.json"])


if __name__ == "__main__":
    unittest.main()
