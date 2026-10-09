"""Joplin ingestion and synchronization engine.

Performs read-only SQLite extraction directly from Joplin's local database
and executes mutations exclusively through Joplin's Local Data API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.config import JoplinConfig, ThirtiesConfig
from thirties_core.models import TaskItem

logger = logging.getLogger(__name__)

CHECKBOX_REGEX = re.compile(r"^[ \t]*-\s*\[ \]\s+(.+)$")

JOPLIN_EXTRACTION_QUERY = """
WITH RECURSIVE live_folders AS (
    SELECT id, parent_id, title 
    FROM folders 
    WHERE deleted_time = 0 
      AND id NOT IN (SELECT item_id FROM deleted_items WHERE item_type = 2)
),
archive_ids AS (
    SELECT id FROM live_folders WHERE title = 'Archive'
    UNION ALL
    SELECT f.id FROM live_folders f JOIN archive_ids a ON f.parent_id = a.id
),
folder_paths AS (
    SELECT id, title, title AS full_path 
    FROM live_folders 
    WHERE (parent_id = '' OR parent_id IS NULL) 
      AND id NOT IN (SELECT id FROM archive_ids)
    UNION ALL
    SELECT f.id, f.title, fp.full_path || ' > ' || f.title 
    FROM live_folders f 
    JOIN folder_paths fp ON f.parent_id = fp.id 
    WHERE f.id NOT IN (SELECT id FROM archive_ids)
)
SELECT 
    n.id,
    fp.full_path, 
    n.title, 
    n.body,
    n.is_todo,
    n.todo_completed,
    n.todo_due
FROM notes n 
JOIN folder_paths fp ON n.parent_id = fp.id 
WHERE n.deleted_time = 0 
  AND n.id NOT IN (SELECT item_id FROM deleted_items WHERE item_type = 1) 
  AND n.is_conflict = 0 
  AND n.encryption_applied = 0 
ORDER BY fp.full_path, n.title;
"""


class JoplinAPIClient:
    """Client for Joplin Local Data API (http://127.0.0.1:41184)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 41184, token: str = "") -> None:
        self.base_url = f"http://{host}:{port}"
        self.token = token

    def _build_url(self, endpoint: str, query_params: Optional[Dict[str, Any]] = None) -> str:
        params = {"token": self.token}
        if query_params:
            params.update(query_params)
        return f"{self.base_url}{endpoint}?{urllib.parse.urlencode(params)}"

    def ping(self) -> bool:
        """Check if Joplin Local API is running and reachable."""
        url = self._build_url("/ping")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Thirties/0.1"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200 and resp.read().decode("utf-8").strip() == "JoplinClipperServer"
        except Exception:
            return False

    def get_note(self, note_id: str, fields: str = "id,title,body,updated_time") -> Optional[Dict[str, Any]]:
        """Fetch a note's current live state."""
        url = self._build_url(f"/notes/{note_id}", {"fields": fields})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Thirties/0.1"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to fetch Joplin note %s: %s", note_id, e)
        return None

    def update_note(self, note_id: str, data: Dict[str, Any]) -> bool:
        """Update note attributes via PUT /notes/{id}."""
        url = self._build_url(f"/notes/{note_id}")
        payload = json.dumps(data).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Thirties/0.1"},
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error("Failed to update Joplin note %s: %s", note_id, e)
            return False


class JoplinEngine:
    """Orchestrates read-only SQLite extraction and write-back mutations."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.joplin_cfg: JoplinConfig = self.config.joplin
        self.api_client = JoplinAPIClient(
            host=self.joplin_cfg.api_host,
            port=self.joplin_cfg.api_port,
            token=self.joplin_cfg.api_token,
        )

    def _open_db(self, db_path: Path) -> sqlite3.Connection:
        """Open SQLite database in immutable read-only mode with timeout."""
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_tasks(self, db_path: Optional[Path] = None) -> List[TaskItem]:
        """Extract candidate tasks from Joplin SQLite database."""
        path = db_path or self.config.resolve_joplin_db_path()
        if not path or not path.is_file():
            logger.info("Joplin database not found at %s", path)
            return []

        tasks: List[TaskItem] = []
        try:
            with self._open_db(path) as conn:
                cursor = conn.cursor()
                cursor.execute(JOPLIN_EXTRACTION_QUERY)
                rows = cursor.fetchall()
        except sqlite3.Error as e:
            logger.error("Failed reading Joplin SQLite: %s", e)
            return []

        allowed_notebooks = self.joplin_cfg.allowed_notebooks
        whitelist_notes = self.joplin_cfg.whitelist_notes

        for row in rows:
            note_id = row["id"]
            full_path = row["full_path"]
            title = row["title"]
            body = row["body"] or ""
            is_todo = bool(row["is_todo"])
            todo_completed = row["todo_completed"]

            # Filter by allowed notebooks whitelist
            is_allowed_notebook = any(
                full_path == nb or full_path.startswith(f"{nb} >")
                for nb in allowed_notebooks
            )
            is_whitelisted_note = title in whitelist_notes

            if not (is_allowed_notebook or is_whitelisted_note):
                continue

            if is_todo:
                if todo_completed != 0:
                    continue  # already completed
                tasks.append(
                    TaskItem(
                        id=note_id,
                        source_notebook=full_path,
                        title=title,
                        description="",
                        is_checkbox_item=False,
                        parent_note_title=None,
                        parent_note_id=None,
                    )
                )
            else:
                # Parse markdown lines for "- [ ]" checkboxes
                for line_idx, line in enumerate(body.splitlines(), start=1):
                    match = CHECKBOX_REGEX.match(line)
                    if match:
                        task_text = match.group(1).strip()
                        synthetic_seed = f"{note_id}_{line_idx}_{task_text}".encode("utf-8")
                        synthetic_id = hashlib.sha256(synthetic_seed).hexdigest()[:16]
                        tasks.append(
                            TaskItem(
                                id=synthetic_id,
                                source_notebook=full_path,
                                title=task_text,
                                description="",
                                is_checkbox_item=True,
                                parent_note_title=title,
                                parent_note_id=note_id,
                            )
                        )

        return tasks

    def complete_task(self, task: TaskItem) -> Tuple[bool, str]:
        """Mark a task complete in Joplin.
        
        For checkboxes: Read-Modify-Write pattern on live note body via API.
        For todo notes: Sets todo_completed via API.
        """
        if not self.joplin_cfg.api_token:
            return False, "Joplin API token is not configured in config.toml"

        if task.is_checkbox_item:
            parent_id = task.parent_note_id
            if not parent_id:
                return False, f"Missing parent note ID for checkbox task {task.id}"

            # 1. Grab live note body
            live_note = self.api_client.get_note(parent_id, fields="id,title,body,updated_time")
            if not live_note or "body" not in live_note:
                return False, f"Could not retrieve note {parent_id} from Joplin Local API"

            body = live_note["body"]

            # 2. Targeted regex string replacement with conflict fallback
            target_pattern = re.compile(
                r"^[ \t]*-\s*\[ \]\s*" + re.escape(task.title) + r"$",
                flags=re.MULTILINE,
            )

            if not target_pattern.search(body):
                logger.warning(
                    "Task '%s' was not found in note '%s'. Note may have been modified externally.",
                    task.title,
                    parent_id,
                )
                return False, f"Conflict: Task '{task.title}' was modified externally in Joplin"

            # 3. Replace only the single matching task checkbox
            updated_body = target_pattern.sub(f"- [x] {task.title}", body, count=1)

            # 4. PUT updated body
            success = self.api_client.update_note(parent_id, {"body": updated_body})
            if success:
                return True, f"Checkbox task '{task.title}' marked complete"
            return False, "Failed to update note body via Joplin Local API"

        else:
            # Standard todo note completion
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            success = self.api_client.update_note(task.id, {"todo_completed": now_ms})
            if success:
                return True, f"Todo note '{task.title}' marked complete"
            return False, "Failed to complete todo note via Joplin Local API"
