"""Unit tests for thirties_core.joplin_engine."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from thirties_core.config import JoplinConfig, ThirtiesConfig
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.models import TaskItem


def create_mock_joplin_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE folders (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        title TEXT,
        deleted_time INTEGER DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE deleted_items (
        item_id TEXT PRIMARY KEY,
        item_type INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        title TEXT,
        body TEXT,
        is_todo INTEGER DEFAULT 0,
        todo_completed INTEGER DEFAULT 0,
        todo_due INTEGER DEFAULT 0,
        deleted_time INTEGER DEFAULT 0,
        is_conflict INTEGER DEFAULT 0,
        encryption_applied INTEGER DEFAULT 0,
        updated_time INTEGER DEFAULT 0
    );
    """)

    # Folders
    cur.execute("INSERT INTO folders VALUES ('f_tasks', '', '1. Tasks', 0)")
    cur.execute("INSERT INTO folders VALUES ('f_dev', '', '2. Dev', 0)")
    cur.execute("INSERT INTO folders VALUES ('f_subdev', 'f_dev', 'Thirties', 0)")
    cur.execute("INSERT INTO folders VALUES ('f_archive', '', 'Archive', 0)")
    cur.execute("INSERT INTO folders VALUES ('f_arch_sub', 'f_archive', 'Old Project', 0)")
    cur.execute("INSERT INTO folders VALUES ('f_unallowed', '', 'Random Vault', 0)")

    # Notes
    # 1. Active Markdown note with checkboxes in "1. Tasks"
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_1', 'f_tasks', 'Weekly Sprint',
        '# Sprints\n- [ ] Ship MVP models\n- [ ] Write Joplin integration\n- [x] Initial design doc\nSome extra text',
        0, 0, 0, 0, 0, 0, 1000
    )
    """)

    # 2. Standalone Todo note (uncompleted) in "2. Dev > Thirties"
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_2', 'f_subdev', 'Fix CI builds',
        'Check Flatpak runner',
        1, 0, 0, 0, 0, 0, 2000
    )
    """)

    # 3. Completed Todo note (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_3', 'f_tasks', 'Old Finished Todo',
        '',
        1, 1600000000, 0, 0, 0, 0, 3000
    )
    """)

    # 4. Soft-deleted note (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_4', 'f_tasks', 'Deleted Note',
        '- [ ] Should not appear',
        0, 0, 0, 1600000000, 0, 0, 4000
    )
    """)

    # 5. Conflict note (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_5', 'f_tasks', 'Conflict Note',
        '- [ ] Conflict task',
        0, 0, 0, 0, 1, 0, 5000
    )
    """)

    # 6. Encrypted note (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_6', 'f_tasks', 'Encrypted Note',
        'Ciphertext',
        0, 0, 0, 0, 0, 1, 6000
    )
    """)

    # 7. Note in Archive folder (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_7', 'f_arch_sub', 'Archived Note',
        '- [ ] Ancient Task',
        0, 0, 0, 0, 0, 0, 7000
    )
    """)

    # 8. Note in non-whitelisted folder (should be ignored)
    cur.execute("""
    INSERT INTO notes VALUES (
        'note_8', 'f_unallowed', 'Secret Note',
        '- [ ] Secret Task',
        0, 0, 0, 0, 0, 0, 8000
    )
    """)

    conn.commit()
    conn.close()


class TestJoplinEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_joplin.sqlite"
        create_mock_joplin_db(self.db_path)

        cfg = ThirtiesConfig()
        cfg.joplin.db_path = str(self.db_path)
        cfg.joplin.api_token = "test_token"
        self.engine = JoplinEngine(cfg)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fetch_tasks_whitelisting_and_filtering(self) -> None:
        tasks = self.engine.fetch_tasks(self.db_path)

        # Expected tasks:
        # 1. "Ship MVP models" (checkbox in note_1)
        # 2. "Write Joplin integration" (checkbox in note_1)
        # 3. "Fix CI builds" (todo note_2)
        titles = [t.title for t in tasks]
        self.assertEqual(len(tasks), 3)
        self.assertIn("Ship MVP models", titles)
        self.assertIn("Write Joplin integration", titles)
        self.assertIn("Fix CI builds", titles)

        # Confirm excluded items are completely absent
        self.assertNotIn("Ancient Task", titles)
        self.assertNotIn("Secret Task", titles)
        self.assertNotIn("Old Finished Todo", titles)
        self.assertNotIn("Conflict task", titles)

        # Inspect checkbox metadata
        checkbox_task = next(t for t in tasks if t.title == "Ship MVP models")
        self.assertTrue(checkbox_task.is_checkbox_item)
        self.assertEqual(checkbox_task.parent_note_id, "note_1")
        self.assertEqual(checkbox_task.parent_note_title, "Weekly Sprint")
        self.assertEqual(checkbox_task.source_notebook, "1. Tasks")

        # Inspect todo note metadata
        todo_task = next(t for t in tasks if t.title == "Fix CI builds")
        self.assertFalse(todo_task.is_checkbox_item)
        self.assertEqual(todo_task.id, "note_2")
        self.assertEqual(todo_task.source_notebook, "2. Dev > Thirties")

    def test_complete_checkbox_task_success(self) -> None:
        task = TaskItem(
            id="synth_123",
            source_notebook="1. Tasks",
            title="Ship MVP models",
            is_checkbox_item=True,
            parent_note_id="note_1",
            parent_note_title="Weekly Sprint",
        )

        mock_body = "# Sprints\n- [ ] Ship MVP models\n- [ ] Write Joplin integration"
        with patch.object(self.engine.api_client, "get_note", return_value={"body": mock_body, "updated_time": 1000}), \
             patch.object(self.engine.api_client, "update_note", return_value=True) as mock_update:
            
            success, msg = self.engine.complete_task(task)
            self.assertTrue(success)
            self.assertIn("marked complete", msg)

            # Verify targeted replacement: only "Ship MVP models" became "- [x]"
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            self.assertEqual(call_args[0], "note_1")
            updated_body = call_args[1]["body"]
            self.assertIn("- [x] Ship MVP models", updated_body)
            self.assertIn("- [ ] Write Joplin integration", updated_body)

    def test_complete_checkbox_conflict_fallback(self) -> None:
        task = TaskItem(
            id="synth_123",
            source_notebook="1. Tasks",
            title="Ship MVP models",
            is_checkbox_item=True,
            parent_note_id="note_1",
            parent_note_title="Weekly Sprint",
        )

        # Simulate body changed externally in Joplin (e.g. line removed or renamed)
        externally_modified_body = "# Sprints\n- [ ] Renamed task elsewhere"
        with patch.object(self.engine.api_client, "get_note", return_value={"body": externally_modified_body}), \
             patch.object(self.engine.api_client, "update_note") as mock_update:
            
            success, msg = self.engine.complete_task(task)
            # Must fail safely without modifying note
            self.assertFalse(success)
            self.assertIn("Conflict", msg)
            mock_update.assert_not_called()

    def test_complete_todo_note(self) -> None:
        task = TaskItem(
            id="note_2",
            source_notebook="2. Dev > Thirties",
            title="Fix CI builds",
            is_checkbox_item=False,
        )

        with patch.object(self.engine.api_client, "update_note", return_value=True) as mock_update:
            success, msg = self.engine.complete_task(task)
            self.assertTrue(success)
            self.assertIn("marked complete", msg)
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            self.assertEqual(call_args[0], "note_2")
            self.assertIn("todo_completed", call_args[1])


if __name__ == "__main__":
    unittest.main()
