"""The assistant proposes the day (0.2 item 11, A6).

"The user is not supposed to have to micromanage either the daily items or the 'what do I need to schedule to keep
my goals on track'" (10-30). Each morning, once the routines and anchors are on (routines.py), the assistant offers
one short, concrete plan for the open time: what's pressing, not the whole backlog. Python gathers the candidates
(due dates, urgency now / soon, what's on the Today note, a little project time) and the free time; the model picks
and says why (propose_day); the proposal is remembered and goes on the plan, exactly as offered, on "yes"
(apply_day_proposal), like the rollover.

Items put off again and again are offered kindly: as a 30-minute start, with a note saying so. When no model can
pick (the on-device brain, or a failed call), Python makes the obvious proposal itself (default_items).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from thirties_core.clock import ClockError, format_clock, format_duration, parse_duration_minutes, thirty_ceil, thirty_of
from thirties_core.models import SpanCategory, SpanStatus, TaskItem

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools
    from thirties_core.models import DayPlan

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 8   # what the model chooses from: enough to choose, never the backlog
MAX_DEFAULT = 3      # Python's own proposal (no model): a few things, not a full day
DEFERRED_OFTEN = 3   # put off this many times: offered as a 30-minute start
PENDING_HOURS = 4    # a proposal left unanswered this long doesn't catch a later "yes"
MIN_FREE_MINUTES = 60  # less free time than this ahead: nothing to propose

MORNING_INSTRUCTION = (
    "[Morning, not from the user] The day has started, and its routines and anchors are on the plan. Propose the day: "
    "pick a few concrete things for the free time from DAY OPTIONS (what's pressing first; not the whole backlog), "
    "call propose_day with them, then write a short message: the proposal as a list, one line on why, and one "
    "question. If yesterday's items were just offered too, one question covers both. If the week's plan says how it's "
    "going (on track or behind), you may mention it in one line. If nothing is worth proposing, reply with only: "
    "NOTHING")


def usual_minutes(assistant: "AssistantTools", task: Optional[TaskItem], title: str) -> int:
    """How long an item usually takes: the length the user gave for it (a lasting fact), else the statistics,
    else half an hour (an hour for time on a project)."""
    said = assistant.lasting_facts(task.title if task is not None else title).minutes
    if said:
        return said
    typical = assistant.typical_duration(title) if assistant.pt.scheduler else None
    if typical:
        return max(30, int(typical))
    return 60 if task is None and title.lower().startswith("time on ") else 30


PROPOSAL_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "day_options",
        "description": "What's pressing and the free time left in the day, for proposing what to do with it. "
                       "Read-only.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "propose_day",
        "description": "Propose a short plan for the free time: a few concrete items (from DAY OPTIONS, or what the "
                       "user named), each with a start. Nothing goes on the plan yet: it's remembered and shown back "
                       "as a list for your reply, and apply_day_proposal puts exactly that on when the user agrees. "
                       "A new call replaces the last proposal.",
        "parameters": {"items": "A list of {item, start, duration}: item is a task_id or title, start a clock time "
                                "in the free time, duration optional (default: how long it usually takes).",
                       "why": "One short line on why these (optional)."},
        "required": ["items"],
    },
    {
        "name": "apply_day_proposal",
        "description": "Put the day's proposal on the plan, as proposed (after the user agrees). Items from the "
                       "lists stay linked.",
        "parameters": {"skip": "Titles or words of items to leave out (optional)."},
        "required": [],
    },
]
PROPOSAL_READ_ONLY = {"day_options"}


class DayProposal:
    """Candidates, free time, and the one proposal per day."""

    def __init__(self, assistant: "AssistantTools") -> None:
        self.a = assistant

    @property
    def pt(self):
        return self.a.pt

    @property
    def db(self):
        return self.a._db

    def _key(self, day: date) -> str:
        return f"day_proposal:{day.isoformat()}"

    def state(self, day: Optional[date] = None) -> Dict[str, Any]:
        raw = self.db.get_sync_meta(self._key(day or self.pt.day_plan.target_date)) if self.db is not None else None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _proposal_day(self) -> date:
        """The day whose proposal is meant: the day being planned, or today's (diurnal) date."""
        for day in (self.pt.day_plan.target_date, self.a._today()):
            if self.state(day).get("items"):
                return day
        return self.pt.day_plan.target_date

    def pending(self) -> bool:
        """A proposal was offered for the day being lived and not yet applied or declined (nor gone stale)."""
        data = self.state(self._proposal_day())
        if data.get("state") != "offered":
            return False
        offered = data.get("offered_at")
        return not offered or self.pt._now() - datetime.fromisoformat(offered) < timedelta(hours=PENDING_HOURS)

    def decline(self) -> None:
        day = self._proposal_day()
        data = self.state(day)
        if data:
            self.db.set_sync_meta(self._key(day), json.dumps({**data, "state": "declined"}))

    # --- what Python gathers ---

    def _bedtime(self, plan: "DayPlan") -> datetime:
        return min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start
                    and s.status != SpanStatus.SKIPPED), default=plan.day_end)

    def slots(self, plan: Optional["DayPlan"] = None) -> List[Tuple[datetime, datetime, str]]:
        """The free stretches left before bed, in whole thirties, each with what's around it ("after Work")."""
        plan = plan or self.pt.day_plan
        now = self.pt._now()
        start = max(now, plan.day_start)
        wake = max((s.end_dt for s in plan.spans if s.category == SpanCategory.SLEEP
                    and s.end_dt <= plan.day_start + timedelta(hours=12)), default=plan.day_start)
        start = max(start, wake)
        bed = self._bedtime(plan)
        saved, self.pt.day_plan = self.pt.day_plan, plan
        try:
            windows = self.pt.open_windows(after=start if start > plan.day_start else None, before=bed)
        finally:
            self.pt.day_plan = saved
        out = []
        busy = [s for s in plan.active_spans() if s.status != SpanStatus.SKIPPED and not s.tentative]
        for a, b in windows:
            a = max(a, thirty_ceil(now + timedelta(minutes=1)))  # from the next thirty: not "start this minute"
            if b - a < timedelta(minutes=30):
                continue
            before = max((s for s in busy if s.end_dt <= a + timedelta(minutes=1) and s.end_dt > a - timedelta(minutes=30)),
                         key=lambda s: s.end_dt, default=None)
            after = min((s for s in busy if s.start_dt >= b - timedelta(minutes=1) and s.start_dt < b + timedelta(minutes=30)),
                        key=lambda s: s.start_dt, default=None)
            near = ", ".join(x for x in (f"after {before.title}" if before else "",
                                         f"before {after.title}" if after else "") if x)
            out.append((a, b, near))
        return out

    def candidates(self, plan: Optional["DayPlan"] = None) -> List[Dict[str, Any]]:
        """What's worth proposing for the day, most pressing first (see _proposal_candidates)."""
        return _proposal_candidates(self, plan or self.pt.day_plan)

    def own_candidates(self, plan: "DayPlan") -> List[Dict[str, Any]]:
        """Due within a week, 'now', on the Today note, 'soon'; then a free-time pick or two and time on a project.
        Never routines, anchors, someday or reference items, anything done, or what's already on the plan."""
        from thirties_core.backlog import DONE_HIDES_DAYS
        from thirties_core.lists import urgency_label
        day = plan.target_date
        daily = self.a.routines.daily_titles()
        on_plan = {s.title.strip().lower() for s in plan.spans if s.status != SpanStatus.SKIPPED}
        on_plan_ids = {s.task_id for s in plan.spans if s.task_id}
        done = self.db.completed_since(day - timedelta(days=DONE_HIDES_DAYS)) if self.db is not None else set()
        today_titles = {t.title.strip().lower() for t in self.a.s.tasks if t.list_role == "today"}
        out: Dict[str, Dict[str, Any]] = {}
        for t in self.a.s.tasks:
            title = t.title.strip()
            low = title.lower()
            if (t.id.startswith("media:") or t.someday or t.list_role in ("routine", "reference", "backlog")
                    or low in daily or low in on_plan or t.id in on_plan_ids or t.id in done or not title):
                continue
            if t.list_role != "today" and low in today_titles:
                continue  # its copy on the Today note stands for it
            due_in = None
            if t.due:
                try:
                    due_in = (date.fromisoformat(t.due) - day).days
                except ValueError:
                    due_in = None
            if due_in is not None and due_in <= 7:
                rank, why = (0, due_in), urgency_label(t)
            elif t.urgency == "now":
                rank, why = (1, 0), "now"
            elif t.list_role == "today":
                rank, why = (2, 0), "on today's list"
            elif t.urgency == "soon":
                rank, why = (3, 0), "soon"
            elif t.list_role == "menu":
                rank, why = (5, 0), "a free-time pick"
            else:
                continue
            if t.deferred_count >= DEFERRED_OFTEN:
                why += f"; put off {t.deferred_count} times: offer a 30-minute start or breaking it down, kindly"
            if low not in out or rank < out[low]["rank"]:
                out[low] = {"rank": rank, "task": t, "title": title, "why": why,
                            "where": t.parent_note_title or t.source_notebook or "",
                            "minutes": 30 if t.deferred_count >= DEFERRED_OFTEN else usual_minutes(self.a, t, title)}
        found = sorted(out.values(), key=lambda c: (c["rank"], c["title"].lower()))
        menus = [c for c in found if c["rank"][0] == 5][:2]
        found = [c for c in found if c["rank"][0] < 5][:MAX_CANDIDATES - len(menus)] + menus
        if len(found) < MAX_CANDIDATES:
            found += self._project_time(plan, on_plan)[:min(2, MAX_CANDIDATES - len(found))]
        return found

    def _project_time(self, plan: "DayPlan", on_plan: set) -> List[Dict[str, Any]]:
        """Time on a project ("Thirties dev"): planned as time on the project, never ticket by ticket."""
        from thirties_core.lists import ListCatalog
        from thirties_core.today import notes_with_role
        if self.db is None:
            return []
        out = []
        for note in notes_with_role(ListCatalog(self.db).roles(), self.a._joplin_notes(), "backlog"):
            title = f"Time on {note['title'].strip()}"
            if title.lower() in on_plan or any(note["title"].strip().lower() in t for t in on_plan):
                continue
            out.append({"rank": (6, 0), "task": None, "title": title, "why": "keeps the project moving",
                        "where": note["title"], "minutes": usual_minutes(self.a, None, title)})
        return out

    def options_text(self, plan: Optional["DayPlan"] = None) -> str:
        plan = plan or self.pt.day_plan
        cands, slots = self.candidates(plan), self.slots(plan)
        if not cands or not slots:
            return ""
        lines = ["Pressing and possible (most pressing first):"]
        for c in cands:
            ref = f" task_id={c['task'].id}" if c["task"] is not None else ""
            lines.append(f"- {c['title']} ({c['why']}; {c['where']}; about {format_duration(c['minutes'])}){ref}")
        lines.append("Free time:")
        saved, self.pt.day_plan = self.pt.day_plan, plan
        try:
            lines += [f"- {self.pt.describe_window(a, b)}" + (f", {near}" if near else "") for a, b, near in slots]
        finally:
            self.pt.day_plan = saved
        return "\n".join(lines)

    def free_minutes(self, plan: Optional["DayPlan"] = None) -> int:
        return int(sum((b - a).total_seconds() for a, b, _ in self.slots(plan)) // 60)

    def should_offer(self, plan: Optional["DayPlan"] = None) -> bool:
        """Nothing proposed yet for the day being lived, and there's time and something to propose."""
        plan = plan or self.pt.day_plan
        if self.db is None or not self.pt.is_current(plan) or self.state(plan.target_date):
            return False
        return self.free_minutes(plan) >= MIN_FREE_MINUTES and bool(self.candidates(plan))

    def prompt_section(self) -> str:
        plan = self.pt.day_plan
        data = self.state(plan.target_date)
        if data.get("state") == "offered" and self.pending():
            return ("DAY PROPOSAL waiting for the user's answer (on yes, apply_day_proposal; to change it, "
                    "propose_day again):\n" + "\n".join(self._lines(data["items"])) + "\n\n")
        if not self.should_offer(plan):
            return ""
        return ("DAY OPTIONS (nothing has been proposed for this day yet. Once what the user asked is handled, propose "
                "one short plan for the free time with propose_day; once only)\n" + self.options_text(plan) + "\n\n")

    # --- the proposal ---

    @staticmethod
    def _lines(items: List[Dict[str, Any]]) -> List[str]:
        return [f"- {format_clock(datetime.fromisoformat(i['start']))} – {i['title']}" for i in items]

    def _store(self, items: List[Dict[str, Any]], why: str = "") -> str:
        day = self.pt.day_plan.target_date
        self.db.set_sync_meta(self._key(day), json.dumps({"items": items, "why": why, "state": "offered",
                                                          "offered_at": self.pt._now().isoformat()}))
        return "\n".join(self._lines(items))

    def default_items(self, plan: Optional["DayPlan"] = None) -> List[Dict[str, Any]]:
        """Python's own proposal (no model to pick): the most pressing few, each at the start of the first free
        stretch it fits, with half an hour of room between them (no cramming)."""
        plan = plan or self.pt.day_plan
        windows = [[a, b] for a, b, _ in self.slots(plan)]
        out = []
        for c in self.candidates(plan):
            need = timedelta(minutes=c["minutes"])
            for w in windows:
                if w[1] - w[0] >= need:
                    out.append({"title": c["title"], "task_id": c["task"].id if c["task"] is not None else None,
                                "start": w[0].isoformat(), "minutes": c["minutes"]})
                    w[0] = w[0] + need + timedelta(minutes=30)
                    break
            if len(out) >= MAX_DEFAULT:
                break
        return sorted(out, key=lambda i: i["start"])

    def propose_day(self, items: Any = None, why: str = "", **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        if self.db is None:
            raise ToolError("Proposals can't be kept here.")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except ValueError:
                raise ToolError("items must be a list of {item, start, duration}.")
        if not isinstance(items, list) or not items:
            raise ToolError("items must be a list of {item, start, duration}.")
        free = [(a, b) for a, b, _ in self.slots()]
        taken: List[Tuple[datetime, datetime]] = []
        out, problems = [], []
        for raw in items:
            if not isinstance(raw, dict):
                raise ToolError("items must be a list of {item, start, duration}.")
            ref = str(raw.get("item") or raw.get("task_id") or raw.get("task") or raw.get("title") or "").strip()
            if not ref:
                raise ToolError("Each item needs item (a task_id or title).")
            task = self.a._exact_task(ref)
            title = task.title if task is not None else ref
            try:
                start = self.pt._start(raw.get("start"))
                minutes = parse_duration_minutes(raw["duration"]) if raw.get("duration") not in (None, "") else \
                    usual_minutes(self.a, task, title)
            except (ClockError, ValueError) as e:
                raise ToolError(f"{title}: {e}")
            start, end = self.pt._snap(start, start + timedelta(minutes=minutes))
            fits = any(a <= start and end <= b for a, b in free)
            clash = next((t for t in taken if t[0] < end and start < t[1]), None)
            if not fits or clash:
                problems.append(f"{title} at {format_clock(start)}" + (" overlaps another proposed item" if clash
                                                                         else " isn't in the free time"))
                continue
            taken.append((start, end))
            out.append({"title": title, "task_id": task.id if task is not None else None, "start": start.isoformat(),
                        "minutes": minutes})
        if problems:
            raise ToolError("; ".join(problems) + ". Free time: " + ("; ".join(
                self.pt.describe_window(a, b) for a, b in free) or "none") + ".")
        listed = self._store(sorted(out, key=lambda i: i["start"]), str(why or ""))
        return ("Proposal remembered; nothing is on the plan until the user says yes (then apply_day_proposal). "
                "Show it like this:\n" + listed)

    def apply_day_proposal(self, skip: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError, _stem_words
        from thirties_core.plan_tools import NOT_DONE
        key_day = self._proposal_day()
        data = self.state(key_day)
        if not data.get("items"):
            raise ToolError("There's no proposal for today; make one with propose_day first.")
        if data.get("state") == "applied":
            return "Today's proposal is already on the plan."
        skip_words = _stem_words(" ".join(skip) if isinstance(skip, list) else str(skip or ""))
        placed, failed, left = [], [], []
        for item in data["items"]:
            if skip_words & _stem_words(item["title"]):
                left.append(item["title"])
                continue
            start = datetime.fromisoformat(item["start"])
            args = {"title": item["title"], "start": format_clock(start), "duration": f"{item['minutes']} minutes"}
            if item.get("task_id"):
                args.update(task_id=item["task_id"], category="task")
            if key_day != self.pt.day_plan.target_date:
                args["day"] = key_day.isoformat()
            out = self.pt.execute("add_event", args)
            if out.startswith(NOT_DONE):
                failed.append(f"{item['title']} ({out[len(NOT_DONE):].lstrip(': ')})")
            else:
                placed.append(f"- {format_clock(thirty_of(start))} – {item['title']}")
        self.db.set_sync_meta(self._key(key_day), json.dumps({**data, "state": "applied"}))
        parts = []
        if placed:
            parts.append("On the plan:\n" + "\n".join(placed))
        if left:
            parts.append("Left out: " + ", ".join(left) + ".")
        if failed:
            parts.append("Couldn't place: " + "; ".join(failed) + ".")
        return "\n".join(parts) or "Nothing to put on."

    def day_options(self, **_: Any) -> str:
        return self.options_text() or "Nothing pressing, or no free time left today."

    def handlers(self) -> Dict[str, Any]:
        return {"day_options": self.day_options, "propose_day": self.propose_day,
                "apply_day_proposal": self.apply_day_proposal}


def _proposal_candidates(dp: DayProposal, plan: "DayPlan") -> List[Dict[str, Any]]:
    """The candidates for a day, best first: what the horizon plans bubble down into it (pinned to the day, due,
    the week's unplaced items, ...: horizons.candidates_for_day), then this module's own (urgency, Today, menus,
    time on a project) for what they don't cover. Each is {"rank", "task", "title", "why", "where", "minutes"}."""
    own = dp.own_candidates(plan)
    try:
        from thirties_core.horizons import candidates_for_day
        bubbled = candidates_for_day(dp.a.pt, plan.target_date, limit=MAX_CANDIDATES)
    except Exception as e:  # the proposal never fails for want of the horizons
        logger.warning("Horizon candidates failed: %s", e)
        return own
    on_plan = {sp.title.strip().lower() for sp in plan.spans} | {sp.task_id for sp in plan.spans if sp.task_id}
    on_plan |= dp.a.routines.daily_titles()  # anchors and routine steps come by themselves
    on_plan |= {c.task.id for c in bubbled if c.task is not None and c.task.list_role == "routine"}
    out, seen = [], set()
    for i, c in enumerate(bubbled):
        if c.title.strip().lower() in on_plan or c.task_id in on_plan:
            continue  # already on the day
        why, minutes = c.why, usual_minutes(dp.a, c.task, c.title)
        if c.deferred >= DEFERRED_OFTEN:
            why += f"; put off {c.deferred} times: offer a 30-minute start or breaking it down, kindly"
            minutes = 30
        out.append({"rank": (0, i), "task": c.task, "title": c.title, "why": why,
                    "where": c.list or (c.task.parent_note_title if c.task is not None else "") or "",
                    "minutes": minutes})
        seen.add(c.title.lower())
    out += [c for c in own if c["title"].lower() not in seen]
    return out[:MAX_CANDIDATES]


MORNING_FACTS = (
    "\nThis is the morning's only message, so it also says, briefly and in your words, what follows (true already; "
    "keep any question in it, folded into your one question). Start with \"☀️ Good morning!\". Don't list the "
    "routines or meals: they go on by themselves every day.\n---\n{facts}\n---")


def morning_proposal(manager, facts: str = "") -> Tuple[str, bool]:
    """The morning proposal as a message: (text, already_in_the_conversation). The Frontier brain picks in a turn of
    its own, and its message also says ``facts`` (what's planned, yesterday's leftovers: one message, 10-31);
    otherwise (or if that fails) Python proposes the obvious few, and the caller adds the facts. ('', False) when
    there's nothing to propose."""
    assistant = manager.tools.assistant
    if assistant is None:
        return "", False
    dp = assistant.proposals
    plan = manager.day_plan
    if not dp.should_offer(plan):
        return "", False
    if getattr(manager, "brain_name", "") == "frontier":
        try:
            reply = manager.assistant_turn(MORNING_INSTRUCTION + (MORNING_FACTS.format(facts=facts) if facts else ""))
            if dp.state(plan.target_date).get("state") == "offered":
                return reply, True
            if reply.strip().upper().startswith("NOTHING"):
                dp.db.set_sync_meta(dp._key(plan.target_date), json.dumps({"state": "nothing"}))
                return "", True
        except Exception as e:
            logger.warning("The morning proposal turn failed: %s", e)
    items = dp.default_items(plan)
    if not items:
        return "", False
    saved, manager.tools.day_plan = manager.tools.day_plan, plan
    try:
        listed = dp._store(items)
    finally:
        manager.tools.day_plan = saved
    week = week_line(manager.tools)
    return ((week + "\n" if week else "") + "For the free time today, how about:\n" + listed
            + "\nWant these on the plan?"), False


def week_line(pt) -> str:
    """How this week's plan stands, in a line for the morning ("This week: on track, 2 of 5 done, 4 days left."),
    or '' when the week has no plan (on_track.py)."""
    if getattr(pt, "scheduler", None) is None:
        return ""
    try:
        from thirties_core.horizons import HorizonPlanner, period_of
        from thirties_core.on_track import track_line
        planner = HorizonPlanner(pt)
        return track_line(planner, period_of("week", planner.today(), planner.week_start), "this week")
    except Exception as e:  # the morning never fails for want of it
        logger.warning("This week's progress couldn't be read: %s", e)
        return ""


__all__ = ["DayProposal", "PROPOSAL_TOOL_SPECS", "PROPOSAL_READ_ONLY", "morning_proposal", "usual_minutes",
           "MORNING_INSTRUCTION", "week_line"]
