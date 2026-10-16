"""Cross-device schedule and analytics synchronization engine via Joplin notes.

Leverages Joplin's encrypted cloud sync transport to synchronize:
- Event spans, per-day metadata (finalized, notes) and calendar decisions,
  merged item by item with last-write-wins
- Task rollover history and completion statistics
- Conversational continuity and assistant memory summaries

Target Note Topology:
  Notebook: Archive
  Subfolder: Thirties_System
  Note Title: Thirties_State_Sync
"""

from __future__ import annotations

import json
import logging
import platform
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.config import SyncConfig, ThirtiesConfig
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.scheduler import DayMeta, StateDatabase, TaskHistoryRecord

logger = logging.getLogger(__name__)

SYNC_PAYLOAD_START = "<!-- THIRTIES_SYNC_PAYLOAD_START -->"
SYNC_PAYLOAD_END = "<!-- THIRTIES_SYNC_PAYLOAD_END -->"
SYNC_SCHEMA = "thirties.sync.v2"
LEGACY_SYNC_SCHEMA = "thirties.sync.v1"


def encode_payload(payload: Dict[str, Any]) -> str:
    """Wrap structured sync payload in human-readable Markdown with delimiters."""
    formatted_json = json.dumps(payload, indent=2, sort_keys=True)
    return f"""# Thirties System State Sync

*Automated cross-device state synchronization for Thirties. Do not edit manually.*

{SYNC_PAYLOAD_START}
```json
{formatted_json}
```
{SYNC_PAYLOAD_END}
"""


def decode_payload(markdown_body: str) -> Optional[Dict[str, Any]]:
    """Extract and parse JSON sync payload from Markdown note body."""
    if not markdown_body:
        return None

    # 1. Delimited marker match
    if SYNC_PAYLOAD_START in markdown_body and SYNC_PAYLOAD_END in markdown_body:
        start_idx = markdown_body.find(SYNC_PAYLOAD_START) + len(SYNC_PAYLOAD_START)
        end_idx = markdown_body.find(SYNC_PAYLOAD_END)
        inner = markdown_body[start_idx:end_idx].strip()
        # Strip ```json fences if present
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", inner)
        raw_json = fence_match.group(1).strip() if fence_match else inner
        try:
            return json.loads(raw_json)
        except Exception as e:
            logger.warning("Failed parsing delimited sync JSON: %s", e)

    # 2. General markdown code fence fallback
    fence_fallback = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", markdown_body)
    if fence_fallback:
        try:
            return json.loads(fence_fallback.group(1).strip())
        except Exception:
            pass

    # 3. Direct raw JSON fallback
    try:
        return json.loads(markdown_body.strip())
    except Exception:
        return None


class SyncEngine:
    """Manages push/pull synchronization between local SQLite state and Joplin sync note."""

    def __init__(
        self,
        config: Optional[ThirtiesConfig] = None,
        state_db: Optional[StateDatabase] = None,
        joplin_engine: Optional[JoplinEngine] = None,
    ) -> None:
        self.config = config or ThirtiesConfig()
        self.sync_cfg: SyncConfig = self.config.sync
        self.state_db = state_db or StateDatabase()
        self.joplin_engine = joplin_engine or JoplinEngine(self.config)

    def get_or_create_device_id(self) -> str:
        """Return a persistent unique identifier for this device."""
        if self.sync_cfg.device_id:
            return self.sync_cfg.device_id

        stored_id = self.state_db.get_sync_meta("device_id")
        if stored_id:
            return stored_id

        node_name = platform.node() or "linux"
        clean_node = re.sub(r"[^a-zA-Z0-9_-]", "", node_name)[:16]
        new_id = f"{clean_node}-{uuid.uuid4().hex[:8]}"
        self.state_db.set_sync_meta("device_id", new_id)
        return new_id

    def export_sync_payload(self) -> Dict[str, Any]:
        """Serialize current SQLite schedules, task histories, and analytics into sync payload."""
        device_id = self.get_or_create_device_id()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Schedule state, item by item (including deletions) so merges are per item
        span_records = self.state_db.get_span_records()
        day_meta = [asdict(m) for m in self.state_db.get_all_day_meta()]
        decisions = self.state_db.get_event_decision_records()
        stores = self._stores()
        assistant_state = {name: store.all_records() for name, store in stores.items()}
        from thirties_core.settings import Settings
        settings = Settings(self.state_db).records()

        # 2. Task History & Analytics
        task_histories = self.state_db.get_all_task_histories()
        task_dict = {rec.task_id: asdict(rec) for rec in task_histories}

        # 3. Conversations & Summaries
        summaries_json = self.state_db.get_sync_meta("recent_conversation_summaries")
        recent_summaries = json.loads(summaries_json) if summaries_json else []

        return {
            "$schema": SYNC_SCHEMA,
            "device_id": device_id,
            "timestamp": now_iso,
            "version": 2,
            "spans": span_records,
            "day_meta": day_meta,
            "event_decisions": decisions,
            "assistant_state": assistant_state,
            "settings": settings,
            "analytics": {
                "task_history": task_dict,
            },
            "conversations": {
                "recent_summaries": recent_summaries,
            },
        }

    def push(self) -> Tuple[bool, str]:
        """Serialize local state and push sync payload to Joplin sync note."""
        if not self.sync_cfg.enabled:
            return False, "Sync is disabled in configuration."

        payload = self.export_sync_payload()
        body = encode_payload(payload)

        ok, msg = self.joplin_engine.upsert_sync_note(
            notebook_name=self.sync_cfg.notebook_name,
            subfolder_name=self.sync_cfg.sync_subfolder,
            note_title=self.sync_cfg.sync_note_title,
            note_body=body,
        )

        if ok:
            self.state_db.set_sync_meta("last_push_timestamp", payload["timestamp"])
            logger.info("Successfully pushed Thirties state sync payload to Joplin.")
            return True, msg
        else:
            logger.warning("Failed to push Thirties sync payload to Joplin: %s", msg)
            return False, msg

    def pull(self) -> Tuple[bool, str]:
        """Fetch sync note from Joplin, reconcile state, and merge into local SQLite."""
        if not self.sync_cfg.enabled:
            return False, "Sync is disabled in configuration."

        note_data = self.joplin_engine.get_sync_note_body(
            notebook_name=self.sync_cfg.notebook_name,
            subfolder_name=self.sync_cfg.sync_subfolder,
            note_title=self.sync_cfg.sync_note_title,
        )

        if not note_data or not note_data.get("body"):
            return False, f"Sync note '{self.sync_cfg.sync_note_title}' not found in Joplin."

        body = note_data.get("body", "")
        payload = decode_payload(body)
        if not payload or payload.get("$schema") not in (SYNC_SCHEMA, LEGACY_SYNC_SCHEMA):
            return False, "Sync note does not contain a valid Thirties sync payload."

        remote_device_id = payload.get("device_id", "unknown")
        remote_timestamp = payload.get("timestamp", "")
        local_device_id = self.get_or_create_device_id()

        # 1. Merge schedule state (last-write-wins per span, day and decision)
        merged_schedules_count = 0
        for record in payload.get("spans", []):
            merged_schedules_count += self.state_db.merge_span_record(record)
        for meta in payload.get("day_meta", []):
            merged_schedules_count += self.state_db.merge_day_meta(DayMeta(**meta))
        for record in payload.get("event_decisions", []):
            merged_schedules_count += self.state_db.merge_event_decision(record)
        merged_schedules_count += self._merge_legacy_schedules(payload.get("schedules", {}))
        for name, store in self._stores().items():
            for record in payload.get("assistant_state", {}).get(name, []):
                merged_schedules_count += store.merge_record(record)
        from thirties_core.settings import Settings
        shared = Settings(self.state_db)
        for record in payload.get("settings", []):
            merged_schedules_count += shared.merge_record(record)

        # 2. Merge Task History & Analytics (union-max counters)
        remote_tasks = payload.get("analytics", {}).get("task_history", {})
        merged_tasks_count = 0
        for task_id, t_data in remote_tasks.items():
            local_rec = self.state_db.get_task_history(task_id)
            if not local_rec:
                merged_rec = TaskHistoryRecord(
                    task_id=task_id,
                    first_scheduled_date=t_data.get("first_scheduled_date", ""),
                    last_scheduled_date=t_data.get("last_scheduled_date", ""),
                    deferred_count=t_data.get("deferred_count", 0),
                    completed_date=t_data.get("completed_date"),
                    allocated_thirties=t_data.get("allocated_thirties", 0),
                )
            else:
                # Union max merge
                merged_deferred = max(local_rec.deferred_count, t_data.get("deferred_count", 0))
                merged_allocated = max(local_rec.allocated_thirties, t_data.get("allocated_thirties", 0))
                first_date = min(
                    filter(None, [local_rec.first_scheduled_date, t_data.get("first_scheduled_date")]),
                    default=local_rec.first_scheduled_date,
                )
                last_date = max(
                    filter(None, [local_rec.last_scheduled_date, t_data.get("last_scheduled_date")]),
                    default=local_rec.last_scheduled_date,
                )
                completed = local_rec.completed_date or t_data.get("completed_date")

                merged_rec = TaskHistoryRecord(
                    task_id=task_id,
                    first_scheduled_date=first_date,
                    last_scheduled_date=last_date,
                    deferred_count=merged_deferred,
                    completed_date=completed,
                    allocated_thirties=merged_allocated,
                )

            self.state_db.upsert_task_history(merged_rec)
            merged_tasks_count += 1

        # 3. Merge Conversation Summaries
        remote_summaries = payload.get("conversations", {}).get("recent_summaries", [])
        if remote_summaries:
            local_summaries_json = self.state_db.get_sync_meta("recent_conversation_summaries")
            local_summaries = json.loads(local_summaries_json) if local_summaries_json else []
            # Merge and deduplicate by (date, summary)
            existing_pairs = {(s.get("date"), s.get("summary")) for s in local_summaries}
            for rem in remote_summaries:
                pair = (rem.get("date"), rem.get("summary"))
                if pair not in existing_pairs:
                    local_summaries.append(rem)
                    existing_pairs.add(pair)
            self.state_db.set_sync_meta("recent_conversation_summaries", json.dumps(local_summaries))

        self.state_db.set_sync_meta("last_pull_timestamp", remote_timestamp)
        result_msg = (
            f"Successfully pulled and merged {merged_schedules_count} schedule changes and "
            f"{merged_tasks_count} tasks from device '{remote_device_id}'."
        )
        logger.info(result_msg)
        return True, result_msg

    def _stores(self) -> Dict[str, Any]:
        from thirties_core.memory import (CheckinMutes, ConversationStore, EnergyLog, MemoryStore, ProfileStore,
                                          ReminderStore)
        return {
            "conversations": ConversationStore(self.state_db),
            "profile": ProfileStore(self.state_db),
            "memory": MemoryStore(self.state_db),
            "energy": EnergyLog(self.state_db),
            "reminders": ReminderStore(self.state_db),
            "checkin_mutes": CheckinMutes(self.state_db),
        }

    def _merge_legacy_schedules(self, schedules: Dict[str, Any]) -> int:
        """Import days from a pre-span (v1) payload, only for days this device has never touched."""
        from thirties_core.legacy import legacy_spans_and_decisions

        merged = 0
        for plan_date, s_data in schedules.items():
            if self.state_db.get_day_meta(datetime.fromisoformat(plan_date).date()).updated_at:
                continue
            try:
                data = json.loads(s_data.get("plan_json", ""))
                spans, decisions = legacy_spans_and_decisions(data)
            except Exception as e:
                logger.warning("Skipping unreadable legacy synced day %s: %s", plan_date, e)
                continue
            for span in spans:
                self.state_db.upsert_span(span)
            for event_id, decision in decisions.items():
                self.state_db.set_event_decision(event_id, decision)
            self.state_db.save_day_meta(DayMeta(plan_date, bool(s_data.get("is_finalized")), data.get("notes", ""),
                                                routines_applied=True))
            merged += 1
        return merged

    def sync(self) -> Tuple[bool, str]:
        """Perform bi-directional sync (pull remote changes first, then push local merged state)."""
        pull_ok, pull_msg = self.pull()
        push_ok, push_msg = self.push()
        overall_ok = pull_ok or push_ok
        summary = f"Pull: {pull_msg} | Push: {push_msg}"
        return overall_ok, summary
