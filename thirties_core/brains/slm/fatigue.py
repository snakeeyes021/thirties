"""Decision fatigue: when the user is low on energy, Thirties proposes one lighter plan and applies it on "sure"
(ROADMAP "After 0.1", item 2). One yes/no question, never a menu: choosing is the thing they're short of.

- Low energy (an energy level of 1-2, or "I'm wiped"): the heavy items still ahead today go to tomorrow; the light
  ones stay. Python decides what's heavy (``steps.effort_of``: a rough effort per item from its wording, length and
  how often it was put off); the user only says yes or no.
- "You pick" while tired: the suggestions are easy items only, fewer and shorter (see ``SlmBrain._do_suggest``).

A mixin of ``SlmBrain``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.brains.slm import steps
from thirties_core.clock import format_clock, thirty_of
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

# Effort from which an item counts as heavy on a tired day (1 easy, 2 medium, 3 hard).
HEAVY_EFFORT = 3
# On a tired day "you pick" fills at most this many easy items, each one thirty.
TIRED_MAX_ITEMS = 3


class TiredDay:
    """The "lighten" proposal and the effort reading the suggestions use."""

    def _latest_energy(self) -> Optional[int]:
        services = getattr(self.tools, "services", None)
        if services is None:
            return None
        try:
            recent = services.energy.entries(since=self._now() - timedelta(hours=4))
        except Exception:
            return None
        return int(recent[-1]["level"]) if recent else None

    def _tired(self) -> bool:
        return steps.sounds_tired(getattr(self, "_message", "") or "", self._latest_energy())

    def _span_effort(self, span: EventSpan) -> int:
        task = self._task_of_span(span)
        if task is not None:
            return steps.task_effort(task)
        return steps.effort_of(span.title, estimated_thirties=span.duration_minutes // 30)

    def _heavy_ahead(self) -> List[EventSpan]:
        """Today's flexible items still ahead that are heavy: not work, sleep, calendar, meals or routine steps,
        nothing fixed in place."""
        if not self._is_today():
            return []
        now = self._now()
        fixed = set()
        if self.tools.scheduler is not None:
            try:
                from thirties_core.facts import SpanFacts
                store = SpanFacts(self.tools.scheduler.state_db)
                fixed = {s.id for s in self.tools.day_plan.spans if store.get(s.id).anchored}
            except Exception:
                fixed = set()
        heavy = []
        for s in sorted(self.tools.day_plan.active_spans(), key=lambda s: s.start_dt):
            if s.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK) or s.status != SpanStatus.PLANNED \
                    or s.tentative or s.source == "calendar" or s.is_envelope or s.start_dt < now or s.id in fixed:
                continue
            task = self._task_of_span(s)
            if (task is not None and task.list_role == "routine") or steps.MEAL_RE.search(s.title):
                continue
            if self._span_effort(s) >= HEAVY_EFFORT:
                heavy.append(s)
        return heavy

    def _offer_lighter_day(self) -> Tuple[str, bool]:
        heavy = self._heavy_ahead()
        if not heavy:
            return "Noted that you're low on energy. What's left today is already light, so I'll leave it as it is.", False
        self.state.proposal = [{"event": s.id, "title": s.title, "to_day": "tomorrow"} for s in heavy]
        self.state.proposal_kind = "lighten"
        lines = ["Noted that you're low on energy. For a lighter rest of today, I'd move these to tomorrow:"]
        lines += [f"- {s.title} ({format_clock(thirty_of(s.start_dt))})" for s in heavy]
        lines.append("Okay?")
        return "\n".join(lines), False

    def _apply_lighter_day(self, items: List[Dict[str, Any]]) -> Tuple[str, bool]:
        moved, failed = [], []
        for p in items:
            ok, out = self._run("update_event", {"event": p["event"], "to_day": p.get("to_day") or "tomorrow"})
            (moved if ok else failed).append(p["title"])
        self.state.proposal, self.state.proposal_kind = [], ""
        lines = []
        if moved:
            lines.append("Moved to tomorrow: " + ", ".join(moved) + ". Go easy on yourself.")
        if failed:
            lines.append("Couldn't move " + ", ".join(failed) + ".")
        return "\n".join(lines), bool(moved)
