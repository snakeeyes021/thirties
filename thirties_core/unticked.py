"""Unticked by hand: a line Thirties ticked in one of the user's lists, and the user unticked before it was filed,
is not done after all.

Thirties' done record (task_history) hides an item from planning for a week, whatever Joplin shows (backlog.py:
done is done, even if the tick never reached Joplin). That stays so. What's different here is the write log: a
successful "complete" by Thirties on that line, and the line open again now. Only the user does that (Thirties'
own undo writes an "untick" after it, and filing writes a "file_done"), so it's read as their answer:

- the done record for the item is taken back, so it's offered again;
- the day's item for it is no longer done: it stays on the record as time spent on it (partial, its times kept);
- nothing is written to Joplin (the line is already as the user wants it; it's never ticked again).

It reads only what's on the record, so it also settles what happened before this was in place, on the first sweep.
Runs from the done sweep (done_log.DoneLog.sweep), while Joplin is up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set

from thirties_core.models import SpanStatus

logger = logging.getLogger(__name__)


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).lower()


def _ids_for(db, note_id: str, text: str) -> Set[str]:
    """Every id the line may have had: its ids in this note, and the old hashes of its open copies."""
    from thirties_core.item_ids import line_ids
    from thirties_core.joplin_engine import item_id
    return set(line_ids(db, note_id, text)) | {item_id(note_id, text.strip(), k) for k in range(3)}


def _last_writes(db, since: datetime) -> Dict[tuple, dict]:
    """The last write Thirties made to each line since ``since``: {(note id, its wording): the write}."""
    from thirties_core.joplin_writeback import WriteLog
    out: Dict[tuple, dict] = {}
    for w in WriteLog(db).since(since.astimezone(timezone.utc).isoformat()):
        op = w.get("op") or {}
        for key in ("title", "parent_title"):
            if op.get(key):
                out[(str(w.get("note_id") or op.get("note_id") or ""), _norm(op[key]))] = w
    return out


def _open_and_ticked(body: str) -> tuple:
    from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX
    open_lines, ticked = [], set()
    for line in (body or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        if m:
            open_lines.append(m.group(2).strip())
            continue
        d = DONE_CHECKBOX_REGEX.match(line)
        if d:
            ticked.add(_norm(d.groups()[-1]))
    return open_lines, ticked


def not_done_after_all(db, assistant, notes: Optional[Iterable[dict]] = None,
                       now: Optional[datetime] = None) -> List[str]:
    """Lines Thirties ticked that the user has unticked since: their done records taken back and their day's items
    no longer done. Returns what changed, for the log."""
    if db is None or assistant is None:
        return []
    from thirties_core.backlog import DONE_HIDES_DAYS
    from thirties_core.done_log import FILED_ROLES, clear_completed
    from thirties_core.lists import ListCatalog
    now = now or assistant.pt._now()
    today = assistant._today()
    completed = db.completed_since(today - timedelta(days=DONE_HIDES_DAYS))  # as long as it's hidden for
    if not completed:
        return []
    writes = _last_writes(db, now - timedelta(days=DONE_HIDES_DAYS))
    roles = ListCatalog(db).roles()
    out: List[str] = []
    for note in (assistant._joplin_notes() if notes is None else notes):
        if (roles.get(note.get("id")) or {}).get("role") not in FILED_ROLES:
            continue  # a list the user keeps (routines and the Today note have their own tick rules)
        open_lines, ticked = _open_and_ticked(note.get("body") or "")
        for text in open_lines:
            if _norm(text) in ticked:
                continue  # another copy is still ticked: which one is open again can't be told
            last = writes.get((note["id"], _norm(text)))
            if not last or (last.get("op") or {}).get("op") != "complete" or not last.get("ok"):
                continue  # Thirties never ticked it (or its tick never landed): its done record stands
            ids = _ids_for(db, note["id"], text) & completed
            if not ids:
                continue
            for task_id in ids:
                clear_completed(db, task_id)
            completed -= ids
            undone = _not_done_on_the_record(db, assistant, ids, text, today, now)
            _offer_again(assistant, ids)
            logger.info("Unticked by hand in %s: %s is not done after all%s.", note.get("title", "a list"), text,
                        f" ({undone} on the record kept as time spent on it)" if undone else "")
            out.append(f"{text} unticked by hand in {note.get('title', 'a list')}: not done after all")
    return out


def _not_done_on_the_record(db, assistant, ids: Set[str], text: str, today, now: datetime) -> int:
    """The items marked done for it on the days it's been hidden for: partial now, their real times kept."""
    from thirties_core.backlog import DONE_HIDES_DAYS
    from thirties_core.titles import title_of
    plan = assistant.pt.day_plan
    start = plan.day_start - timedelta(days=DONE_HIDES_DAYS)
    live = {s.id: s for s in plan.spans}
    name = _norm(text)
    count = 0
    for stored in db.get_spans_between(start, max(now, plan.day_end)):
        span = live.get(stored.id, stored)
        if span.status != SpanStatus.DONE:
            continue
        if not ((span.task_id and span.task_id in ids) or _norm(span.title) == name or _norm(title_of(span.title)) == name):
            continue
        span.status = SpanStatus.PARTIAL  # the time on it happened; finishing it didn't
        if span.actual_start is None:
            span.actual_start, span.actual_end = span.start_dt, span.end_dt
        db.upsert_span(span)
        count += 1
    return count


def _offer_again(assistant, ids: Set[str]) -> None:
    """Back on the lists this session reads (it was taken off when it was ticked), so it's offered again now."""
    ticked_off = getattr(assistant, "_ticked_off", {}) or {}
    have = {t.id for t in assistant.s.tasks}
    for task_id in ids:
        task = ticked_off.pop(task_id, None)
        if task is not None and task.id not in have:
            assistant.s.tasks.append(task)


__all__ = ["not_done_after_all"]
