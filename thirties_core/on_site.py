"""Working on site for a day: nothing else fits during work, and what Thirties placed there moves out by itself.

Items may share time with work as a rule (some jobs leave room for a call or an errand). When the user says the
day's work is on site (at the office, at the workplace), nothing else can happen during it. That's a fact about the
day, kept from the user's own words, synced, and it holds for the whole day:

- what Thirties placed inside work (a routine's part, an errand, a call) moves out at once, by Python
  (placement.out_of_work): to the nearest free time before or after work, or tomorrow when nothing fits today; a
  routine's steps that go together move together. One line says where each went. The user isn't asked first:
  rearranging around their job is Thirties' job;
- what the user timed themselves, anything fixed, and meals (eaten at work) keep their times;
- while the fact holds, nothing new goes inside work unless the user says it really happens during it
  (alongside=true), and new hours for work that day move what the new hours cover.

The user says it in their own words; the turn picks it up from the message before the model reads it, and a tool
(work_on_site) records it too, so either way it's kept. Saying they're working remotely takes it back.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from thirties_core.scheduler import utc_now_iso

if TYPE_CHECKING:
    from thirties_core.models import DayPlan, EventSpan
    from thirties_core.plan_tools import PlanTools

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_on_site (
    day TEXT PRIMARY KEY,
    on_site INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

_WORK = r"(?:work|job|shift|office)"
_THERE = r"(?:in[- ]person|in[- ]office|in the office|at the office|on[- ]?site)"
# work said to be on site: "shift on site", "an office day", "I'll be at the office", "heading into the office"
ON_SITE_RE = re.compile(
    rf"\b{_WORK}\w*\b[^.!?;\n]{{0,30}}?\b{_THERE}\b"
    rf"|\b(?:in[- ]person|on[- ]?site|in[- ]office)\s+(?:work|shift|day|today)\b"
    r"|\boffice day\b"
    r"|\b(?:i'?m|i am|i'?ll be|im|i will be)\s+(?:in|at) the office\b"
    r"|\b(?:i'?m|i am|i'?ll be|im|i will be)\s+on[- ]?site\b"
    r"|\b(?:going|go|heading|head)\s+in(?:to| to)? (?:the office|work)\b"
    r"|\b(?:going|go|heading|head)\s+in today\b", re.IGNORECASE)
# taken back: "remote shift", "working remotely", "wfh", "not on site after all"
REMOTE_RE = re.compile(
    rf"\b(?:not|isn'?t|no longer|won'?t be)\s+(?:going\s+)?(?:in[- ]person|in the office|at the office|on[- ]?site|"
    r"in today)\b"
    rf"|\b{_WORK}\w*\b[^.!?;\n]{{0,30}}?\b(?:remote(?:ly)?|from home|virtual(?:ly)?)\b"
    r"|\b(?:working|work|wfh) from home\b|\bwfh\b|\bremote (?:today|day|work|shift)\b", re.IGNORECASE)


def said(message: Optional[str]) -> Optional[bool]:
    """What the user's words say about work being on site: True (on site), False (remote, taken back), or None
    (nothing about it)."""
    if not message:
        return None
    if REMOTE_RE.search(message):
        return False
    if ON_SITE_RE.search(message):
        return True
    return None


class OnSiteDays:
    """The on-site days, as the user said them. Synced (last write wins)."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def get(self, day: date) -> Optional[bool]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT on_site FROM work_on_site WHERE day = ?", (day.isoformat(),)).fetchone()
        return None if row is None else bool(row["on_site"])

    def set(self, day: date, on_site: bool) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO work_on_site (day, on_site, updated_at) VALUES (?, ?, ?)",
                         (day.isoformat(), int(bool(on_site)), utc_now_iso()))

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM work_on_site").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM work_on_site WHERE day = ?", (record["day"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO work_on_site (day, on_site, updated_at) VALUES (?, ?, ?)",
                         (record["day"], int(record.get("on_site", 0)), record["updated_at"]))
        return True


def _db(pt: "PlanTools"):
    return getattr(pt, "_state_db", None)


def works(plan: "DayPlan") -> List["EventSpan"]:
    """The day's work, as planned (not skipped)."""
    from thirties_core.models import SpanCategory, SpanStatus
    return sorted((s for s in plan.spans if s.category == SpanCategory.WORK and s.status != SpanStatus.SKIPPED),
                  key=lambda s: s.start_dt)


def is_on_site(pt: "PlanTools", plan: Optional["DayPlan"] = None) -> bool:
    db = _db(pt)
    if db is None:
        return False
    plan = plan or pt.day_plan
    return bool(OnSiteDays(db).get(plan.target_date))


def note_words(pt: "PlanTools", plan: Optional["DayPlan"] = None) -> Optional[bool]:
    """Keep what the user's message this turn says about work being on site, for the plan's day. Returns what
    it said (None: nothing)."""
    db = _db(pt)
    value = said(getattr(pt, "user_message", None))
    if db is None or value is None:
        return None
    OnSiteDays(db).set((plan or pt.day_plan).target_date, value)
    return value


def clear_work(pt: "PlanTools", plan: Optional["DayPlan"] = None) -> List[str]:
    """With work on site on the plan's day: move what Thirties placed inside work out of it (placement.py). The
    lines saying where each went."""
    plan = plan or pt.day_plan
    if not is_on_site(pt, plan) or not works(plan):
        return []
    from thirties_core.placement import out_of_work
    saved = pt.day_plan
    pt.day_plan = plan
    try:
        return out_of_work(pt, plan, works(plan))
    finally:
        pt.day_plan = saved


def after_work_change(pt: "PlanTools", span: "EventSpan") -> str:
    """Work's hours changed (or work was added) on a day: the user's words about working on site are kept, and on an
    on-site day what now falls inside work moves out. Text for the tool's answer ("" when nothing moved)."""
    from thirties_core.models import SpanCategory
    if span.category != SpanCategory.WORK:
        return ""
    try:
        note_words(pt)
        lines = clear_work(pt)
    except Exception:
        return ""
    return (" An on-site day, so what Thirties had put inside work moved out:\n" + "\n".join(lines)) if lines else ""


def at_turn_start(pt: "PlanTools", message: Optional[str]) -> str:
    """Before the model reads a message: if it says the day's job is on site (or remote), keep that, and on an
    on-site day move what's inside work out. A note for the model with the lines to pass on ("" when nothing to say)."""
    if said(message) is None or _db(pt) is None or not pt.is_current(pt.day_plan) or not works(pt.day_plan):
        return ""
    saved = getattr(pt, "user_message", None)
    pt.user_message = message
    try:
        value = note_words(pt)
        lines = clear_work(pt) if value else []
    except Exception:
        return ""
    finally:
        pt.user_message = saved
    if not value:
        return ""
    where = "\n".join(lines) if lines else "Nothing Thirties placed was inside work."
    return ("(The user's message makes today an on-site day: nothing else fits during work. Thirties already moved "
            "what it had placed inside work; pass these lines on as they are, and don't ask about moving "
            "anything:\n" + where + ")")


def tool(pt: "PlanTools", on_site: Any = True, day: Any = None) -> str:
    """The work_on_site tool: keep (or take back) an on-site day, and move what's inside work."""
    from thirties_core.plan_tools import ToolError, _as_bool
    db = _db(pt)
    if db is None:
        raise ToolError("This can't be kept here.")
    value = _as_bool(on_site) if on_site not in (None, "") else True
    message = getattr(pt, "user_message", None)
    if message is not None and said(message) is None and not re.search(rf"\b{_WORK}\w*\b", message, re.IGNORECASE):
        raise ToolError("An on-site day comes from the user's own words, and their message this turn says nothing "
                        "about their job. Ask them if it isn't clear.")
    plan = pt.plan_for(day) if day not in (None, "") else pt.day_plan
    OnSiteDays(db).set(plan.target_date, value)
    when = "today" if pt.is_current(plan) else plan.target_date.strftime("%A")
    if not value:
        return f"Noted: not an on-site day ({when}), so things can share time with work again."
    if not works(plan):
        return f"Noted: an on-site day ({when}); there's no work on the plan that day."
    lines = clear_work(pt, plan)
    if not lines:
        return f"Noted: an on-site day ({when}); nothing Thirties placed was inside work."
    return (f"Noted: an on-site day ({when}), so what Thirties had put inside work moved out (say these lines as "
            "they are):\n" + "\n".join(lines))


def refusal(pt: "PlanTools", span: "EventSpan", alongside: bool = False) -> Optional[str]:
    """Why ``span`` can't go where it is: inside on-site work. None when it can (not an on-site day, the
    user's own controls, alongside=true, a meal, work or sleep itself)."""
    if getattr(pt, "by_hand", False) or alongside or span.is_envelope or span.source == "calendar":
        return None
    from thirties_core.meals import meal_word
    if meal_word(span.title) is not None:
        return None
    hit = next((w for w in works(pt.day_plan) if w.start_dt < span.end_dt and span.start_dt < w.end_dt), None)
    if hit is None or not is_on_site(pt):
        return None
    from thirties_core.clock import format_clock
    return (f"Today is an on-site day, so nothing else fits during work ({format_clock(hit.start_dt)} – "
            f"{format_clock(hit.end_dt)}). Put {span.title} before or after it, or pass alongside=true only if the user "
            "says it really happens at work.")


ON_SITE_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "work_on_site",
    "description": "When the user's words make a day an on-site day (their job is at the workplace, so nothing "
                   "else fits during it), or take that back (remote after all): keep it for that day. Thirties "
                   "moves what it had placed inside work itself and says where each went: pass those lines on, "
                   "never ask whether to move them.",
    "parameters": {"on_site": "true (on site) or false (remote).",
                   "day": "The day, if not today ('tomorrow', 'Monday')."},
    "required": [],
}]


__all__ = ["said", "OnSiteDays", "works", "is_on_site", "note_words", "clear_work", "after_work_change",
           "at_turn_start", "tool", "refusal", "ON_SITE_TOOL_SPECS"]
