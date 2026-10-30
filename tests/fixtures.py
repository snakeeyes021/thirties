"""Shared test fixtures: a scheduler on a temporary database for a fixed day."""

from __future__ import annotations

import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.models import DayPlan
from thirties_core.scheduler import DeterministicScheduler, StateDatabase

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 10, 15)  # sunrise ~7:17 AM, so the canvas runs 7:00 AM Thu - 7:00 AM Fri


def at(hhmm: str, day_offset: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(DAY + timedelta(days=day_offset), time(h, m), tzinfo=TZ)


def event(eid: str, start: str, end: str, summary: Optional[str] = None, invite: bool = False) -> CalendarEvent:
    return CalendarEvent(
        id=eid, summary=summary or eid, start_dt=at(start), end_dt=at(end),
        is_ambiguous=invite, response_status="needsAction" if invite else "accepted",
    )


def make_scheduler(events: Optional[List[CalendarEvent]] = None, db_path: Optional[Path] = None) -> DeterministicScheduler:
    cfg = ThirtiesConfig()
    cfg.general.timezone = "America/New_York"
    db = StateDatabase(db_path or Path(tempfile.mkdtemp(prefix="thirties_test_")) / "state.sqlite")
    return DeterministicScheduler(config=cfg, state_db=db, calendar_engine=MockCalendarEngine(list(events or [])))


def build_day(events: Optional[List[CalendarEvent]] = None) -> Tuple[DeterministicScheduler, DayPlan, List[CalendarEvent]]:
    sched = make_scheduler(events)
    plan, pending = sched.build_day_plan(DAY)
    return sched, plan, pending


def occupant(plan: DayPlan, hhmm: str, day_offset: int = 0) -> Optional[str]:
    """Title of what is shown at a clock time, or None if open."""
    block = plan.block_at(at(hhmm, day_offset) + timedelta(minutes=1))
    span = plan.display_span(block) if block else None
    return span.title if span else None


class FakeJoplinAPI:
    """Joplin's local Data API, in memory: notes by id; ``up`` says whether Joplin is running."""

    def __init__(self, notes=None, up=True):
        self.notes = {k: dict(v) for k, v in (notes or {}).items()}
        self.up = up
        self.updates = []

    def ping(self):
        return self.up

    def get_note(self, note_id, fields=""):
        return dict(self.notes[note_id]) if self.up and note_id in self.notes else None

    def update_note(self, note_id, data):
        if not self.up or note_id not in self.notes:
            return False
        self.notes[note_id].update(data)
        self.updates.append((note_id, data))
        return True

    # folders and new notes (the Done notes, a list split by triage)
    folders: dict = None

    def get_folders(self):
        return [dict(f) for f in (self.folders or {}).values()] if self.up else []

    def create_folder(self, title, parent_id=None):
        if not self.up:
            return None
        self.folders = self.folders or {}
        fid = f"f{len(self.folders) + 1}"
        self.folders[fid] = {"id": fid, "title": title, "parent_id": parent_id or ""}
        return dict(self.folders[fid])

    def get_folder_notes(self, folder_id):
        return [{"id": k, "title": v.get("title", ""), "parent_id": v.get("parent_id", "")}
                for k, v in self.notes.items() if v.get("parent_id") == folder_id] if self.up else []

    def create_note(self, title, body, parent_id=None):
        if not self.up:
            return None
        nid = f"new{len(self.notes) + 1}"
        self.notes[nid] = {"id": nid, "title": title, "body": body, "parent_id": parent_id or ""}
        return {"id": nid, "title": title}
