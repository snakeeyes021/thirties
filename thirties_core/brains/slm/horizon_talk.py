"""Horizon talk and the day's actual times, for a Gemma user (0.2 parity with the Frontier brain).

What the Frontier brain does with set_horizon, pin_to_period, show_period, propose_period / apply_period_proposal
(horizons.py) and log_day_times (day_times.py), Gemma does in small steps:

- **Python reads the plain phrasings itself**, with no model call when that's the whole message: "that's a this-year
  thing", "I want to get to the gutters this month", "put the taxes on Thursday", "plan my week", "what's on this
  week?", "I woke up at 7", "started work at 7:15", "done with work". In a message with other requests, Python's
  reading replaces whatever the model made of that part (it tends to read "started work at 7:15" as moving Work).
- **The model names the action** (``horizon`` or ``times``, parse.py) when the words are less plain; Python still
  reads the obvious words over the model's fields, finds the item, places AM/PM near the planned time, and calls
  the same tools.
- **A short answer to the day-times question** ("☀️ For the record: when did you wake up…", day_times.py) is read
  in order: "7, and 7:30" is woke at 7 and started work at 7:30; "yes" to "Done with work?" is now.

Pinning and horizons change only Thirties' own plans, but items are still found the way urgency finds them: the
item named, or one near match asked about first ("Do you mean …?").

A mixin of ``SlmBrain`` (it uses ``_run``, ``_task_for``, ``_last_span``, ``_task_of_span``, ``_last_assistant``
and the dialog state).
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.brains.slm import steps
from thirties_core.brains.slm.parse import Action
from thirties_core.clock import format_clock

PRONOUNS = {"it", "that", "this", "them", "those", "the thing", "that one", "this one"}

# --- the day's actual times (B6) ---

_TIME = r"(?:\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?|noon|midnight)"
_AT = r"(?:\s+(?:at|around|about|by|right at|at about|at around|close to|like))*\s*"
WOKE_RE = re.compile(rf"\b(?:woke(?:\s+up)?|got\s+up|was\s+up|got\s+out\s+of\s+bed|rolled\s+out\s+of\s+bed|"
                     rf"slept(?:\s+in)?\s+(?:until|till|til))\b{_AT}(?P<t>{_TIME})", re.I)
SLEPT_RE = re.compile(rf"\b(?:went\s+to\s+(?:bed|sleep)|fell\s+asleep|got\s+to\s+(?:bed|sleep)|crashed|turned\s+in|"
                      rf"was\s+(?:in\s+bed|asleep))\b(?:\s+last\s+night)?{_AT}(?P<t>{_TIME})", re.I)
WORK_START_RE = re.compile(rf"\b(?:(?:started|began|got\s+(?:to|in\s+to|into)|clocked\s+(?:in|on)|"
                           rf"logged\s+(?:on|in)|signed\s+on)\s+(?:to\s+)?(?:work(?:ing)?|the\s+office|my\s+shift)|"
                           rf"work\s+(?:started|began)|clocked\s+in)\b{_AT}(?P<t>{_TIME})", re.I)
WORK_END_RE = re.compile(rf"\b(?:(?:done|finished|through)\s+(?:with\s+)?work(?:ing)?(?:\s+for\s+the\s+day)?|"
                         rf"(?:finished|wrapped\s+up|ended|stopped|left)\s+work|got\s+off\s+work|off\s+work\s+now|"
                         rf"clocked\s+out|logged\s+off|work\s+(?:ended|finished|wrapped\s+up)|work'?s\s+(?:done|over))"
                         rf"\b(?:{_AT}(?P<t>{_TIME}))?", re.I)
# Not a report of what happened: what will happen ("I'll be done with work at 5"), or a question.
NOT_PAST_RE = re.compile(r"\b(will|'ll|going to|gonna|plan(?:ning)? to|should|tomorrow|be done|by the time|"
                         r"when (?:do|will|am|should|can) i)\b|\?", re.I)
ASKED_WAKE = "when did you wake up"
ASKED_START = "when did you start work"
ASKED_END = "done with work?"
_BARE_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?\b|\bnoon\b", re.I)
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|yup|sure|i am|just now|just did|done|finally|all done)\b", re.I)

# --- horizons (C2) ---

_LEVEL = r"(?P<h>(?:this|next)[- ]?(?:week|month|year)|some ?day|week|month|year)"
HORIZON_RE = re.compile(rf"^(?:oh,?\s+|and\s+|also,?\s+|so\s+)?(?P<what>[^.?!]{{2,80}}?)(?:['’](?:s|re)|"
                        rf"\s+(?:is|are|feels like|sounds like))\s+(?:more\s+of\s+|really\s+|just\s+|definitely\s+|"
                        rf"probably\s+)?(?:an?\s+)?{_LEVEL}[- ]?(?:thing|item|project|goal|task|kind of thing|"
                        rf"sort of thing|type of thing)\b", re.I)
WANT_RE = re.compile(r"\b(?:i\s+)?(?:want|need|hope|would like|'d like|plan|mean|have)\s+to\s+(?:get\s+to|do|finish|"
                     r"tackle|get|deal\s+with|work\s+on|knock\s+out|sort(?:\s+out)?)\s+(?P<what>.+?)\s+"
                     r"(?:done\s+)?(?:sometime\s+|at\s+some\s+point\s+)?(?P<h>this\s+(?:week|month|year)|"
                     r"next\s+(?:week|month|year)|someday)\b", re.I)
_WHEN = (r"(?P<when>tomorrow|(?:next |this |on )?(?:mon|tues|wed|thurs|fri|sat|sun)\w*|(?:next|this) (?:week|weekend|"
         r"month|year)|the weekend|the week of [\w ]+|(?:in )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*"
         r"(?: \d{1,2})?|q[1-4])")
PIN_RE = re.compile(rf"^(?:can you\s+|could you\s+|please\s+|let'?s\s+|and\s+|ok,?\s+)?(?:put|move|plan|pin|slot|save|"
                    rf"push|bump|drop|do|leave)\s+(?P<what>.+?)\s+(?:on|for|to|into|in|till|until|til)\s+{_WHEN}"
                    rf"\s*(?:instead|then|please)?\s*[.!]*$", re.I)
SHOW_RE = re.compile(r"\b(?:what'?s|what is|what do i have|what have i got|anything|show(?: me)?|list|see|how'?s|"
                     r"how is|how are we|how am i|am i|are we|where are we)\b[^.?!]{0,40}?\b"
                     r"(?P<when>(?:this|next|the|my) (?:week|month|year)|(?:january|february|march|april|may|june|july|august|september|october|"
                     r"november|december))\b", re.I)
PLAN_RE = re.compile(r"\b(?:plan(?:ning)?|map out|set up|organi[sz]e)\s+(?:out\s+)?(?P<when>(?:my|the|this|our|next)\s+"
                     r"(?:week|month|year))\b", re.I)
_CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)\b|\b\d{1,2}:\d{2}\b|\bat \d{1,2}\b|\bnoon\b", re.I)
_LEVEL_WORDS = {"week": "week", "month": "month", "year": "year", "someday": "someday", "some day": "someday"}


def _level(text: str) -> Tuple[str, str]:
    """('this'|'next', level) from "this-year", "next month", "someday"."""
    raw = re.sub(r"[- ]+", " ", text.strip().lower())
    which = "next" if raw.startswith("next") else "this"
    word = raw.split()[-1]
    if raw.replace(" ", "") == "someday":
        return "this", "someday"
    return which, _LEVEL_WORDS.get(word, word)


def _when(h: str) -> str:
    which, level = _level(h)
    return "someday" if level == "someday" else f"{which} {level}"


def _this(period: str) -> str:
    return re.sub(r"^(my|the|our)\s+", "this ", period.strip().lower())


def read_day_times(text: str) -> Dict[str, str]:
    """The actual times a message reports, as log_day_times arguments (bare times as said)."""
    if NOT_PAST_RE.search(text):
        return {}
    out: Dict[str, str] = {}
    for key, pattern in (("woke", WOKE_RE), ("slept", SLEPT_RE), ("work_started", WORK_START_RE)):
        m = pattern.search(text)
        if m:
            out[key] = m.group("t")
    m = WORK_END_RE.search(text)
    if m:
        out["work_ended"] = m.group("t") or "now"
    return out


def read_horizon(text: str) -> Optional[Action]:
    """A horizon request in plain words, as a ``horizon`` action, or None."""
    clean = text.strip()
    m = PLAN_RE.search(clean)
    if m:
        return Action("horizon", {"topic": "plan", "when": _this(m.group("when"))})
    m = SHOW_RE.search(clean)
    if m and not re.search(r"\b(today|tonight|tomorrow|weather|rain|forecast)\b", clean, re.I):
        return Action("horizon", {"topic": "show", "when": _this(m.group("when"))})
    m = HORIZON_RE.search(clean) or WANT_RE.search(clean)
    if m:
        return Action("horizon", {"target": m.group("what").strip(), "when": _when(m.group("h"))})
    m = PIN_RE.search(clean)
    if m and not _CLOCK_RE.search(clean):
        return Action("horizon", {"target": m.group("what").strip(), "when": m.group("when").strip(), "pin": True})
    return None


class HorizonTalk:
    """Handlers for the ``horizon`` and ``times`` actions, and Python's own reading of them."""

    # --- reading ---

    def _asked_day_times(self, message: str) -> Dict[str, str]:
        """A short answer to the day-times question just asked: times in order, or a yes to "Done with work?"."""
        asked = self._last_assistant().lower()
        if not any(q in asked for q in (ASKED_WAKE, ASKED_START, ASKED_END)) or len(message) > 60:
            return {}
        times = _BARE_TIME_RE.findall(message)
        if ASKED_END in asked:
            if times:
                return {"work_ended": times[0]}
            return {"work_ended": "now"} if _YES_RE.search(message) else {}
        keys = [k for k, q in (("woke", ASKED_WAKE), ("work_started", ASKED_START)) if q in asked]
        return {k: t for k, t in zip(keys, times)}

    def _python_reads(self, piece: str) -> Optional[Action]:
        """Python's reading of one request, if it's horizon talk or the day's actual times."""
        times = read_day_times(piece)
        if times:
            return Action("times", times)
        horizon = read_horizon(piece)
        if horizon is not None and horizon.get("pin"):
            # "move dinner to Thursday" and "move it to Thursday" move what's on the schedule (the parse does that);
            # only a list item that isn't on it is pinned
            what = re.sub(r"^(?:the|my|a|an)\s+", "", horizon.get("target") or "", flags=re.I)
            if what.lower() in PRONOUNS or self._task_for(what) is None or self._span_candidates(what):
                return None
        elif horizon is not None and horizon.get("target"):
            task, _ = self._horizon_item(horizon.get("target"))
            if task is None:  # "I need to do laundry this week": not a list item; the parse may add it
                return None
        return horizon

    def _horizon_shortcut(self, text: str) -> Optional[str]:
        """The whole message is horizon talk or the day's times: Python handles it, no parse."""
        pieces = steps.split_requests(text, max_requests=99)
        acts = [self._python_reads(p) for p in pieces]
        if len(pieces) == 1 and acts[0] is None:
            asked = self._asked_day_times(text)
            acts = [Action("times", asked)] if asked else acts
        if not acts or any(a is None for a in acts):
            return None
        times = {k: v for a in acts if a.type == "times" for k, v in a.fields.items()}
        replies = [self._do_times(Action("times", times))[0]] if times else []
        replies += [self._do_horizon(a)[0] for a in acts if a.type == "horizon"]
        return "\n".join(r for r in replies if r) or None

    def _horizon_repair(self, actions: List[Action], message: str) -> List[Action]:
        """Python's reading of a part wins over the model's: the model's actions about that part go."""
        out = list(actions)
        found: List[Action] = []
        for piece in steps.split_requests(message, max_requests=99):
            act = self._python_reads(piece)
            if act is None:
                continue
            about = steps.content_stems(piece)
            envelope = act.type == "times"  # the model's reading of it: moving Work, or Work "done"
            out = [a for a in out if a.type in ("confirm", "reject", "choose")
                   or (not (steps.action_stems([a]) & about)
                       and not (envelope and a.type in ("move", "outcome")
                                and re.search(r"\b(work|sleep|bed|wake)\b", a.get("target") or "", re.I)))]
            found.append(act)
        for act in found:
            same = next((a for a in out if a.type == act.type == "times"), None)
            if same is not None:
                same.fields.update(act.fields)
            elif not any(a.type == act.type and a.fields == act.fields for a in out):
                out.append(act)
        return out

    # --- the day's actual times ---

    def _near_planned(self, key: str) -> Optional[datetime]:
        """The usual time for it on this day (week.day_hours: the user's week), to read a bare time against."""
        from thirties_core.week import day_hours
        plan = self.tools.day_plan
        gen = self.tools.scheduler.config.general if getattr(self.tools, "scheduler", None) else None
        if gen is None:
            return None
        h = day_hours(gen, plan.target_date)
        thirty = {"woke": h.sleep_end, "slept": h.sleep_start, "work_started": h.work_start,
                  "work_ended": h.work_end + 1}[key] % 48
        return datetime.combine(plan.target_date, time(thirty // 2, 30 * (thirty % 2)), tzinfo=plan.day_start.tzinfo)

    def _meridiem(self, key: str, said: str) -> str:
        """A bare "7" or "4:30" gets the AM or PM nearest its planned time (woke at 7 is 7 AM; work ended at 4:30
        is 4:30 PM)."""
        raw = str(said).strip().lower()
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", raw)
        ref = self._near_planned(key) if m else None
        if m is None or ref is None or not 1 <= int(m.group(1)) <= 12:
            return said
        hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
        target = ref.hour * 60 + ref.minute

        def gap(h: int) -> int:
            d = abs(h * 60 + minute - target)
            return min(d, 1440 - d)
        best = min((hour, hour + 12), key=gap)
        return format_clock(datetime.combine(ref.date(), time(best, minute)))

    def _do_times(self, a: Action) -> Tuple[str, bool]:
        message = getattr(self, "_message", "") or ""
        args: Dict[str, Any] = {k: a.get(k) for k in ("woke", "slept") if a.get(k)}
        for field, key in (("started", "work_started"), ("finished", "work_ended"), ("work_started", "work_started"),
                           ("work_ended", "work_ended")):
            if a.get(field):
                args[key] = a.get(field)
        args.update(read_day_times(message))  # the plain words win over the model's fields
        if not args:
            return "When was that?", False
        args = {k: self._meridiem(k, v) if v != "now" else v for k, v in args.items()}
        ok, out = self._run("log_day_times", args)
        return (out if ok else f"I couldn't record that: {out}"), ok

    # --- horizons ---

    def _horizon_item(self, ref: str):
        """(task, question): the list item meant, or what to ask."""
        ref = re.sub(r"^(?:(?:eh|oh|ok|okay|well|so|honestly|actually|hmm|yeah|and|also|um|uh|i think|i guess)"
                     r"[,!]?\s+)+", "", ref.strip(" .'\""), flags=re.I)
        if not ref or ref.lower() in PRONOUNS:
            task = self._task_of_span(self._last_span())
            return (task, None) if task is not None else (None, "Which item do you mean?")
        ref = re.sub(r"^(?:the|my|a|an)\s+", "", ref, flags=re.I)
        task = self._task_for(ref)
        if task is not None:
            return task, None
        from thirties_core.assistant_tools import backlog_match, rank_tasks
        near = [t for t in rank_tasks(list(self.tasks), ref, limit=2) if backlog_match(ref, t) != "different"]
        if len(near) == 1:
            return near[0], f"Do you mean {near[0].title}?"
        return None, f"I can't find {ref} on your lists."

    def _do_horizon(self, a: Action) -> Tuple[str, bool]:
        from thirties_core.horizons import HorizonPlanner
        if getattr(self.tools, "scheduler", None) is None:
            return "Plans beyond today aren't available here.", False
        topic = (a.get("topic") or "").lower()
        when = a.get("when") or a.get("day") or ""
        if topic == "show" or (not a.get("target") and not topic and when):
            ok, out = self._run("show_period", {"when": when or "this week"})
            return out, False
        if topic == "plan":
            return self._plan_period(HorizonPlanner(self.tools), when or "this week")
        task, question = self._horizon_item(a.get("target") or a.get("title") or "")
        if task is None:
            return question, False
        which, level = _level(when) if when else ("this", "")
        if a.get("pin") or (level not in ("week", "month", "year", "someday") or which == "next"):
            args = {"tool": "pin_to_period", "task": task.id, "when": when}
        else:
            args = {"tool": "set_horizon", "task": task.id, "horizon": level}
        if question:  # a near match: ask first, then apply on yes
            self.state.proposal = [{**args, "title": task.title}]
            self.state.proposal_kind = "horizon"
            return question, False
        return self._apply_horizon(args)

    def _apply_horizon(self, p: Dict[str, Any]) -> Tuple[str, bool]:
        self.state.proposal, self.state.proposal_kind = [], ""
        tool = p["tool"]
        ok, out = self._run(tool, {k: v for k, v in p.items() if k in ("task", "when", "horizon")})
        return (out if ok else f"I couldn't note that: {out}"), False

    def _plan_period(self, planner, when: str) -> Tuple[str, bool]:
        """Python's proposal for a week, month or year, offered in the user's words; a yes applies it (the brain's
        confirm path, through apply_period_proposal)."""
        from thirties_core.assistant_tools import ToolError
        from thirties_core.on_track import track_line
        try:
            p = planner.period(when)
            _, picks = planner.propose(p)
        except ToolError as e:
            return str(e), False
        label = planner.label(p)
        how = track_line(planner, p, label)
        if not picks:
            return ((how + "\n") if how else "") + f"Nothing is waiting for {label}: nothing pinned, due or left " \
                                                    "over.", False
        lines = ([how] if how else []) + [f"For {label} I'd plan:"]
        for c in picks:
            why = c.facts()
            lines.append(f"- {c.title}" + (f" ({why})" if why else ""))
        lines.append(f"Want {label} like that?")
        return "\n".join(lines), False


__all__ = ["HorizonTalk", "read_day_times", "read_horizon"]
