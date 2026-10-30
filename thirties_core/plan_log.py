"""A log of every change to a plan item, so "why is X skipped?" and "why did X move?" are answered from facts.

Like joplin_writeback.WriteLog for Joplin, one row per change: when, what changed (before and after: times,
status, title), which tool did it and which message caused it. Changes with no message say where they came from
instead: the Day page (a drag, a tick, Replan), the day being set up, new work hours, another device, or
"background" (Thirties on its own). The 10-28 skip nobody could account for ("accidentally marked skipped when
clearing yesterday's check-in") and the 10-27 story about why Cloudflare was split are what this is for:
``item_history`` (message_tools.py) lists these rows.

Where it hooks in: the span store itself (scheduler.StateDatabase.upsert_span / delete_span /
insert_span_if_absent / merge_span_record) and calendar_outcomes.CalendarOutcomes.record, so every path that
changes a stored item is covered, whichever tool or service did it. The cause travels in context variables:
``PlanTools.execute`` wraps each tool call in ``tool_call`` (the outermost tool wins, so put_routine's own
add_events are put_routine's), clients wrap their own changes in ``caused_by("Day page")``, and the turn's
message is joplin_writeback.current_turn (set by the conversation for the length of a turn).

Decisions:
- **Device-local, not synced.** It's an audit trail of what happened on this device; a change that came in by sync
  is logged here as "another device" (its own device has the full cause). Syncing it would double the payload for
  little: the question is nearly always asked about today, on the device the change was made on or nearby.
- **Pruned after 60 days** (``KEEP_DAYS``), once per process per database, when the table is first used.
- Only what a person would call a change is logged: title, times, status, recorded times, tentative / shown only.
  A save that changes none of those (notes, ids of facts) writes no row.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

KEEP_DAYS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    span_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    plan_date TEXT NOT NULL DEFAULT '',
    change TEXT NOT NULL,
    before TEXT NOT NULL DEFAULT '{}',
    after TEXT NOT NULL DEFAULT '{}',
    tool TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT ''
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS plan_changes_by_span ON plan_changes (span_id)"

# The fields a person would call "a change"; the rest of a span (notes, facts) is not logged.
_FIELDS = ("title", "start_dt", "end_dt", "status", "actual_start", "actual_end", "tentative", "shown_only")


@dataclass
class _Cause:
    tools: List[str] = field(default_factory=list)
    message: str = ""
    via: str = ""


_cause: contextvars.ContextVar[Optional[_Cause]] = contextvars.ContextVar("plan_change_cause", default=None)
_ready: set = set()  # databases whose table exists and was pruned this process


@contextlib.contextmanager
def caused_by(via: str) -> Iterator[None]:
    """Changes made inside are from ``via`` ("Day page", "day set-up", "new work hours", "another device")."""
    outer = _cause.get()
    token = _cause.set(_Cause(list(outer.tools) if outer else [], outer.message if outer else "", via))
    try:
        yield
    finally:
        _cause.reset(token)


@contextlib.contextmanager
def tool_call(name: str, message: Optional[str] = None) -> Iterator[None]:
    """A tool is running (PlanTools.execute). Nested calls keep the outermost tool first."""
    outer = _cause.get()
    cause = _Cause(list(outer.tools) if outer else [], outer.message if outer else "", outer.via if outer else "")
    cause.tools.append(name)
    if not cause.message and message:
        cause.message = message
    token = _cause.set(cause)
    try:
        yield
    finally:
        _cause.reset(token)


def _current() -> Tuple[str, str, str]:
    from thirties_core.joplin_writeback import current_turn
    cause = _cause.get() or _Cause()
    tools = [t for i, t in enumerate(cause.tools) if t not in cause.tools[:i]]
    return " > ".join(tools), (current_turn.get() or cause.message or "")[:500], cause.via


def _ensure(db, conn) -> None:
    key = str(getattr(db, "db_path", id(db)))
    if key in _ready:
        return
    conn.execute(_SCHEMA)
    conn.execute(_INDEX)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    conn.execute("DELETE FROM plan_changes WHERE at < ?", (cutoff,))
    _ready.add(key)


def _view(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: data.get(k) for k in _FIELDS} if data else {}


def _change(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    if not before and after:
        return "added"
    if before and not after:
        return "removed"
    kinds = []
    if before.get("title") != after.get("title"):
        kinds.append("renamed")
    b0, b1, a0, a1 = (before.get("start_dt"), before.get("end_dt"), after.get("start_dt"), after.get("end_dt"))
    if (b0, b1) != (a0, a1):
        try:
            same_length = (datetime.fromisoformat(b1) - datetime.fromisoformat(b0)
                           == datetime.fromisoformat(a1) - datetime.fromisoformat(a0))
        except (TypeError, ValueError):
            same_length = False
        kinds.append("moved" if same_length else ("moved and resized" if b0 != a0 and b1 != a1 else "resized"))
    if before.get("status") != after.get("status"):
        kinds.append(f"marked {after.get('status')}")
    if (before.get("actual_start"), before.get("actual_end")) != (after.get("actual_start"), after.get("actual_end")):
        kinds.append("actual time recorded")
    if before.get("tentative") != after.get("tentative") or before.get("shown_only") != after.get("shown_only"):
        kinds.append("confirmed" if before.get("tentative") and not after.get("tentative") else "changed how it shows")
    return ", ".join(kinds)


def record_span_change(db, conn, old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]],
                       via: Optional[str] = None) -> None:
    """Log one change to a stored item (``old``/``new``: the span's dicts, None when absent or deleted), in the
    caller's transaction. Nothing is logged when nothing a person would notice changed. Never raises: the log must
    not cost the user a change."""
    try:
        before, after = _view(old), _view(new)
        if before == after:
            return
        change = _change(before, after)
        if not change:
            return
        tool, message, cause_via = _current()
        span = new or old or {}
        start = str(span.get("start_dt") or "")
        _ensure(db, conn)
        conn.execute("INSERT INTO plan_changes (at, span_id, title, plan_date, change, before, after, tool, message, "
                     "via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (datetime.now(timezone.utc).isoformat(), str(span.get("id") or ""), str(span.get("title") or ""),
                      start[:10], change, json.dumps(before), json.dumps(after), tool, message,
                      via if via is not None else cause_via))
    except Exception:  # pragma: no cover - logging is best effort
        import logging
        logging.getLogger(__name__).debug("plan change not logged", exc_info=True)


def stored_span(conn, span_id: str) -> Optional[Dict[str, Any]]:
    """The live stored span's dict (None if absent or deleted), read before a write."""
    row = conn.execute("SELECT data, deleted FROM spans WHERE id = ?", (span_id,)).fetchone()
    if row is None or row["deleted"]:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None


# --- reading ---

def changes_for(db, span_id: str = "", title: str = "", plan_date: str = "", since: str = "") -> List[Dict[str, Any]]:
    """The logged changes to an item, oldest first: by its id, or by its exact title on a day (a redraw or a move
    to another day can give an item a new id)."""
    with db._get_connection() as conn:
        _ensure(db, conn)
        rows = conn.execute(
            "SELECT * FROM plan_changes WHERE (span_id = ? OR (? != '' AND lower(title) = ? AND plan_date = ?)) "
            "AND at >= ? ORDER BY id",
            (span_id, title, title.strip().lower(), plan_date, since)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["before"], d["after"] = json.loads(d["before"]), json.loads(d["after"])
        out.append(d)
    return out


# Tools as a person would name them when there's no message behind them.
_PAGE_ACTIONS = {"update_event": "drag", "log_outcome": "tick", "remove_event": "remove", "add_event": "add",
                 "redraw": "Replan", "undo_redraw": "Undo of Replan", "skip_routine": "skip", "pin_to_period": "pin",
                 "push_back": "push back"}


def cause_text(row: Dict[str, Any]) -> str:
    """Why a change happened, in a line: the message (and tool), else where it came from."""
    tool, via, message = row.get("tool") or "", row.get("via") or "", row.get("message") or ""
    first = tool.split(" > ")[0]
    if message:
        return f"the user's message \"{message[:200]}\"" + (f" ({tool})" if tool else "")
    if via == "Day page":
        return "Day page " + _PAGE_ACTIONS.get(first, first or "change")
    if via:
        return via + (f" ({tool})" if tool else "")
    return "background (Thirties on its own" + (f", {tool}" if tool else "") + ")"


def describe(row: Dict[str, Any], tz=None) -> str:
    """One line for item_history: when, what changed (before → after), and why."""
    from thirties_core.clock import format_clock

    def when(iso: Optional[str]) -> str:
        if not iso:
            return "?"
        t = datetime.fromisoformat(iso)
        return format_clock(t.astimezone(tz) if tz else t)

    def times(view: Dict[str, Any]) -> str:
        return f"{when(view.get('start_dt'))} – {when(view.get('end_dt'))}" if view.get("start_dt") else ""

    b, a = row["before"], row["after"]
    what = row["change"]
    detail = []
    if "renamed" in what:
        detail.append(f"'{b.get('title')}' → '{a.get('title')}'")
    if "moved" in what or "resized" in what:
        detail.append(f"{times(b)} → {times(a)}")
    if "marked" in what:
        detail.append(f"{b.get('status')} → {a.get('status')}")
    if what == "added":
        detail.append(f"at {times(a)}")
    if what == "removed":
        detail.append(f"was {times(b)}")
    at = datetime.fromisoformat(row["at"])
    return (f"{when(row['at'])}" + ("" if tz is None else f" {at.astimezone(tz):%a}") + f": {row['title']} {what}"
            + (f" ({'; '.join(detail)})" if detail else "") + f", caused by: {cause_text(row)}")


__all__ = ["KEEP_DAYS", "caused_by", "tool_call", "record_span_change", "stored_span", "changes_for", "cause_text",
           "describe"]
