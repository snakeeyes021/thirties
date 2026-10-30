"""The kind-of-item and moot side of AssistantTools: what Python works out about items so the rollover, the
morning proposal and the brains don't have to (AGENTS.md §7: "the kinds of items, what has gone moot").

``AssistantTools`` inherits this. Other parts of the core (the morning day proposal, routines, statistics) call:

- ``kind_of(item)``: 'once', 'ongoing' or 'daily' for a planned item, a list item or a name (kinds.py);
- ``is_moot(span, now)`` and ``settle_moot(plan, now)``: gone moot, and storing it as "unknown";
- ``lasting_facts(title)``: what holds every day for an item by name (item_facts.py);
- ``put_off_counts(span)``: whether not doing it counts as putting it off.

The rules themselves live in kinds.py and item_facts.py; this only gathers what they need (the user's lists,
routines, Today links and stored facts) from the services.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.kinds import put_off_counts
from thirties_core.models import EventSpan, SpanStatus, TaskItem

logger = logging.getLogger(__name__)


def rollover_question(plan: List[Dict[str, Any]], carry: Dict[str, List[str]]) -> str:
    """One plain question for the morning offer, naming what it would do (10-29: "Want today like that?" got
    "Today like what?")."""
    timed = any(p["start"] for p in plan)
    asks = []
    if timed:
        asks.append("plan these today at those times" if not carry["keep"] else "plan the ones with a time today")
    if carry["keep"]:
        asks.append("keep " + ("the others" if timed else "these") + " on today's list")
    if carry["add"]:
        asks.append("add the pressing " + ("one" if len(carry["add"]) == 1 else "ones"))
    if not asks:  # only what has no room today: it goes back to the lists
        return "Should I put " + ("it" if len(plan) == 1 else "them") + " back on your lists?"
    joined = asks[0] if len(asks) == 1 else ", ".join(asks[:-1]) + " and " + asks[-1]
    return f"Should I {joined}? Say which to leave out."



class KindTools:
    """Mixed into AssistantTools (it supplies ``s``, ``pt``, ``_db``, ``_today``, ``_routines``, ``_exact_task``,
    ``_writer``, ``_tick_twin`` and ``_complete_backlog_task``)."""

    # --- kinds, moot and lasting facts (kinds.py, item_facts.py): for the rollover, the morning and the brains ---

    def _kind_context(self) -> Dict[str, Any]:
        """What reading kinds needs, read once: routine names and steps, list roles, Today links, lasting facts."""
        from thirties_core.item_facts import LastingFacts
        from thirties_core.lists import ListCatalog
        from thirties_core.today import TodayLinks, note_items
        if self._db is None:
            return {"steps": set(), "roles": {}, "links": None, "lasting": None}
        routines = self._routines()
        steps = {text.lower() for n in routines for text, _ in note_items(n)} | {n["title"].strip().lower()
                                                                                for n in routines}
        return {"steps": steps, "roles": ListCatalog(self._db).roles(), "links": TodayLinks(self._db),
                "lasting": LastingFacts(self._db)}

    def kind_of(self, item: Any, ctx: Optional[Dict[str, Any]] = None) -> str:
        """'once', 'ongoing' or 'daily' (kinds.py) for a planned item (EventSpan), a list item (TaskItem) or a
        name: the user's word if they gave one, else its list's role (a Today line's home list's), else the
        obvious wording, else once."""
        from thirties_core.kinds import kind_of
        ctx = ctx or self._kind_context()
        if isinstance(item, EventSpan):
            title = item.title
            task = next((t for t in self.s.tasks if item.task_id and t.id == item.task_id), None)
            task = task or self._exact_task(title)
        elif isinstance(item, TaskItem):
            title, task = item.title, item
        else:
            title = str(item)
            task = self._exact_task(title)
        home, home_role = None, ""
        link = ctx["links"].home_of(task.title if task is not None else title) if ctx["links"] is not None else None
        if link and (task is None or task.list_role in ("today", "")):
            home = next((t for t in self.s.tasks if t.id == link["home_id"]), None)
            home_role = (ctx["roles"].get(link["home_note_id"]) or {}).get("role", "")
        names = [n for n in (title, task.title if task else None, home.title if home else None,
                             link["home_title"] if link else None) if n]
        stored = ctx["lasting"].kind_override(*names) if ctx["lasting"] is not None else ""
        return kind_of(title, task, home=home, home_role=home_role, routine_steps=ctx["steps"], stored=stored)

    def _link_kind(self, link: Dict[str, str], ctx: Dict[str, Any]) -> str:
        """The kind of a Today item's home item, from the link alone (the home item may be ticked already)."""
        from thirties_core.kinds import kind_of
        stored = ctx["lasting"].kind_override(link["home_title"]) if ctx["lasting"] is not None else ""
        return kind_of(link["home_title"], home_role=(ctx["roles"].get(link["home_note_id"]) or {}).get("role", ""),
                       routine_steps=ctx["steps"], stored=stored)

    def put_off_counts(self, span: EventSpan) -> bool:
        """Whether not doing this counts as putting it off (a daily item's doesn't)."""
        return put_off_counts(self.kind_of(span))

    def _worked_on(self, task: TaskItem) -> str:
        """Done on an ongoing item means a session: today's copy is ticked, the item itself stays open (B2)."""
        writer = self._writer()
        if task.list_role == "today":
            return self._complete_backlog_task(task, for_good=False)
        if writer is not None:
            self._tick_twin(task, writer)  # its Today copy, if it has one
        return f"(worked on it: {task.title} stays open on {task.parent_note_title or 'its list'})"

    def _plan_peek(self, day: date):
        """A day's plan as it's stored, without keeping it in PlanTools' cache of other days (a prompt or a check
        mustn't leave a stale copy behind for the next tool call)."""
        if day == self.pt.day_plan.target_date:
            return self.pt.day_plan
        cached = getattr(self.pt, "_other_days", {}).get(day)
        if cached is not None or self.pt.scheduler is None:
            return cached or self.pt.day_plan
        return self.pt.scheduler.build_day_plan(day, events=[])[0]  # its own items; calendar events have no outcome

    def _plan_of(self, span: EventSpan):
        """The day plan a span belongs to (the diurnal day its start falls in)."""
        for plan in [self.pt.day_plan] + list(getattr(self.pt, "_other_days", {}).values()):
            if plan.day_start <= span.start_dt < plan.day_end:
                return plan
        gen = self.pt.scheduler.config.general if self.pt.scheduler else None
        if gen is None:
            return self.pt.day_plan
        from thirties_core.astronomy import get_diurnal_date
        return self._plan_peek(get_diurnal_date(span.start_dt, lat=gen.latitude, lon=gen.longitude,
                                                 tz_name=gen.timezone))

    def is_moot(self, span: EventSpan, now: Optional[datetime] = None, ctx: Optional[Dict[str, Any]] = None) -> bool:
        """Past the point where it's worth doing, offering or asking about (kinds.moot_after), with nothing said."""
        from thirties_core import kinds
        plan = self._plan_of(span)
        return kinds.is_moot(span, now or self.pt._now(), self.kind_of(span, ctx), plan.day_end, plan.spans,
                             self.pt._is_fixed(span))

    def settle_moot(self, plan=None, now: Optional[datetime] = None,
                    ctx: Optional[Dict[str, Any]] = None) -> List[EventSpan]:
        """Store what has gone moot with nothing said as "unknown" (not skipped, not put off), on one plan or on
        yesterday's and today's. The background service runs this every tick; the morning before offering."""
        from thirties_core import kinds
        now = now or self.pt._now()
        if plan is None:
            today = self._today()
            plans = [self._plan_peek(today - timedelta(days=1)), self._plan_peek(today)]
        else:
            plans = [plan]
        changed: List[EventSpan] = []
        for p in plans:
            if not any(kinds.trackable(s) and s.status == SpanStatus.PLANNED and s.end_dt <= now for s in p.spans):
                continue
            ctx = ctx or self._kind_context()
            changed += kinds.settle_moot(p.spans, now, p.day_end, lambda s: self.kind_of(s, ctx), self.pt._is_fixed,
                                         self.pt._persist)
        if changed:
            logger.info("Gone moot, stored as unknown: %s.", ", ".join(s.title for s in changed))
        return changed

    def lasting_facts(self, title: str):
        """What holds every day for an item by name (item_facts.Lasting; empty if nothing is known)."""
        from thirties_core.item_facts import Lasting
        store = self.pt._lasting()
        return store.for_item(title) if store is not None else Lasting(name=title)



__all__ = ["KindTools", "rollover_question"]
