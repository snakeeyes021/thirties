"""Gemini's tools: plain verbs over Thirties' own data layer, with a journal and a one-step undo.

No meaning checks and no rewrites: a verb does what it's told, or says plainly why it couldn't (a line that isn't
there any more, Joplin not reachable). Three safety rails only, none of them a judgement:

- every write is appended to the journal (journal.jsonl in Gemini's workspace), with what was there before;
- ``undo_last`` takes the last write back, only if nothing has changed it since;
- a whole note is never deleted, a note's last line is never removed, and routine notes are never written (they
  are reset by Thirties each morning).

Notes are addressed by id or exact title, lines by their 1-based number in ``read_lists``; a write may also give
the line's current text (``expect``), and then refuses when the line no longer says that.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BOX_RE = re.compile(r"^(\s*)([-*+])\s+\[([ xX])\]\s?(.*)$")
_BULLET_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_MARKUP_START_RE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s)")


class ToolError(Exception):
    """A verb that couldn't do its job: the reason is returned to Gemini unchanged."""


# --- the journal ---

class Journal:
    """Append-only: one JSON object per line. An undo is another line ({"undo": <id>}), never a rewrite."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def entries(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"id": uuid.uuid4().hex[:12], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **entry}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def last_write(self) -> Optional[Dict[str, Any]]:
        """The newest write not undone yet."""
        rows = self.entries()
        undone = {r["undo"] for r in rows if "undo" in r}
        for row in reversed(rows):
            if "undo" not in row and row.get("id") not in undone and row.get("kind") in ("note", "plan"):
                return row
        return None


# --- line helpers (pure) ---

def _lines(body: str) -> Tuple[List[str], bool]:
    return body.split("\n") if body else [], body.endswith("\n")


def _join(lines: List[str]) -> str:
    return "\n".join(lines)


def _pick(lines: List[str], line: Any, expect: Optional[str]) -> int:
    """The 0-based index of a 1-based line number, checked against ``expect`` when given."""
    try:
        n = int(line)
    except (TypeError, ValueError):
        raise ToolError(f"line must be a line number, got {line!r}.")
    if not 1 <= n <= len(lines):
        raise ToolError(f"The note has {len(lines)} lines; there's no line {n}.")
    if expect is not None and _plain(lines[n - 1]) != _plain(expect):
        raise ToolError(f"Line {n} now reads {lines[n - 1]!r}, not {expect!r}. Read the lists again.")
    return n - 1


def _plain(text: str) -> str:
    """A line's words: no indent, bullet or box, for comparing with what Gemini expects."""
    text = str(text or "")
    for rx, group in ((_BOX_RE, 4), (_BULLET_RE, 3), (_NUMBERED_RE, 3)):
        m = rx.match(text)
        if m:
            return m.group(group).strip()
    return text.strip()


def tick(text: str, on: bool) -> Optional[str]:
    m = _BOX_RE.match(text)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)} [{'x' if on else ' '}] {m.group(4)}".rstrip()


def _indent_of(text: str) -> str:
    return re.match(r"^\s*", text).group(0)


def _indent_unit(lines: List[str]) -> str:
    for line in lines:
        ind = _indent_of(line)
        if ind and line.strip():
            return "\t" if ind.startswith("\t") else ind
    return "    "


def _meridiem(text: str) -> str:
    """A bare "HH:MM" is a 24-hour time (the tools read times out that way): "13:50" -> "1:50 PM"."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not m or not (0 <= int(m.group(1)) <= 23):
        return text
    hour = int(m.group(1))
    return f"{hour % 12 or 12}:{m.group(2)} {'AM' if hour < 12 else 'PM'}"


def new_line(text: str, indent: str = "", checkbox: bool = True) -> str:
    """The line to write: as given when it already has markup, else a to-do (or a plain bullet)."""
    text = str(text).rstrip("\n")
    if "\n" in text:
        raise ToolError("One line at a time: the text has a line break.")
    if _MARKUP_START_RE.match(text):
        return indent + text.lstrip()
    return f"{indent}- [ ] {text.strip()}" if checkbox else f"{indent}- {text.strip()}"


# --- the tools ---

READ_TOOLS = ("read_lists", "read_calendar", "read_plan", "now")
WRITE_TOOLS = ("add_line", "tick_line", "untick_line", "edit_line", "remove_line", "set_plan")


class AgentTools:
    """The verbs, bound to one person's data: the scheduler (state database and config), the Joplin reader and a
    writer, the day the conversation is on, and the workspace's journal."""

    def __init__(self, scheduler, joplin_engine, writer_factory: Callable[[], Any], journal: Journal,
                 day: Callable[[], date], on_change: Optional[Callable[[], None]] = None) -> None:
        self.scheduler = scheduler
        self.joplin = joplin_engine
        self._writer_factory = writer_factory
        self.journal = journal
        self._day = day
        self._on_change = on_change
        self.writes = 0  # this session's writes (route.py refreshes the views when any were made)

    # --- the dispatcher (what the MCP bridge calls) ---

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> str:
        args = dict(args or {})
        fn = getattr(self, f"t_{name}", None) if name in READ_TOOLS + WRITE_TOOLS else None
        if fn is None:
            return json.dumps({"error": f"no tool named {name!r}"})
        try:
            result = fn(**args)
        except ToolError as e:
            return json.dumps({"error": str(e)})
        except TypeError as e:
            return json.dumps({"error": f"bad arguments for {name}: {e}"})
        except Exception as e:  # never take the bridge down: say what broke
            logger.exception("Agent tool %s failed", name)
            return json.dumps({"error": f"{name} failed: {e}"})
        if name in WRITE_TOOLS:
            self.writes += 1
            if self._on_change is not None:
                try:
                    self._on_change()
                except Exception as e:
                    logger.warning("Refreshing after a write from Gemini failed: %s", e)
        return json.dumps(result, ensure_ascii=False)

    # --- lists ---

    def _notes(self) -> List[Dict[str, Any]]:
        if self.joplin is None:
            raise ToolError("No lists are connected.")
        return list(self.joplin.fetch_active_notes() or [])

    def _note(self, note: str) -> Dict[str, Any]:
        notes = self._notes()
        key = str(note or "").strip()
        found = [n for n in notes if n.get("id") == key]
        if not found:
            found = [n for n in notes if str(n.get("title", "")).strip().lower() == key.lower()]
        if not found:
            raise ToolError(f"No list called {note!r}. read_lists shows the notes and their ids.")
        if len(found) > 1:
            raise ToolError(f"More than one list is called {note!r}: use its id ("
                            + ", ".join(n["id"] for n in found) + ").")
        return found[0]

    def t_read_lists(self, note: Optional[str] = None) -> Dict[str, Any]:
        notes = [self._note(note)] if note else self._notes()
        out = []
        for n in notes:
            body = self._live_body(n["id"], n.get("body", ""))
            out.append({"id": n["id"], "title": n.get("title", ""), "notebook": n.get("source_notebook", ""),
                        "lines": [f"{i}: {line}" for i, line in enumerate(_lines(body)[0], 1)]})
        return {"notes": out}

    def _live_body(self, note_id: str, fallback: str) -> str:
        api = getattr(self.joplin, "api_client", None) if self.joplin is not None else None
        try:
            if api is not None and api.ping():
                live = api.get_note(note_id, fields="id,body")
                if live and "body" in live:
                    return live["body"]
        except Exception as e:
            logger.debug("Reading the note live failed: %s", e)
        return fallback

    def _guard_note(self, note_id: str) -> None:
        if self.scheduler is None:
            return
        from thirties_core.lists import ListCatalog
        if ListCatalog(self.scheduler.state_db).role(note_id) == "routine":
            raise ToolError("That's a routine note: Thirties resets it each morning, so it isn't changed here.")

    def _edit(self, verb: str, note: str, edit: Callable[[List[str]], List[str]], args: Dict[str, Any]) -> Dict:
        target = self._note(note)
        note_id = target["id"]
        self._guard_note(note_id)
        writer = self._writer_factory()
        if writer is None or not getattr(writer, "allowed", False):
            raise ToolError("Thirties isn't allowed to change the lists on this device (Preferences › Tasks).")
        failure: List[str] = []

        def apply(body: str) -> Optional[str]:
            lines, _ = _lines(body)
            try:
                new = edit(list(lines))
            except ToolError as e:
                failure.append(str(e))
                return body  # unchanged: nothing is written
            if not any(line.strip() for line in new):
                failure.append("That would leave the note empty; a whole note is never cleared.")
                return body
            return _join(new)

        op = {"op": "agent_edit", "verb": verb, "note_id": note_id, "title": target.get("title", "")}
        ok, msg, edits = writer.edit_note(note_id, apply, op)
        if failure:
            raise ToolError(failure[0])
        if not ok:
            raise ToolError(msg or "The change didn't go through.")
        for nid, title, before, after in edits:
            self.journal.append({"kind": "note", "verb": verb, "args": args, "note_id": nid, "title": title,
                                 "before": before, "after": after})
        body = edits[-1][3] if edits else self._live_body(note_id, target.get("body", ""))
        return {"ok": True, "note": target.get("title", ""), "lines": [f"{i}: {line}" for i, line in
                                                                      enumerate(_lines(body)[0], 1)]}

    def t_add_line(self, note: str, text: str, after_line: Optional[int] = None, nested: bool = False,
                   checkbox: bool = True) -> Dict[str, Any]:
        def edit(lines: List[str]) -> List[str]:
            if after_line is None:
                anchor = len(lines) - 1
                while anchor >= 0 and not lines[anchor].strip():
                    anchor -= 1
            elif int(after_line) == 0:
                anchor = -1
            else:
                anchor = _pick(lines, after_line, None)
            indent = _indent_of(lines[anchor]) if anchor >= 0 and not lines[anchor].lstrip().startswith("#") else ""
            if nested and anchor >= 0:
                indent += _indent_unit(lines)
            lines.insert(anchor + 1, new_line(text, indent, checkbox))
            return lines
        return self._edit("add_line", note, edit, {"text": text, "after_line": after_line, "nested": nested})

    def _set_tick(self, verb: str, note: str, line: Any, expect: Optional[str], on: bool) -> Dict[str, Any]:
        def edit(lines: List[str]) -> List[str]:
            i = _pick(lines, line, expect)
            changed = tick(lines[i], on)
            if changed is None:
                raise ToolError(f"Line {i + 1} has no checkbox: {lines[i]!r}.")
            lines[i] = changed
            return lines
        return self._edit(verb, note, edit, {"line": line, "expect": expect})

    def t_tick_line(self, note: str, line: Any, expect: Optional[str] = None) -> Dict[str, Any]:
        return self._set_tick("tick_line", note, line, expect, True)

    def t_untick_line(self, note: str, line: Any, expect: Optional[str] = None) -> Dict[str, Any]:
        return self._set_tick("untick_line", note, line, expect, False)

    def t_edit_line(self, note: str, line: Any, text: str, expect: Optional[str] = None) -> Dict[str, Any]:
        def edit(lines: List[str]) -> List[str]:
            i = _pick(lines, line, expect)
            if "\n" in str(text):
                raise ToolError("One line at a time: the text has a line break.")
            old = lines[i]
            if _MARKUP_START_RE.match(str(text)):
                lines[i] = _indent_of(old) + str(text).lstrip()
            else:  # the words change; the indent, bullet and box stay
                m = _BOX_RE.match(old)
                b = _BULLET_RE.match(old) or _NUMBERED_RE.match(old)
                if m:
                    lines[i] = f"{m.group(1)}{m.group(2)} [{m.group(3)}] {str(text).strip()}"
                elif b:
                    lines[i] = f"{b.group(1)}{b.group(2)} {str(text).strip()}"
                else:
                    lines[i] = _indent_of(old) + str(text).strip()
            return lines
        return self._edit("edit_line", note, edit, {"line": line, "text": text, "expect": expect})

    def t_remove_line(self, note: str, line: Any, expect: Optional[str] = None) -> Dict[str, Any]:
        def edit(lines: List[str]) -> List[str]:
            i = _pick(lines, line, expect)
            del lines[i]
            return lines
        return self._edit("remove_line", note, edit, {"line": line, "expect": expect})

    # --- the day ---

    def _plan(self, day: Optional[str] = None):
        if self.scheduler is None:
            raise ToolError("No day plan here.")
        target = self._day()
        if day:
            try:
                target = date.fromisoformat(str(day))
            except ValueError:
                raise ToolError(f"day must be YYYY-MM-DD, got {day!r}.")
        plan, pending = self.scheduler.build_day_plan(target)
        return plan, pending

    @staticmethod
    def _hhmm(dt: datetime) -> str:
        return dt.strftime("%H:%M")

    def _span_row(self, s) -> Dict[str, Any]:
        row = {"id": s.id, "title": s.title, "start": self._hhmm(s.start_dt), "end": self._hhmm(s.end_dt),
               "length": int((s.end_dt - s.start_dt).total_seconds() // 60),
               "kind": getattr(s.category, "value", str(s.category)), "source": s.source,
               "status": getattr(s.status, "value", str(s.status))}
        if s.start_dt.date() != s.end_dt.date():
            row["ends_next_day"] = True
        for key, value in (("note", s.notes), ("task_ref", s.task_id), ("firm", s.is_firm),
                           ("tentative", s.tentative), ("whose", s.whose)):
            if value:
                row[key] = value
        return row

    def t_read_plan(self, day: Optional[str] = None) -> Dict[str, Any]:
        plan, _ = self._plan(day)
        return {"day": plan.target_date.isoformat(), "day_starts": self._hhmm(plan.day_start),
                "items": [self._span_row(s) for s in plan.spans
                          if getattr(s.category, "value", s.category) != "calendar" and not s.shown_only]}

    def t_read_calendar(self, day: Optional[str] = None) -> Dict[str, Any]:
        plan, pending = self._plan(day)
        events = [self._span_row(s) for s in plan.spans if getattr(s.category, "value", s.category) == "calendar"
                  or s.shown_only]
        invites = [{"title": e.summary, "start": self._hhmm(e.start_dt), "end": self._hhmm(e.end_dt)}
                   for e in pending]
        return {"day": plan.target_date.isoformat(), "events": events, "invites_not_answered": invites,
                **({"calendars_not_read": plan.calendar_unread} if plan.calendar_unread else {})}

    def t_set_plan(self, items: List[Dict[str, Any]], day: Optional[str] = None) -> Dict[str, Any]:
        from thirties_core.clock import ClockError, parse_duration_minutes, resolve_clock
        from thirties_core.models import EventSpan, SpanCategory, SpanStatus, new_span_id
        plan, _ = self._plan(day)
        if not isinstance(items, list):
            raise ToolError("items must be a list of {title, start, length, note?, task_ref?}.")
        new: List[EventSpan] = []
        for n, item in enumerate(items, 1):
            if not isinstance(item, dict) or not str(item.get("title", "")).strip():
                raise ToolError(f"Item {n} needs a title.")
            try:
                start = resolve_clock(_meridiem(str(item.get("start", ""))), plan.day_start)
                minutes = parse_duration_minutes(item.get("length", 30))
            except (ClockError, ValueError) as e:
                raise ToolError(f"Item {n} ({item.get('title')}): {e}")
            task_ref = str(item.get("task_ref") or "").strip() or None
            new.append(EventSpan(new_span_id(), str(item["title"]).strip(), start, start + timedelta(minutes=minutes),
                                 SpanCategory.TASK if task_ref else SpanCategory.ACTIVITY, source="user",
                                 task_id=task_ref, notes=str(item.get("note") or "")))
        replaced = [s for s in plan.spans if s.source == "user" and s.status == SpanStatus.PLANNED
                    and getattr(s.category, "value", s.category) in ("task", "activity")]
        db = self.scheduler.state_db
        for s in replaced:
            db.delete_span(s.id)
        for s in new:
            db.upsert_span(s)
        self.journal.append({"kind": "plan", "verb": "set_plan", "day": plan.target_date.isoformat(),
                             "before": [s.to_dict() for s in replaced], "after": [s.id for s in new],
                             "args": {"items": items}})
        return {"ok": True, "day": plan.target_date.isoformat(), "replaced": len(replaced),
                "items": [self._span_row(s) for s in sorted(new, key=lambda s: s.start_dt)]}

    def t_now(self) -> Dict[str, Any]:
        from thirties_core.astronomy import get_current_time
        tz = getattr(self.scheduler, "tz", None) if self.scheduler is not None else None
        now = get_current_time(tz)
        out = {"now": now.isoformat(timespec="minutes"), "weekday": now.strftime("%A"),
               "day": self._day().isoformat()}
        try:
            plan, _ = self._plan()
            out.update({"sunrise": self._hhmm(plan.sunrise), "sunset": self._hhmm(plan.sunset)})
            block = plan.block_at(now)
            if block is not None:
                out["block"] = f"{self._hhmm(block.start_dt)}-{self._hhmm(block.end_dt)}"
        except Exception as e:
            logger.debug("now: no plan details (%s)", e)
        return out

    # --- undo ---

    def undo_last(self) -> Tuple[bool, str]:
        """Take the last journaled write back. (done, what happened, in plain words)."""
        entry = self.journal.last_write()
        if entry is None:
            return False, "There's no change of Gemini's to undo."
        if entry["kind"] == "note":
            writer = self._writer_factory()
            if writer is None or not getattr(writer, "allowed", False):
                return False, "Thirties isn't allowed to change the lists on this device."
            if not writer.api.ping():
                return False, "Joplin isn't reachable, so the change can't be undone right now."
            if not writer.revert(entry["note_id"], entry["after"], entry["before"]):
                return False, (f"\"{entry.get('title') or 'The note'}\" has changed since, so Gemini's last "
                               "change wasn't undone.")
            self.journal.append({"undo": entry["id"], "kind": "undo"})
            self._changed()
            return True, f"Undone: Gemini's last change to \"{entry.get('title') or 'a list'}\"."
        db = self.scheduler.state_db
        from thirties_core.models import EventSpan
        for span_id in entry.get("after", []):
            if db.get_span(span_id) is not None:
                db.delete_span(span_id)
        for data in entry.get("before", []):
            db.upsert_span(EventSpan.from_dict(data))
        self.journal.append({"undo": entry["id"], "kind": "undo"})
        self._changed()
        return True, "Undone: Gemini's last change to the plan."

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as e:
                logger.warning("Refreshing after an undo failed: %s", e)
