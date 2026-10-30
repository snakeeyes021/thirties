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

CHECKBOX_REGEX = re.compile(r"^([ \t]*)-\s*\[ \]\s+(.+)$")
DONE_CHECKBOX_REGEX = re.compile(r"^([ \t]*)-\s*\[[xX]\]\s+(.+)$")
HEADING_REGEX = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _indent_width(prefix: str) -> int:
    return len(prefix.replace("\t", "    "))


def item_id(note_id: str, text: str, occurrence: int = 0) -> str:
    """A checklist item's id: its note, its text, and which open copy of that text it is."""
    return hashlib.sha256(f"{note_id}|{text}|{occurrence}".encode("utf-8")).hexdigest()[:16]


def checklist_tasks(note_id: str, note_title: str, notebook: str, body: str,
                    someday_sections: List[str]) -> List[TaskItem]:
    """Open checklist items in a note body, with heading, nesting and someday context.

    Ids are stable across edits elsewhere in the note: they hash the note id, the
    item text and its occurrence number, not the line number.
    """
    tasks: List[TaskItem] = []
    someday = {s.strip().lower() for s in someday_sections}
    section = ""
    seen: Dict[str, int] = {}
    stack: List[Tuple[int, Optional[str], bool]] = []  # (indent, task id or None if done, someday)
    for line in body.splitlines():
        heading = HEADING_REGEX.match(line)
        if heading:
            section = heading.group(1).strip()
            stack = []
            continue
        open_match = CHECKBOX_REGEX.match(line)
        done_match = None if open_match else DONE_CHECKBOX_REGEX.match(line)
        match = open_match or done_match
        if not match:
            continue
        indent = _indent_width(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        is_someday = section.lower() in someday or (bool(stack) and stack[-1][2])
        if done_match:
            stack.append((indent, None, is_someday))
            continue
        text = open_match.group(2).strip()
        n = seen.get(text, 0)
        seen[text] = n + 1
        task_id = item_id(note_id, text, n)
        tasks.append(TaskItem(
            id=task_id,
            occurrence=n,
            source_notebook=notebook,
            title=text,
            is_checkbox_item=True,
            parent_note_title=note_title,
            parent_note_id=note_id,
            section=section,
            parent_task_id=parent_id,
            someday=is_someday,
        ))
        stack.append((indent, task_id, is_someday))
    return tasks

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

    def get_folders(self) -> List[Dict[str, Any]]:
        """List folders from Joplin Local API."""
        url = self._build_url("/folders", {"fields": "id,title,parent_id"})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Thirties/0.1"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("items", data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning("Failed to fetch Joplin folders: %s", e)
        return []

    def create_folder(self, title: str, parent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Create a folder via POST /folders."""
        url = self._build_url("/folders")
        payload = {"title": title}
        if parent_id:
            payload["parent_id"] = parent_id
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Thirties/0.1"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status in (200, 201):
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error("Failed to create Joplin folder '%s': %s", title, e)
        return None

    def search_notes(self, query: str, fields: str = "id,title,parent_id,updated_time") -> List[Dict[str, Any]]:
        """Search notes via GET /search."""
        url = self._build_url("/search", {"query": query, "type": "note", "fields": fields})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Thirties/0.1"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("items", data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning("Failed to search Joplin notes for '%s': %s", query, e)
        return []

    def create_note(self, title: str, body: str, parent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Create a note via POST /notes."""
        url = self._build_url("/notes")
        payload: Dict[str, Any] = {"title": title, "body": body}
        if parent_id:
            payload["parent_id"] = parent_id
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Thirties/0.1"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status in (200, 201):
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error("Failed to create Joplin note '%s': %s", title, e)
        return None



class JoplinEngine:
    """The first ``connectors.TaskSource``: reads Joplin's SQLite database; writes go through its Data API."""

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

            # Filter by the chosen notebooks (none chosen: all but the excluded ones)
            excluded = any(full_path == nb or full_path.startswith(f"{nb} >") for nb in self.joplin_cfg.excluded_notebooks)
            is_allowed_notebook = not excluded and (not allowed_notebooks or any(
                full_path == nb or full_path.startswith(f"{nb} >")
                for nb in allowed_notebooks
            ))
            is_whitelisted_note = title in whitelist_notes

            if not (is_allowed_notebook or is_whitelisted_note):
                continue

            # 1. Checklist items, with their heading, nesting and someday context
            note_tasks = checklist_tasks(note_id, title, full_path, body, self.joplin_cfg.someday_sections)
            tasks.extend(note_tasks)
            found_checkboxes = bool(note_tasks) or any(DONE_CHECKBOX_REGEX.match(l) for l in body.splitlines())

            # 2. If it is an uncompleted todo note without internal checkboxes, treat the note itself as a task
            if is_todo and not found_checkboxes and todo_completed == 0:
                tasks.append(
                    TaskItem(
                        id=note_id,
                        source_notebook=full_path,
                        title=title,
                        description=body.strip(),
                        is_checkbox_item=False,
                        parent_note_title=None,
                        parent_note_id=None,
                    )
                )

        return tasks

    def list_notebooks(self, db_path: Optional[Path] = None) -> List[str]:
        """Top-level notebook names, for choosing which ones to read."""
        path = db_path or self.config.resolve_joplin_db_path()
        if not path or not path.is_file():
            return []
        try:
            with self._open_db(path) as conn:
                rows = conn.execute("SELECT title FROM folders WHERE deleted_time = 0 AND (parent_id = '' OR parent_id IS NULL) "
                                    "ORDER BY title").fetchall()
        except sqlite3.Error as e:
            logger.warning("Couldn't list Joplin notebooks: %s", e)
            return []
        return [r["title"] for r in rows]

    def fetch_active_notes(self, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Fetch all active non-archive notes with notebook path, title, body, and metadata."""
        path = db_path or self.config.resolve_joplin_db_path()
        if not path or not path.is_file():
            return []

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
        notes: List[Dict[str, Any]] = []

        for row in rows:
            full_path = row["full_path"]
            title = row["title"]
            body = row["body"] or ""
            is_todo = bool(row["is_todo"])
            todo_completed = row["todo_completed"]

            excluded = any(full_path == nb or full_path.startswith(f"{nb} >") for nb in self.joplin_cfg.excluded_notebooks)
            is_allowed = not excluded and (not allowed_notebooks or any(
                full_path == nb or full_path.startswith(f"{nb} >")
                for nb in allowed_notebooks
            ))
            is_whitelisted = title in whitelist_notes

            if not (is_allowed or is_whitelisted):
                continue

            notes.append({
                "id": row["id"],
                "source_notebook": full_path,
                "title": title,
                "body": body,
                "is_todo": is_todo,
                "todo_completed": todo_completed,
            })

        return notes

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
                r"^([ \t]*)-\s*\[ \]\s*" + re.escape(task.title) + r"[ \t]*$",
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
            # Keep the item's indentation so nested checklists stay nested
            updated_body = target_pattern.sub(lambda m: f"{m.group(1)}- [x] {task.title}", body, count=1)

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

    def get_sync_note_body(self, notebook_name: str, subfolder_name: str, note_title: str) -> Optional[Dict[str, Any]]:
        """Fetch sync note body from Joplin, checking API first, then local SQLite DB fallback."""
        # 1. API search
        if self.joplin_cfg.api_token:
            results = self.api_client.search_notes(note_title)
            for item in results:
                if item.get("title") == note_title:
                    note = self.api_client.get_note(item["id"])
                    if note:
                        return note

        # 2. Local SQLite DB fallback (mode=ro)
        path = self.config.resolve_joplin_db_path()
        if path and path.is_file():
            try:
                with self._open_db(path) as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT n.id, n.title, n.body, n.updated_time, f.title as folder_title
                        FROM notes n
                        JOIN folders f ON n.parent_id = f.id
                        WHERE n.title = ? AND n.deleted_time = 0
                        LIMIT 1
                    """, (note_title,))
                    row = cur.fetchone()
                    if row:
                        return {
                            "id": row["id"],
                            "title": row["title"],
                            "body": row["body"] or "",
                            "updated_time": row["updated_time"],
                        }
            except sqlite3.Error as e:
                logger.warning("Failed reading sync note from local SQLite: %s", e)

        return None

    def upsert_sync_note(
        self,
        notebook_name: str,
        subfolder_name: str,
        note_title: str,
        note_body: str,
    ) -> Tuple[bool, str]:
        """Create or update the sync note via Joplin Local API."""
        if not self.joplin_cfg.api_token:
            return False, "Joplin API token is not configured in config.toml"

        if not self.api_client.ping():
            return False, "Joplin Local API is not reachable (is Joplin desktop running?)"

        # 1. Look up folders
        folders = self.api_client.get_folders()
        archive_folder = next((f for f in folders if f.get("title") == notebook_name), None)
        archive_id = archive_folder["id"] if archive_folder else None

        subfolder = next(
            (f for f in folders if f.get("title") == subfolder_name and (archive_id is None or f.get("parent_id") == archive_id)),
            None
        )
        if not subfolder:
            created_sub = self.api_client.create_folder(title=subfolder_name, parent_id=archive_id)
            if not created_sub or "id" not in created_sub:
                return False, f"Failed to create subfolder '{subfolder_name}' via Joplin API"
            subfolder_id = created_sub["id"]
        else:
            subfolder_id = subfolder["id"]

        # 2. Check if note already exists
        existing_notes = self.api_client.search_notes(note_title)
        matching_note = next(
            (n for n in existing_notes if n.get("title") == note_title and (n.get("parent_id") == subfolder_id or not n.get("parent_id"))),
            None
        )

        if matching_note:
            note_id = matching_note["id"]
            ok = self.api_client.update_note(note_id, {"body": note_body})
            if ok:
                return True, f"Updated sync note '{note_title}' (id: {note_id})"
            return False, f"Failed updating sync note '{note_title}'"
        else:
            created_note = self.api_client.create_note(title=note_title, body=note_body, parent_id=subfolder_id)
            if created_note and "id" in created_note:
                return True, f"Created sync note '{note_title}' (id: {created_note['id']})"
            return False, f"Failed creating sync note '{note_title}'"

