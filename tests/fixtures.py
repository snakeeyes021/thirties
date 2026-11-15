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


_TEMP_DIRS: List[str] = []


def temp_dir(prefix: str) -> str:
    """A temporary directory that's removed when the test run ends (the suite used to leave thousands behind)."""
    path = tempfile.mkdtemp(prefix=prefix)
    if not _TEMP_DIRS:
        import atexit
        import shutil
        atexit.register(lambda: [shutil.rmtree(p, ignore_errors=True) for p in _TEMP_DIRS])
    _TEMP_DIRS.append(path)
    return path


def make_scheduler(events: Optional[List[CalendarEvent]] = None, db_path: Optional[Path] = None) -> DeterministicScheduler:
    cfg = ThirtiesConfig()
    cfg.general.timezone = "America/New_York"
    cfg.assistant.after_change = "nothing"  # tests of replies see the model's text alone (test_whats_next turns it on)
    db = StateDatabase(db_path or Path(temp_dir("thirties_test_")) / "state.sqlite")
    return DeterministicScheduler(config=cfg, state_db=db, calendar_engine=MockCalendarEngine(list(events or [])))


def build_day(events: Optional[List[CalendarEvent]] = None) -> Tuple[DeterministicScheduler, DayPlan, List[CalendarEvent]]:
    sched = make_scheduler(events)
    plan, pending = sched.build_day_plan(DAY)
    return sched, plan, pending


def line_with(text: str, *facts: str) -> str:
    """The line of ``text`` that mentions every one of ``facts`` (an item, a time, a day, a count).

    Tests check what a message or tool result says (which item, which time) with this, not its exact sentence: the
    wording can change, the facts can't. Exact text is kept only where it is the contract (a note body written to
    Joplin, a few golden lines the user reads)."""
    for line in text.splitlines():
        if all(f in line for f in facts):
            return line
    raise AssertionError(f"no line mentions all of {list(facts)}:\n{text}")


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


def mark_up(db: StateDatabase, when: datetime) -> None:
    """The user wrote at ``when``: their day has started (wake.user_is_up), so the morning and check-ins can go."""
    db.set_sync_meta("last_user_message_at", when.isoformat())
