"""The connector interfaces (AGENTS.md anti-pattern 1): Joplin and EDS are implementations, not the architecture."""

import json
import tempfile
import unittest
from pathlib import Path

from thirties_core.calendar_engine import EDSCalendarEngine, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.connectors import CalendarSource, SyncTransport, TaskSource, calendars_of
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.sync_engine import SYNC_SCHEMA, FolderTransport, JoplinNoteTransport


class ConnectorsTest(unittest.TestCase):
    def test_the_first_connectors_implement_the_interfaces(self):
        self.assertIsInstance(JoplinEngine(ThirtiesConfig()), TaskSource)
        self.assertIsInstance(EDSCalendarEngine(ThirtiesConfig()), CalendarSource)
        self.assertIsInstance(MockCalendarEngine(), CalendarSource)
        self.assertIsInstance(FolderTransport("", lambda: "me"), SyncTransport)
        self.assertIsInstance(JoplinNoteTransport(JoplinEngine(ThirtiesConfig()), ThirtiesConfig().sync), SyncTransport)

    def test_any_task_source_feeds_the_backlog(self):
        from thirties_core.backlog import load_backlog
        from thirties_core.models import TaskItem

        class Todoist:  # a second task source needs only the three methods
            def fetch_tasks(self):
                return [TaskItem("todo:1", "Inbox", "Call the bank", is_checkbox_item=True)]

            def fetch_active_notes(self):
                return [{"id": "inbox", "title": "Inbox", "body": "- [ ] Call the bank"}]

            def list_notebooks(self):
                return ["Inbox"]
        tasks, notes = load_backlog(ThirtiesConfig(), Todoist(), media=[])
        self.assertEqual(([t.title for t in tasks], [n["id"] for n in notes]), (["Call the bank"], ["inbox"]))

    def test_calendars_of_a_source_without_calendars(self):
        self.assertEqual(calendars_of(MockCalendarEngine()), [])

    def test_folder_transport_reads_only_the_other_devices(self):
        folder = tempfile.mkdtemp(prefix="thirties_sync_")
        mine, theirs = FolderTransport(folder, lambda: "laptop"), FolderTransport(folder, lambda: "desk")
        payload = {"$schema": SYNC_SCHEMA, "device_id": "laptop", "timestamp": "t"}
        self.assertTrue(mine.push(payload)[0])
        (Path(folder) / "Thirties sync" / "junk.json").write_text(json.dumps({"$schema": "other"}))
        self.assertEqual(mine.pull()[2], [])  # its own file, and a file that isn't a payload
        ok, msg, payloads = theirs.pull()
        self.assertEqual((ok, [p["device_id"] for p in payloads]), (True, ["laptop"]))
        self.assertEqual(FolderTransport("", lambda: "x").pull(), (False, "No sync folder chosen.", []))


if __name__ == "__main__":
    unittest.main()
