"""What a Gemma user can do that a Gemini user can: today's list, urgency, item facts and routines (0.2 parity).

The model only names the action and the item (``today``, ``urgency``, ``fact``, ``routine``; see parse.py). Python
finds the item, reads the obvious words itself ("not urgent", "can't move"), lays a routine out, and calls the same
tools the Frontier brain calls (add_to_today, set_urgency, set_item_facts, put_routine) through ``self._run``.

A mixin of ``SlmBrain``: it uses the brain's resolution helpers (``_resolve_one``, ``_span_candidates``,
``_task_for``, ``_loose_time``) and its dialog state.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.assistant_tools import _stem, _words
from thirties_core.brains.slm.parse import Action
from thirties_core.clock import ClockError, format_clock, thirty_ceil, thirty_of
from thirties_core.models import EventSpan, SpanStatus

# A routine with no placement known: its steps go in thirties of this many, in the note's order. The user's own
# morning routine is two thirties of four (R2: "kitties, caffeinate, water and breakfast in one; throne, shower, PT
# and brush teeth in the other"). Python's default until routines carry their own placements (0.2 item 8).
ROUTINE_STEPS_PER_THIRTY = 4
# How far back to look for the last time a routine was planned, to lay it out the same way (10-30: "assistant failed
# to offer the same pattern we've been using for the past week").
ROUTINE_PATTERN_DAYS = 7

PRONOUNS = {"it", "that", "this", "them", "those", "the thing", "that one", "this one"}

# Read in Python before the model's field: the words are plain enough.
URGENCY_WORDS = [
    (re.compile(r"\b(not urgent|isn'?t urgent|no rush|not in a rush|can wait|not (?:a )?priority|low priority|"
                r"whenever)\b", re.I), "whenever"),
    (re.compile(r"\b(someday|some day|eventually|one day)\b", re.I), "someday"),
    (re.compile(r"\b(this week|next week|soon|in a few days|before long)\b", re.I), "soon"),
    (re.compile(r"\b(urgent|asap|right away|pressing|high priority|today|tomorrow)\b", re.I), "now"),
]
DUE_RE = re.compile(r"\b(?:by|before|due)\s+((?:next |this )?(?:mon|tues|wed|thurs|fri|sat|sun)\w*|tomorrow|"
                    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* \d{1,2}|\d{1,2}/\d{1,2})\b", re.I)
FIXED_RE = re.compile(r"\b(?:can'?t|cannot|not allowed to|shouldn'?t|doesn'?t|does not|never|won'?t)\s+(?:be\s+)?"
                      r"(?:move|moved|change|changed|shift|shifted)\b|\bstays? put\b|\bfixed\b|\bset in stone\b|"
                      r"\bnot movable\b|\bdon'?t move (?:it|that|this)\b", re.I)
DAYLIGHT_RE = re.compile(r"\b(?:needs?|in the|while it'?s|during)\s+(?:day)?light\b|\bbefore (?:dark|sunset)\b|"
                         r"\bwhile it'?s (?:light|bright|sunny)\b", re.I)
# "X has to be after Y", "outside things wait until after the shower": a rule, not a one-off move.
RULE_RE = re.compile(r"\b(?:has|have|needs?) to (?:be|wait|come|happen|go) (?:after|until|till)\b|"
                     r"\bmust (?:be|come|wait|go|happen) (?:after|until)\b|\b(?:always|only) after\b|"
                     r"\bnever before\b|\bnot before\b|\bno earlier than\b|"
                     r"\bcan'?t (?:start|go|happen|be done|do (?:it|that|them|anything)) (?:until|before)\b|"
                     r"\bwaits?(?:ing)? (?:until|till|til|for) (?:after )?\b|\b(?:goes|comes) after\b", re.I)
OUTSIDE_RE = re.compile(r"\b(?:outside|outdoor|outdoors)\s+(?:things|stuff|items|activities|errands)\b|"
                        r"\b(?:anything|everything|things|stuff)\s+(?:outside|outdoors)\b|\bgoing out(?:side)?\b", re.I)
AFTER_RE = re.compile(r"\bafter (?:the |my )?([\w' ]{2,30}?)(?:[,.!?]|$| and | so | because)", re.I)


class ParityActions:
    """Handlers for the ``today``, ``urgency``, ``fact`` and ``routine`` actions."""

    # --- reading ---

    def _parity(self, actions: List[Action], message: str) -> List[Action]:
        """Python's repairs for the parity actions: one call for today's list, rules said as moves or as memories
        become facts, and an add that names a routine is the routine."""
        out: List[Action] = []
        today: List[str] = []
        for a in actions:
            if a.type == "today":
                today += a.get("items") or [a.get("title") or a.get("target") or ""]
                continue
            if a.type == "move" and (a.get("after") or a.get("start")) and not a.get("shift") and RULE_RE.search(message):
                # "the walk has to be after the shower": keep it every time the day is redrawn, and now
                fields = {k: a.get(k) for k in ("target", "after", "start") if a.get(k)}
                a = Action("fact", fields)
            if a.type == "remember" and OUTSIDE_RE.search(message) and RULE_RE.search(message):
                m = AFTER_RE.search(message)
                if m:  # "outside things wait until after the shower": the rule, applied to today's items too
                    a = Action("fact", {"target": "outside things", "after": m.group(1).strip(),
                                        "text": a.get("text") or message})
            if a.type == "fact" and not any(a.get(k) for k in ("after", "start", "fixed", "daylight")):
                if FIXED_RE.search(message):
                    a.fields["fixed"] = True
                if DAYLIGHT_RE.search(message):
                    a.fields["daylight"] = True
            out.append(a)
        if today:
            out.insert(0, Action("today", {"items": [t for t in today if str(t).strip()]}))
        return out

    def _last_span(self) -> Optional[EventSpan]:
        for span_id in self.state.last_ids:
            span = self.tools.day_plan.get_span(span_id)
            if span is not None:
                return span
        return None

    def _task_of_span(self, span: Optional[EventSpan]):
        if span is None or not span.task_id:
            return None
        pool = list(self.tasks) + (list(self.tools.services.tasks) if self.tools.services else [])
        return next((t for t in pool if t.id == span.task_id), None)

    # --- today's list ---

    def _do_today(self, a: Action) -> Tuple[str, bool]:
        names: List[str] = []
        for raw in a.get("items") or [a.get("title") or a.get("target") or ""]:
            name = str(raw or "").strip(" .")
            if not name:
                continue
            if name.lower() in PRONOUNS:  # "put that on today": the item just talked about
                span = self._last_span()
                if span is None:
                    continue
                task = self._task_of_span(span)
                name = task.title if task is not None else span.title
            else:
                task = self._task_for(name)
                if task is not None:
                    name = task.title  # the list item itself, so the two stay linked
            names.append(name[:1].upper() + name[1:] if name.islower() else name)
        if not names:
            return "What should go on today's list?", False
        ok, out = self._run("add_to_today", {"items": names})
        return (out if ok else f"I couldn't add that to today's list: {out}"), False

    # --- urgency ---

    def _do_urgency(self, a: Action) -> Tuple[str, bool]:
        ref = (a.get("target") or a.get("title") or "").strip()
        task = None
        if not ref or ref.lower() in PRONOUNS:
            task = self._task_of_span(self._last_span())
            if task is None:
                span = self._last_span()
                ref = span.title if span is not None else ""
        if task is None and ref:
            task = self._task_for(ref)
        if task is None and not ref:
            return "Which item do you mean?", False
        message = getattr(self, "_message", "") or ""
        args: Dict[str, Any] = {"task": task.id if task is not None else ref}
        due = DUE_RE.search(message)
        if due or a.get("day"):
            args["due"] = due.group(1) if due else a.get("day")
        urgency = next((level for pattern, level in URGENCY_WORDS if pattern.search(message)), None)
        if urgency is None:
            urgency = a.get("urgency") if a.get("urgency") in ("now", "soon", "whenever", "someday") else None
        if urgency and not (urgency == "now" and "due" in args):
            args["urgency"] = urgency
        if "urgency" not in args and "due" not in args:
            return f"How pressing is {task.title if task else ref}: today, this week, whenever, or someday?", False
        if task is None:
            # Only a near match: a list is changed only through an exact item, so ask first (B1; the 10-24 review:
            # near matches ticked the wrong items).
            from thirties_core.assistant_tools import backlog_match, rank_tasks
            near = [t for t in rank_tasks(list(self.tasks), ref, limit=2) if backlog_match(ref, t) != "different"]
            if len(near) == 1:
                self.state.proposal = [{**args, "task": near[0].id, "title": near[0].title}]
                self.state.proposal_kind = "urgency"
                return f"Do you mean {near[0].title}?", False
        ok, out = self._run("set_urgency", args)
        if not ok:
            return f"I can't find {ref} on your lists, so I couldn't note that.", False
        return out, False

    def _apply_urgency(self, p: Dict[str, Any]) -> Tuple[str, bool]:
        self.state.proposal, self.state.proposal_kind = [], ""
        ok, out = self._run("set_urgency", {k: v for k, v in p.items() if k in ("task", "urgency", "due")})
        return out, False

    # --- facts about items ---

    def _do_fact(self, a: Action) -> Tuple[str, bool]:
        """A rule an item keeps whenever the day is redrawn; an item that breaks it right now is moved to keep it."""
        ref = (a.get("target") or a.get("title") or "").strip()
        message = getattr(self, "_message", "") or ""
        generic = bool(OUTSIDE_RE.search(ref) or (not ref and OUTSIDE_RE.search(message)))
        if generic:
            from thirties_core.weather import OUTDOOR_RE
            spans = [s for s in self.tools.day_plan.spans_in_view() if OUTDOOR_RE.search(s.title)
                     and not s.is_envelope and s.source != "calendar" and s.status == SpanStatus.PLANNED]
        else:
            span, problem = self._resolve_one(Action("fact", {"target": ref or "it"}))
            if span is None:
                return problem, False
            spans = [span]
        args: Dict[str, Any] = {}
        anchor = None
        if a.get("after"):
            anchors = [s for s in self._span_candidates(a.get("after")) if s.id not in {x.id for x in spans}]
            if len(anchors) == 1:
                anchor = anchors[0]
                args["after"] = anchor.id
            elif not generic:
                return (f"After which {a.get('after')}?" if anchors else
                        f"I don't see {a.get('after')} on the schedule, so I can't keep {spans[0].title} after it."), False
        not_before = None
        if a.get("start"):
            try:
                not_before = self._loose_time(a.get("start"), near=spans[0].start_dt if spans else None)
                args["not_before"] = format_clock(not_before)
            except ClockError:
                pass
        if a.get("daylight") or DAYLIGHT_RE.search(message):
            args["daylight"] = True
        if a.get("fixed") or FIXED_RE.search(message):
            args["stays_put"] = True
        lines: List[str] = []
        if generic:  # a standing rule: kept for later days too, and applied to what's planned today
            self._run("remember", {"note": a.get("text") or message})
            lines.append("Got it, I'll keep outside things " + (f"after {anchor.title}" if anchor else
                                                                  "to that") + " from now on.")
        if not args:
            return ("\n".join(lines) if lines else f"What should always hold for {spans[0].title}?"), False
        changed = False
        for span in spans:
            ok, out = self._run("set_item_facts", {"event": span.id, **args})
            if not ok:
                lines.append(f"I couldn't note that for {span.title}: {out}")
                continue
            if not generic:
                lines.append(out)
            start = None
            if anchor is not None and span.start_dt < anchor.end_dt:
                start = anchor.end_dt
            if not_before is not None and span.start_dt < not_before:
                start = max(start or not_before, not_before)
            if start is None:
                continue
            length = span.end_dt - span.start_dt
            ok, out = self._run("update_event", {"event": span.id, "start": format_clock(start),
                                                 "end": format_clock(start + length)})
            if ok:
                changed = True
                lines.append(f"Moved {span.title} to {format_clock(span.start_dt)} – {format_clock(span.end_dt)} "
                             f"to keep it.")
            else:
                lines.append(f"{span.title} doesn't fit there right now: {out}")
        return "\n".join(lines), changed

    # --- routines ---

    def _routine_notes(self) -> List[Dict[str, Any]]:
        assistant = getattr(self.tools, "assistant", None)
        if assistant is None or self.tools.scheduler is None:
            return []
        from thirties_core.lists import ListCatalog
        from thirties_core.today import notes_with_role
        return notes_with_role(ListCatalog(self.tools.scheduler.state_db).roles(), assistant._joplin_notes(), "routine")

    def _routine_note(self, name: str) -> Optional[Dict[str, Any]]:
        """The routine note a name points to ("morning routine", "bedtime"), or None."""
        notes = self._routine_notes()
        words = {_stem(w) for w in _words(name)} - {"routin", "my", "the"}
        hits = [n for n in notes if words and words & {_stem(w) for w in _words(n["title"])}]
        if len(hits) == 1:
            return hits[0]
        if not words and len(notes) == 1:
            return notes[0]
        return None

    def _names_a_routine(self, title: str) -> bool:
        return "routine" in (title or "").lower() and self._routine_note(title) is not None

    def _routine_pattern(self, note: Dict[str, Any], steps: List[str]) -> List[Tuple[datetime, List[str]]]:
        """How the routine was last laid out: its steps grouped by thirty, from the most recent of the last few days
        (not today), as (that day's start time, steps)."""
        db = self.tools.scheduler.state_db if self.tools.scheduler else None
        if db is None:
            return []
        by_text = {s.lower(): s for s in steps}
        today = self.tools.day_plan.target_date
        tz = self.tools.day_plan.day_start.tzinfo
        for back in range(1, ROUTINE_PATTERN_DAYS + 1):
            day = today - timedelta(days=back)
            start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            spans = [s for s in db.get_spans_between(start, start + timedelta(days=1, hours=6))
                     if s.title.lower() in by_text and s.status != SpanStatus.SKIPPED]
            if len({s.title.lower() for s in spans}) < 2:
                continue
            groups: Dict[datetime, List[str]] = {}
            for s in sorted(spans, key=lambda s: s.start_dt):
                group = groups.setdefault(thirty_of(s.start_dt), [])
                if by_text[s.title.lower()] not in group:
                    group.append(by_text[s.title.lower()])
            return sorted(groups.items())
        return []

    def _first_free_thirty(self, after: datetime) -> datetime:
        for start, end in self.tools.open_windows(after=after):
            slot = thirty_ceil(start)
            if end - slot >= timedelta(minutes=30):
                return slot
        return thirty_ceil(after)

    def _routine_parts(self, note: Dict[str, Any], steps: List[str], start: Optional[datetime]) -> List[Dict[str, Any]]:
        """Each thirty of the routine and the steps in it: the way it was last laid out, else in thirties of
        ROUTINE_STEPS_PER_THIRTY in the note's order. A given start puts the thirties back to back from it. With no
        start, last time's thirties are kept where they're still ahead; the rest go in the next free thirties."""
        plan = self.tools.day_plan
        pattern = self._routine_pattern(note, steps)
        if pattern:
            groups = [list(g) for _, g in pattern]
            seen = {s.lower() for g in groups for s in g}
            new = [s for s in steps if s.lower() not in seen]  # added to the note since: with the last thirty
            if new:
                groups[-1] += new
        else:
            n = ROUTINE_STEPS_PER_THIRTY
            groups = [steps[i:i + n] for i in range(0, len(steps), n)]
        if start is not None:
            return [{"start": format_clock(start + timedelta(minutes=30 * i)), "items": g} for i, g in enumerate(groups)]
        is_today = self._is_today()
        now = self._now()
        cursor = max(thirty_ceil(now), plan.day_start) if is_today else plan.day_start
        parts = []
        for i, group in enumerate(groups):
            usual = None
            if pattern:
                usual = datetime.combine(plan.day_start.date(), pattern[i][0].time(), tzinfo=plan.day_start.tzinfo)
            if usual is not None and usual >= cursor:
                at = usual
            else:
                at = self._first_free_thirty(cursor)
            parts.append({"start": format_clock(at), "items": group})
            cursor = at + timedelta(minutes=30)
        return parts

    def _put_routine(self, note: Dict[str, Any], start: Optional[datetime], day: Optional[str] = None
                     ) -> Tuple[bool, str]:
        from thirties_core.today import note_items
        steps = [text for text, _ in note_items(note)]  # ticked or not: the note is a template
        if not steps:
            return False, f"{note['title']} has no steps in it yet."
        saved = self.tools.day_plan
        try:
            self.tools.day_plan = self.tools.plan_for(day)
            parts = self._routine_parts(note, steps, start)
        finally:
            self.tools.day_plan = saved
        return self._run("put_routine", {"routine": note["title"], "parts": parts, **({"day": day} if day else {})})

    @staticmethod
    def _routine_reply(out: str) -> str:
        out = re.sub(r"\s*Ask the user whether to add it to the routine itself\.", "", out)
        if re.search(r"Added to today's list: [^.]+\.$", out):
            out = re.sub(r"\s*Added to today's list: [^.]+\.$", " It's on today's list too.", out)
        return out

    def _do_routine(self, a: Action) -> Tuple[str, bool]:
        name = (a.get("title") or a.get("target") or "").strip()
        note = self._routine_note(name)
        if note is None:
            names = [n["title"] for n in self._routine_notes()]
            if not names:
                return ("You don't have a routine yet: mark a note of steps as a routine in Profile → Lists, and I'll "
                        "put it on the plan."), False
            return f"Which routine: {', '.join(names)}?", False
        day = a.get("day")
        start = None
        if a.get("start"):
            saved = self.tools.day_plan
            try:
                self.tools.day_plan = self.tools.plan_for(day)
                start = self._loose_time(a.get("start"))
            except ClockError:
                return f"What time should {note['title']} start?", False
            finally:
                self.tools.day_plan = saved
        ok, out = False, ""
        if start is None:
            # When routines carry their own placements (0.2 item 8), put_routine places a routine by itself; until
            # then it asks for a start, and Python lays it out below.
            ok, out = self._run("put_routine", {"routine": note["title"], **({"day": day} if day else {})})
        if not ok:
            ok, out = self._put_routine(note, start, day)
        if not ok:
            return f"I couldn't put {note['title']} on the plan: {out}", False
        for span in self.tools.plan_for(day).spans:
            if span.task_id and span.title in out:
                self._remember(span)
                break
        return self._routine_reply(out), True
