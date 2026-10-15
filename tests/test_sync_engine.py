"""Unit tests for thirties_core.sync_engine."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from thirties_core.config import ThirtiesConfig
from thirties_core.scheduler import StateDatabase, TaskHistoryRecord
from thirties_core.sync_engine import (
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
        # Populate state db with task history and a day snapshot
        self.state_db.record_task_allocation("task-abc", date(2026, 10, 10), count=2)
        self.state_db.record_task_deferred("task-abc")
        self.state_db.save_raw_day_snapshot("2026-10-10", '{"target_date": "2026-10-10"}', is_finalized=1)

        engine = SyncEngine(config=self.config, state_db=self.state_db)
        payload = engine.export_sync_payload()

        self.assertEqual(payload["$schema"], SYNC_SCHEMA)
        self.assertEqual(payload["device_id"], "test-device-1")
        self.assertIn("2026-10-10", payload["schedules"])
        self.assertTrue(payload["schedules"]["2026-10-10"]["is_finalized"])
        self.assertIn("task-abc", payload["analytics"]["task_history"])
        self.assertEqual(payload["analytics"]["task_history"]["task-abc"]["deferred_count"], 1)

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
            "version": 1,
            "schedules": {
                "2026-10-11": {
                    "plan_json": '{"target_date": "2026-10-11", "blocks": []}',
                    "is_finalized": 1,
                }
            },
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

        # Verify schedule snapshot added
        snapshots = self.state_db.get_all_day_snapshots()
        dates = [s[0] for s in snapshots]
        self.assertIn("2026-10-11", dates)

        # Verify conversation summaries merged
        summaries_json = self.state_db.get_sync_meta("recent_conversation_summaries")
        self.assertIsNotNone(summaries_json)
        self.assertIn("User completed task-2", summaries_json)


if __name__ == "__main__":
    unittest.main()
