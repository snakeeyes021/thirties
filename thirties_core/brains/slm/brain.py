"""The SLM brain: Gemma turns each message into actions; Python does everything else.

Per user turn:
1. Python builds a small context (time, today's items, the last assistant line,
   anything pending, a few backlog items related to the message).
2. Gemma returns actions as schema-constrained JSON (one model call).
3. Python resolves references ("it", "the second one", "taxes"), reads times
   (adding AM/PM when the user left it out), plans suggestions from the scored
   backlog and free time, executes through the shared ``PlanTools``, and writes
   the reply from templates. Gemma writes free text only for small talk.

Dialog state that must survive restarts (the pending proposal, what "it" refers
to) is stored on the assistant messages of the conversation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from thirties_core.assistant_tools import _stem, _words, rank_tasks
from thirties_core.astronomy import get_current_time
from thirties_core.brains.slm.parse import (ACTION_SCHEMA, PICKER_PROMPT, SYSTEM_PROMPT, VOICE_PROMPT, Action,
                                            build_context, extract_json, parse_actions)
from thirties_core.clock import ClockError, format_clock, parse_clock, parse_duration_minutes
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

logger = logging.getLogger(__name__)

PRONOUNS = {"it", "that", "this", "them", "those", "the thing", "that one", "this one"}
ENVELOPE_WORDS = {"work": SpanCategory.WORK, "job": SpanCategory.WORK, "shift": SpanCategory.WORK,
                  "sleep": SpanCategory.SLEEP, "bed": SpanCategory.SLEEP, "bedtime": SpanCategory.SLEEP}
CHAT_SYSTEM = ("You are Thirties, a warm, concise personal planning assistant. Reply to the user in one or two "
               "short sentences. Do not claim to have changed their schedule. Do not invent plans.")


@dataclass
class DialogState:
    last_ids: List[str] = field(default_factory=list)       # what "it" refers to
    proposal: List[Dict[str, Any]] = field(default_factory=list)  # add_event argument dicts awaiting yes/no
    proposal_kind: str = ""                                  # "plan" (all) or "options" (pick one)

    def to_dict(self) -> Dict[str, Any]:
        return {"last_ids": self.last_ids, "proposal": self.proposal, "proposal_kind": self.proposal_kind}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DialogState":
        return cls(list(data.get("last_ids", [])), list(data.get("proposal", [])), data.get("proposal_kind", ""))


class SlmBrain:
    """Pipeline brain for a small on-device model."""

    def __init__(self, tools: PlanTools, tasks: Optional[List[TaskItem]] = None, config: Any = None,
                 runtime: Any = None) -> None:
        self.tools = tools
        self.tasks = list(tasks or [])
        self.config = config
        self._runtime = runtime
        self._parser = None
        self.messages: List[Dict[str, Any]] = []
        self.state = DialogState()
        self.reset()

    # --- Brain protocol ---

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": "(SLM brain: no system prompt; see brains/slm)"}]
        self.state = DialogState()
        for name in ("_parser", "_picker", "_voice"):
            session = getattr(self, name, None)
            if session is not None:
                session.close()
            setattr(self, name, None)

    @property
    def parser(self):
        if self._parser is None:
            self._parser = self.runtime.parser(SYSTEM_PROMPT)
        return self._parser

    @property
    def voice(self):
        if getattr(self, "_voice", None) is None:
            self._voice = self.runtime.parser(VOICE_PROMPT)
        return self._voice

    def _voice_enabled(self) -> bool:
        return bool(getattr(getattr(self.config, "assistant", None), "slm_voice", True))

    _TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?[AP]M\b")

    def _polish(self, draft: str, user_text: str) -> str:
        """Natural wording from the model; facts guaranteed by checking them against the draft."""
        if not draft.strip() or not self._voice_enabled():
            return draft
        try:
            out = self.voice.ask(f'User said: "{user_text[:300]}"\nDRAFT:\n{draft}').strip().strip('"')
        except Exception as e:
            logger.warning("[SlmBrain] voice pass failed: %s", e)
            return draft
        if not out or len(out) > 2 * len(draft) + 120:
            return draft
        def norm(times):
            return {t.replace(" ", "").upper() for t in times}
        required = norm(t for line in draft.splitlines() if not line.lower().startswith(("still free", "free"))
                        for t in self._TIME_RE.findall(line))
        allowed = norm(self._TIME_RE.findall(draft))
        produced = norm(self._TIME_RE.findall(out))
        if not required <= produced or not produced <= allowed:
            logger.info("[SlmBrain] voice changed the times; using the draft")
            return draft
        names = {s.title for s in self.tools.day_plan.spans if s.title.lower() in draft.lower()}
        names |= {p["title"] for p in self.state.proposal}
        if any(n.lower() not in out.lower() for n in names):
            logger.info("[SlmBrain] voice dropped a title; using the draft")
            return draft
        if draft.rstrip().endswith("?") != out.rstrip().endswith("?"):
            return draft  # asking vs telling must not flip
        return out

    @property
    def picker(self):
        if getattr(self, "_picker", None) is None:
            self._picker = self.runtime.parser(PICKER_PROMPT)
        return self._picker

    def warm(self) -> None:
        """Load the model and process the instructions before the first message (call off the UI thread)."""
        self.parser.warm()
        if self._voice_enabled():
            self.voice.ask('User said: "thanks"\nDRAFT:\nYou\'re welcome!')

    def _restore_state(self) -> None:
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and "slm_state" in m:
                self.state = DialogState.from_dict(m["slm_state"])
                return

    @property
    def runtime(self):
        if self._runtime is None:
            from thirties_core.brains.slm.runtime import GemmaRuntime
            path = getattr(getattr(self.config, "assistant", None), "slm_model_path", "") or ""
            self._runtime = GemmaRuntime.shared(path)
        return self._runtime

    def respond(self, user_text: str) -> str:
        if len(self.messages) > 1 and not self.state.last_ids and not self.state.proposal:
            self._restore_state()
        self.messages.append({"role": "user", "content": user_text})
        self._message = user_text
        actions = self._parse(user_text)
        logger.info("[SlmBrain] actions: %s", [(a.type, a.fields) for a in actions])
        try:
            reply = self._execute(actions, user_text)
            if actions and not all(a.type == "chat" for a in actions):
                reply = self._polish(reply, user_text)
        except Exception as e:  # never leave the user without an answer
            logger.exception("[SlmBrain] failed handling %r", user_text)
            reply = f"Sorry, something went wrong on my side ({e}). Could you try saying that another way?"
        self.messages.append({"role": "assistant", "content": reply, "slm_state": self.state.to_dict(),
                              "slm_actions": [{"type": a.type, **a.fields} for a in actions]})
        return reply

    # --- 1. context and parsing ---

    def _now(self) -> datetime:
        return get_current_time(self.tools.day_plan.day_start.tzinfo)

    def _is_today(self) -> bool:
        return self.tools.day_plan.target_date == self.tools.today()

    def _schedule_lines(self) -> List[str]:
        now = self._now()
        lines = []
        for s in self.tools.day_plan.spans_in_view():
            if self._is_today() and s.end_dt <= now and not s.is_envelope:
                continue
            note = " (unconfirmed invite)" if s.tentative else ""
            lines.append(f"{s.title} {format_clock(s.start_dt)}-{format_clock(s.end_dt)}{note}")
        return lines[:14]

    def _last_assistant(self) -> str:
        for m in reversed(self.messages[:-1]):
            if m.get("role") == "assistant":
                return str(m.get("content", ""))
        return ""

    def _pending_text(self) -> Optional[str]:
        if not self.state.proposal:
            return None
        items = "; ".join(f"{p['title']} at {p['start']}" for p in self.state.proposal)
        kind = "pick one of" if self.state.proposal_kind == "options" else "whether to add"
        return f"{kind}: {items}"

    def _parse(self, message: str) -> List[Action]:
        now = self._now()
        related = [t.title for t in rank_tasks([t for t in self.tasks if not t.someday], message, limit=5)]
        last = self.tools.day_plan.get_span(self.state.last_ids[0]) if self.state.last_ids else None
        context = build_context(
            now_text=now.strftime("%A ") + format_clock(now),
            schedule_lines=self._schedule_lines(),
            last_assistant=self._last_assistant(),
            pending=self._pending_text(),
            related_tasks=related,
            message=message,
            last_item=last.title if last else "",
        )
        text = self.parser.ask(context)
        raw = extract_json(text)
        if raw is None or "actions" not in raw:
            logger.warning("[SlmBrain] unparseable parser output %r; retrying with constrained decoding", text[:300])
            raw = self.runtime.json_call(SYSTEM_PROMPT, context, ACTION_SCHEMA)
        return parse_actions(raw)

    # --- 2. resolution helpers ---

    def _loose_time(self, text: str, after: Optional[datetime] = None, prefer_future: bool = True,
                    near: Optional[datetime] = None) -> datetime:
        """A clock time on the planned day; adds AM/PM sensibly when the user left it out."""
        plan = self.tools.day_plan
        raw = str(text).strip().lower()
        if raw in ("now", "right now", "asap", "next"):
            now = self._now()
            return now.replace(minute=0 if now.minute < 15 else (30 if now.minute < 45 else 0), second=0,
                               microsecond=0) + (timedelta(hours=1) if now.minute >= 45 else timedelta())
        try:
            parsed = parse_clock(raw)
            candidates_t = [parsed]
        except ClockError:
            m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", raw.replace(" ", ""))
            if not m:
                raise
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if not 1 <= hour <= 12 or minute > 59:
                raise ClockError(f"'{text}' is not a time I can read.")
            candidates_t = [time(hour % 12, minute), time(hour % 12 + 12, minute)]
        candidates: List[datetime] = []
        for t in candidates_t:
            for offset in (0, 1):
                dt = datetime.combine(plan.day_start.date() + timedelta(days=offset), t, tzinfo=plan.day_start.tzinfo)
                if after is not None:
                    if dt > after and dt - after <= timedelta(hours=16):
                        candidates.append(dt)
                elif plan.day_start <= dt < plan.day_end:
                    candidates.append(dt)
        if not candidates:
            raise ClockError(f"I couldn't place '{text}' on {plan.target_date.strftime('%A')}.")
        if after is not None:
            return min(candidates)
        if near is not None and len(candidates) > 1:
            return min(candidates, key=lambda dt: abs(dt.timestamp() - near.timestamp()))
        now = self._now()

        def score(dt: datetime) -> Tuple[int, int, float]:
            asleep = any(s.category == SpanCategory.SLEEP and s.covers(dt) for s in plan.active_spans())
            past = prefer_future and self._is_today() and dt < now - timedelta(minutes=30)
            odd_hour = not (6 <= dt.hour < 23)
            return (int(past) + int(asleep) * 2, int(odd_hour), dt.timestamp())
        return min(candidates, key=score)

    def _clock_arg(self, dt: datetime) -> str:
        return format_clock(dt)

    ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1, "final": -1}

    def _span_candidates(self, ref: str) -> List[EventSpan]:
        plan = self.tools.day_plan
        ref_l = (ref or "").strip().lower()
        spans = plan.spans_in_view()
        ordinal = next((v for k, v in self.ORDINALS.items() if re.search(rf"\b{k}\b", ref_l)), None)
        if ordinal is not None:
            rest = re.sub(r"\b(first|1st|second|2nd|third|3rd|last|final|round|session|part|block|of|the)\b", " ", ref_l)
            matches = self._span_candidates(rest.strip())
            matches = sorted(matches, key=lambda s: s.start_dt)
            if matches and (ordinal == -1 or ordinal < len(matches)):
                return [matches[ordinal]]
        if not ref_l or ref_l in PRONOUNS:
            remembered = [plan.get_span(i) for i in self.state.last_ids]
            remembered = [s for s in remembered if s is not None]
            if remembered:
                return remembered[:1]
            movable = [s for s in spans if not s.is_envelope and s.source != "calendar"]
            return movable[-1:] if len(movable) == 1 else movable
        for word, cat in ENVELOPE_WORDS.items():
            if ref_l == word or ref_l == f"my {word}":
                return [s for s in spans if s.category == cat]
        exact = [s for s in spans if s.title.lower() == ref_l or s.id == ref]
        if exact:
            return exact
        q = {_stem(w) for w in _words(ref_l)}
        scored = []
        for s in spans:
            overlap = len(q & {_stem(w) for w in _words(s.title)})
            if overlap:
                scored.append((overlap, s))
        if not scored:
            return []
        best = max(o for o, _ in scored)
        return [s for o, s in scored if o == best]

    def _remember(self, *spans: EventSpan) -> None:
        ids = [s.id for s in spans if s is not None]
        if ids:
            self.state.last_ids = ids + [i for i in self.state.last_ids if i not in ids][:3]

    def _task_for(self, title: str) -> Optional[TaskItem]:
        q = {_stem(w) for w in _words(title)}
        if not q:
            return None
        best = None
        for t in self.tasks:
            overlap = len(q & {_stem(w) for w in _words(t.title)})
            if overlap >= max(2, len(q)) or (overlap and overlap == len({_stem(w) for w in _words(t.title)})):
                if best is None or overlap > best[0]:
                    best = (overlap, t)
        return best[1] if best else None

    def _run(self, name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        out = self.tools.execute(name, args)
        return (not out.startswith(NOT_DONE), out[len(NOT_DONE) + 2:] if out.startswith(NOT_DONE) else out)

    # --- 3. execution ---

    def _sanity(self, actions: List[Action], message: str) -> List[Action]:
        """Drop or repair actions the message can't support (a small model sometimes invents)."""
        said = {_stem(w) for w in _words(message)}
        refers_back = bool(re.search(r"\b(it|that|those|them|these|this|same|again)\b", message.lower()))
        kept: List[Action] = []
        for a in actions:
            if a.type == "add" and not refers_back:
                title_words = {_stem(w) for w in _words(a.get("title") or "")}
                if title_words and not (title_words & said):
                    logger.info("[SlmBrain] dropping invented add: %s", a.fields)
                    continue
            if a.type == "remove" and self.state.proposal and not self._span_candidates(a.get("target") or ""):
                target = {_stem(w) for w in _words(a.get("target") or "")}
                if any(target & {_stem(w) for w in _words(p["title"])} for p in self.state.proposal):
                    a = Action("reject", {})  # turning down a suggestion, not removing anything
                    if any(k.type == "reject" for k in kept):
                        continue
            if a.type == "move" and not self._span_candidates(a.get("target") or "") and (a.get("start") or a.get("after") or a.get("before")):
                if not any(b.type == "add" for b in actions):
                    a = Action("add", {**a.fields, "title": a.get("target")})
                else:
                    continue
            kept.append(a)
        return kept

    def _execute(self, actions: List[Action], message: str) -> str:
        actions = self._sanity(actions, message)
        if not actions:
            return self._chat(message)
        lines: List[str] = []
        changed = False
        handlers: Dict[str, Callable[[Action], Tuple[str, bool]]] = {
            "add": self._do_add, "move": self._do_move, "remove": self._do_remove, "swap": self._do_swap,
            "outcome": self._do_outcome,
            "confirm": self._do_confirm, "reject": self._do_reject, "choose": self._do_choose,
            "suggest": self._do_suggest, "question": self._do_question, "remember": self._do_remember,
            "profile": self._do_profile, "energy": self._do_energy,
        }
        for action in actions:
            if action.type == "chat":
                if len(actions) == 1:
                    return self._chat(message)
                continue
            text, did_change = handlers[action.type](action)
            if text:
                lines.append(text)
            changed = changed or did_change
        if not lines:
            return self._chat(message)
        # Report what happened first, then ask whatever is still open.
        questions = [l for l in lines if l.rstrip().endswith("?") and "\n" not in l]
        lines = [l for l in lines if l not in questions] + questions
        if changed and not self.state.proposal and not any(l.rstrip().endswith("?") for l in lines):
            free = self._free_summary()
            if free:
                lines.append(free)
        return "\n".join(lines)

    def _free_summary(self) -> str:
        now = self._now() if self._is_today() else None
        windows = [(a, b) for a, b in self.tools.open_windows(after=now) if (b - a) >= timedelta(minutes=30)]
        if not windows:
            return ""
        shown = ", ".join(f"{format_clock(a)} – {format_clock(b)}" for a, b in windows[:3])
        label = "Still free" if now else "Free"
        return f"{label}: {shown}." if len(windows) <= 3 else f"{label}: {shown}, and more later."

    def _day_label(self, day: Optional[str]) -> str:
        return f" ({day})" if day and day.lower() not in ("today", "tonight") else ""

    def _do_add(self, a: Action, alternatives: bool = True) -> Tuple[str, bool]:
        title = a.get("title") or a.get("target")
        if not title:
            return "What should I call it, and when?", False
        day = a.get("day")
        anchor_ref = a.get("after") or a.get("before")
        if not a.get("start") and not anchor_ref:
            self.state.proposal, self.state.proposal_kind = [], ""
            return f"When should I put {title}?", False
        saved = self.tools.day_plan
        try:
            self.tools.day_plan = self.tools.plan_for(day)
            if anchor_ref:
                anchors = self._span_candidates(anchor_ref)
                if len(anchors) != 1:
                    return (f"After which {anchor_ref}?" if anchors else f"I don't see {anchor_ref} on the schedule."), False
                minutes = parse_duration_minutes(a.get("duration") or "30 minutes")
                if a.get("after"):
                    start = anchors[0].end_dt
                else:
                    start = anchors[0].start_dt - timedelta(minutes=minutes)
                end = start + timedelta(minutes=minutes)
            else:
                start = self._loose_time(a.get("start"))
                end = self._loose_time(a.get("end"), after=start) if a.get("end") else None
        except ClockError as e:
            return f"I couldn't read that time: {e}", False
        finally:
            self.tools.day_plan = saved
        args: Dict[str, Any] = {"title": title.strip().capitalize() if title.islower() else title.strip(),
                                "start": self._clock_arg(start)}
        if end:
            args["end"] = self._clock_arg(end)
        else:
            try:
                args["duration"] = f"{parse_duration_minutes(a.get('duration') or '30 minutes')} minutes"
            except ClockError:
                args["duration"] = "30 minutes"
        if day:
            args["day"] = day
        if a.get("alongside"):
            args["alongside"] = True
        if a.get("push"):
            args["push_later"] = True
        task = self._task_for(title)
        if task:
            args["task_id"] = task.id
        ok, out = self._run("add_event", args)
        if ok:
            span = self._span_from_result(out, day)
            self._remember(span)
            text = self._added_line(out)
            slid = re.findall(r"(?:; |\()([^;()]+? now \d[^;()]*?(?:AM|PM) – \d[^;()]*?(?:AM|PM))", out)
            if slid:
                text += "\nTo make room: " + "; ".join(slid) + "."
            return text, True
        if alternatives and "would overlap" in out:
            return self._offer_alternatives(args, out), False
        return f"I couldn't add {args['title']}: {out}", False

    def _span_from_result(self, out: str, day: Optional[str]) -> Optional[EventSpan]:
        m = re.search(r"\[id ([0-9a-f]+)\]", out)
        if not m:
            return None
        plan = self.tools.plan_for(day) if day else self.tools.day_plan
        return plan.get_span(m.group(1))

    def _added_line(self, out: str) -> str:
        day = ""
        m = re.match(r"\[(.+?)\] ", out)
        if m:
            day, out = f" on {m.group(1)}", out[m.end():]
        m = re.match(r"Added (.+?), (.+? – .+?) \((.+?)\)", out)
        if not m:
            return out
        return f"Added {m.group(1)}{day}: {m.group(2)} ({m.group(3)})."

    def _offer_alternatives(self, args: Dict[str, Any], why: str) -> str:
        """The requested time clashes: offer concrete options instead of failing."""
        plan = self.tools.day_plan
        start = self._loose_time(args["start"])
        end = self._loose_time(args["end"], after=start) if "end" in args else \
            start + timedelta(minutes=parse_duration_minutes(args["duration"]))
        length = end - start
        clashes = [s for s in plan.spans_overlapping(start, end) if not s.is_envelope]
        options: List[Dict[str, Any]] = []
        if clashes and clashes[0].start_dt > start + timedelta(minutes=15):
            options.append({**args, "start": format_clock(start), "end": format_clock(clashes[0].start_dt),
                            "duration": None})
        after_last = max(s.end_dt for s in clashes) if clashes else end
        for w_start, w_end in self.tools.open_windows(after=after_last):
            if w_end - w_start >= length:
                options.append({**args, "start": format_clock(w_start), "end": format_clock(w_start + length),
                                "duration": None})
                break
        options.append({**args, "alongside": True})
        options = [{k: v for k, v in o.items() if v is not None} for o in options]
        self.state.proposal, self.state.proposal_kind = options, "options"
        clash_text = ", ".join(f"{s.title} ({format_clock(s.start_dt)} – {format_clock(s.end_dt)})" for s in clashes)
        lines = [f"{args['title']} would overlap {clash_text}. I could:"]
        for i, o in enumerate(options, 1):
            if o.get("alongside"):
                lines.append(f"{i}. keep both at the same time")
            else:
                lines.append(f"{i}. do it {o['start']} – {o['end']}")
        lines.append("Which one?")
        return "\n".join(lines)

    def _resolve_one(self, a: Action) -> Tuple[Optional[EventSpan], str]:
        ref = a.get("target") or a.get("title") or ""
        saved = self.tools.day_plan
        if a.get("day"):
            self.tools.day_plan = self.tools.plan_for(a.get("day"))
        try:
            found = self._span_candidates(ref)
        finally:
            self.tools.day_plan = saved
        if not found:
            return None, f"I don't see {ref or 'that'} on the schedule."
        if len(found) > 1:
            found = sorted(found, key=lambda s: s.start_dt)
            said = getattr(self, "_message", "").lower()
            ordinal = next((v for k, v in self.ORDINALS.items() if re.search(rf"\b{k}\b", said)), None)
            if ordinal is not None and (ordinal == -1 or ordinal < len(found)):
                return found[ordinal], ""
            for text in (ref, said):
                for m in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", text or ""):
                    hour, minute = int(m.group(1)), int(m.group(2) or 0)
                    at_time = [s for s in found if s.start_dt.minute == minute
                               and s.start_dt.hour % 12 == hour % 12]
                    if len(at_time) == 1:
                        return at_time[0], ""
            recent = [s for s in found if s.id in self.state.last_ids]
            if len(recent) == 1:
                return recent[0], ""
            options = "; ".join(f"{s.title} at {format_clock(s.start_dt)}" for s in found[:4])
            return None, f"Which one do you mean: {options}?"
        return found[0], ""

    def _do_move(self, a: Action) -> Tuple[str, bool]:
        span, problem = self._resolve_one(a)
        if span is None:
            return problem, False
        args: Dict[str, Any] = {"event": span.id}
        if a.get("day"):
            args["day"] = a.get("day")
        if a.get("to_day"):
            args["to_day"] = a.get("to_day")
            if a.get("start"):
                target_plan = self.tools.plan_for(a.get("to_day"))
                saved = self.tools.day_plan
                self.tools.day_plan = target_plan
                try:
                    args["start"] = self._clock_arg(self._loose_time(a.get("start"), prefer_future=False,
                                                                      near=target_plan.day_start + timedelta(hours=4)))
                except ClockError as e:
                    return f"I couldn't read that time: {e}", False
                finally:
                    self.tools.day_plan = saved
            ok, out = self._run("update_event", args)
            if not ok:
                return f"I couldn't move {span.title}: {out}", False
            return out.replace("Moved ", "Moved ", 1), True
        if a.get("push"):
            args["push_later"] = True
        anchor_ref = a.get("after") or a.get("before")
        if a.get("shift"):
            args["shift"] = a.get("shift")
        elif anchor_ref:
            anchors = [s for s in self._span_candidates(anchor_ref) if s.id != span.id]
            if len(anchors) != 1:
                return (f"After which {anchor_ref}?" if anchors else f"I don't see {anchor_ref} on the schedule."), False
            length = span.end_dt - span.start_dt
            start = anchors[0].end_dt if a.get("after") else anchors[0].start_dt - length
            args["start"], args["end"] = self._clock_arg(start), self._clock_arg(start + length)
        else:
            try:
                start = self._loose_time(a.get("start"), near=span.start_dt) if a.get("start") else None
                end = self._loose_time(a.get("end"), after=start or span.start_dt) if a.get("end") else None
            except ClockError as e:
                return f"I couldn't read that time: {e}", False
            if start and not end and not a.get("duration") and not span.is_envelope:
                # Moving an activity to a new start keeps its length.
                end = start + (span.end_dt - span.start_dt)
            if start:
                args["start"] = self._clock_arg(start)
            if end:
                args["end"] = self._clock_arg(end)
            if a.get("duration") and not end:
                args["duration"] = a.get("duration")
        if not any(k in args for k in ("shift", "start", "end", "duration")):
            return f"How should I change {span.title}?", False
        ok, out = self._run("update_event", args)
        if not ok:
            return f"I couldn't change {span.title}: {out}", False
        self._remember(span)
        if span.is_envelope and "end" in args and "start" not in args:
            return f"{span.title} now ends at {format_clock(span.end_dt)}.", True
        if span.is_envelope and "start" in args and "end" not in args:
            return f"{span.title} now starts at {format_clock(span.start_dt)}.", True
        text = f"Moved {span.title} to {format_clock(span.start_dt)} – {format_clock(span.end_dt)}."
        slid = re.findall(r"(?:; |\()([^;()]+? now \d[^;()]*?(?:AM|PM) – \d[^;()]*?(?:AM|PM))", out)
        if slid:
            text += "\nTo make room: " + "; ".join(slid) + "."
        return text, True

    def _do_swap(self, a: Action) -> Tuple[str, bool]:
        first, problem = self._resolve_one(Action("swap", {"target": a.get("target"), "day": a.get("day")}))
        if first is None:
            return problem, False
        second, problem = self._resolve_one(Action("swap", {"target": a.get("other"), "day": a.get("day")}))
        if second is None:
            return problem, False
        ok, out = self._run("swap_events", {"first": first.id, "second": second.id,
                                            **({"day": a.get("day")} if a.get("day") else {})})
        if not ok:
            return f"I couldn't swap them: {out}", False
        self._remember(first, second)
        a_, b_ = sorted((first, second), key=lambda s: s.start_dt)
        return (f"Swapped:\n- {format_clock(a_.start_dt)} – {format_clock(a_.end_dt)}: {a_.title}\n"
                f"- {format_clock(b_.start_dt)} – {format_clock(b_.end_dt)}: {b_.title}"), True

    def _do_remove(self, a: Action) -> Tuple[str, bool]:
        span, problem = self._resolve_one(a)
        if span is None:
            return problem, False
        if span.tentative:
            ok, out = self._run("resolve_calendar_event", {"event": span.id, "attend": False})
            return (f"Declined {span.title}." if ok else out), ok
        args = {"event": span.id, **({"day": a.get("day")} if a.get("day") else {})}
        ok, out = self._run("remove_event", args)
        if not ok:
            return f"I couldn't remove {span.title}: {out}", False
        self.state.last_ids = [i for i in self.state.last_ids if i != span.id]
        return f"Removed {span.title} ({format_clock(span.start_dt)} – {format_clock(span.end_dt)}).", True

    def _do_outcome(self, a: Action) -> Tuple[str, bool]:
        ref = a.get("target") or a.get("title") or ""
        found = self._span_candidates(ref) if ref.lower() not in PRONOUNS or self.state.last_ids else []
        found = [s for s in found if not s.is_envelope and s.source != "calendar"]
        event = found[0].id if len(found) == 1 else ref
        args: Dict[str, Any] = {"event": event, "status": a.get("status") or "done"}
        for key in ("started", "finished"):
            if a.get(key):
                try:
                    args[key] = self._clock_arg(self._loose_time(a.get(key), prefer_future=False))
                except ClockError:
                    pass
        if a.get("duration"):
            args["duration"] = a.get("duration")
        if not (args.get("started") and args.get("finished")) and (args.get("started") or args.get("finished")):
            args["estimated"] = True
        ok, out = self._run("log_outcome", args)
        if not ok:
            return f"I couldn't find {ref} to mark it: {out}", False
        status = args["status"]
        name = found[0].title if len(found) == 1 else ref
        if status == "done":
            text = f"Nice work: {name} is done."
            if "checked it off in Joplin" in out:
                text += " Checked off in Joplin too."
        elif status == "partial":
            text = f"Noted that {name} is partly done."
        else:
            text = f"No problem, {name} is back on the list."
            m = re.search(r"put off (\d+) times", out)
            if m and int(m.group(1)) >= 3:
                text += f" That's {m.group(1)} times now. Want to try just 30 minutes of it, break it down, or drop it?"
        return text, True

    def _apply_proposals(self, items: List[Dict[str, Any]]) -> Tuple[str, bool]:
        lines, changed = [], False
        for args in items:
            ok, out = self._run("add_event", args)
            if ok:
                changed = True
                self._remember(self._span_from_result(out, args.get("day")))
                lines.append(self._added_line(out))
            else:
                lines.append(f"Couldn't add {args['title']}: {out}")
        self.state.proposal, self.state.proposal_kind = [], ""
        return "\n".join(lines), changed

    def _do_confirm(self, a: Action) -> Tuple[str, bool]:
        target = {_stem(w) for w in _words(a.get("target") or "")}
        invites = [s for s in self.tools.day_plan.spans_in_view() if s.tentative]
        named = [s for s in invites if target & {_stem(w) for w in _words(s.title)}]
        if named or (invites and not self.state.proposal):
            invite = (named or invites)[0]
            ok, out = self._run("resolve_calendar_event", {"event": invite.id, "attend": True})
            return (f"Great, {invite.title} is confirmed: {format_clock(invite.start_dt)} – "
                    f"{format_clock(invite.end_dt)}." if ok else out), ok
        if not self.state.proposal:
            return "", False
        if self.state.proposal_kind == "options":
            return self._apply_proposals(self.state.proposal[:1])
        return self._apply_proposals(self.state.proposal)

    def _do_reject(self, a: Action) -> Tuple[str, bool]:
        invites = [s for s in self.tools.day_plan.spans_in_view() if s.tentative]
        if not self.state.proposal and invites:
            ok, out = self._run("resolve_calendar_event", {"event": invites[0].id, "attend": False})
            return (f"Okay, declined {invites[0].title}." if ok else out), ok
        self.state.proposal, self.state.proposal_kind = [], ""
        return "No problem, I'll leave those off.", False

    def _do_choose(self, a: Action) -> Tuple[str, bool]:
        n = a.get("option")
        if not self.state.proposal or not isinstance(n, int) or not 1 <= n <= len(self.state.proposal):
            return "Which option did you mean?", False
        return self._apply_proposals([self.state.proposal[n - 1]])

    # --- suggestions ---

    def _window_for(self, topic: str) -> Tuple[datetime, datetime]:
        plan = self.tools.day_plan
        now = self._now()
        start, end = plan.day_start, plan.day_end
        t = (topic or "").lower()
        day = plan.day_start.date()
        tz = plan.day_start.tzinfo
        if any(w in t for w in ("tonight", "evening", "after dinner", "night")):
            start = datetime.combine(day, time(17, 0), tzinfo=tz)
        elif "afternoon" in t:
            start, end = datetime.combine(day, time(12, 0), tzinfo=tz), datetime.combine(day, time(17, 0), tzinfo=tz)
        elif "morning" in t:
            end = datetime.combine(day, time(12, 0), tzinfo=tz)
        if self._is_today():
            start = max(start, now)
        sleep = [s for s in plan.active_spans() if s.category == SpanCategory.SLEEP and s.start_dt > start]
        if sleep:
            end = min(end, sleep[0].start_dt)
        return start, end

    def _score_task(self, task: TaskItem, topic_words: set, goal_words: set, scheduled_titles: set) -> float:
        if task.someday or task.title.lower() in scheduled_titles:
            return -1.0
        words = {_stem(w) for w in _words(f"{task.title} {task.parent_note_title or ''} {task.section}")}
        score = 1.0
        score += 4.0 * len(topic_words & words)
        score += 2.0 * len(goal_words & words)
        score += min(task.deferred_count, 4) * 0.75
        if (task.parent_note_title or "").lower() in ("today to-do", "today"):
            score += 3.0
        if task.parent_task_id:
            score -= 0.5  # prefer whole tasks over sub-steps
        if not task.is_checkbox_item and not task.id.startswith("media:"):
            score -= 0.5  # a whole note ("Assorted Quickies") is usually a bucket, not an action
        if "media" in task.tags or task.id.startswith("media:"):
            score -= 1.5 if not (topic_words & {"watch", "read", "relax", "movi", "show", "book"}) else -1.0
        return score

    def _do_suggest(self, a: Action) -> Tuple[str, bool]:
        topic = a.get("topic") or ""
        start, end = self._window_for(topic)
        windows = [(max(s, start), min(e, end)) for s, e in self.tools.open_windows(after=start, before=end)]
        windows = [(s, e) for s, e in windows if e - s >= timedelta(minutes=30)]
        if not windows:
            return "There's no open time left in that stretch. Want me to look at tomorrow?", False
        profile = self.tools.services.profile.sections() if self.tools.services else {}
        goal_words = {_stem(w) for w in _words(" ".join(profile.get(k, "") for k in ("Near-term goals", "Long-term goals")))}
        time_words = {"tonight", "evening", "afternoon", "morning", "today", "something", "night", "now", "later",
                      "rest", "day", "next", "couple", "hours", "hour", "few", "what", "should", "suggest", "ideas", "idea"}
        topic_words = {_stem(w) for w in _words(topic) if w not in time_words}
        scheduled = {s.title.lower() for s in self.tools.day_plan.spans}
        scored = sorted(((self._score_task(t, topic_words, goal_words, scheduled), i, t) for i, t in enumerate(self.tasks)),
                        key=lambda x: (-x[0], x[1]))
        ranked, seen_notes = [], set()
        for score, _, t in scored:
            if score <= 0 or t.parent_task_id:
                continue
            note = t.parent_note_id or t.source_notebook
            if note in seen_notes:
                continue
            seen_notes.add(note)
            ranked.append(t)
            if len(ranked) >= 12:
                break
        ranked = self._pick(ranked, topic, windows) or ranked[:3]
        try:
            total = parse_duration_minutes(a.get("duration")) if a.get("duration") else None
        except ClockError:
            total = None
        want = None
        proposal: List[Dict[str, Any]] = []
        cursor_windows = list(windows)
        budget = total
        for task in ranked:
            minutes = want or max(30, 30 * (task.estimated_thirties or 1))
            if budget is not None:
                if budget < 30:
                    break
                minutes = min(minutes, budget)
                budget -= minutes
            for i, (ws, we) in enumerate(cursor_windows):
                slot_start = ws if ws.minute in (0, 30) and ws.second == 0 else \
                    ws.replace(minute=0 if ws.minute == 0 else 30, second=0, microsecond=0) + (timedelta(minutes=30) if ws.minute not in (0, 30) else timedelta())
                if we - slot_start >= timedelta(minutes=minutes):
                    proposal.append({"title": task.title, "start": format_clock(slot_start),
                                     "duration": f"{minutes} minutes", "task_id": task.id})
                    cursor_windows[i] = (slot_start + timedelta(minutes=minutes), we)
                    break
            if len(proposal) >= 3:
                break
        if not proposal:
            return "I don't have a good backlog item for that. What are you in the mood for?", False
        self.state.proposal, self.state.proposal_kind = proposal, "plan"
        lines = ["How about:"]
        for i, p in enumerate(proposal, 1):
            p_start = self._loose_time(p["start"])
            p_end = p_start + timedelta(minutes=parse_duration_minutes(p["duration"]))
            task = next((t for t in self.tasks if t.id == p["task_id"]), None)
            why = f" (put off {task.deferred_count}x)" if task and task.deferred_count >= 2 else ""
            lines.append(f"{i}. {format_clock(p_start)} – {format_clock(p_end)}: {p['title']}{why}")
        lines.append("Want all of these, just one (say the number), or something else?")
        return "\n".join(lines), False

    def _pick(self, candidates: List[TaskItem], topic: str, windows) -> List[TaskItem]:
        """Let the model choose the best few candidates for the moment (one small decision)."""
        if len(candidates) <= 3:
            return candidates
        now = self._now()
        lines = [f"{i}. {t.title} [{t.parent_note_title or t.source_notebook}"
                 f"{', put off ' + str(t.deferred_count) + 'x' if t.deferred_count else ''}]"
                 for i, t in enumerate(candidates, 1)]
        energy = ""
        if self.tools.services:
            recent = self.tools.services.energy.entries(since=now - timedelta(hours=4))
            if recent:
                energy = f"Energy: {recent[-1]['level']}/5. "
        goals = ""
        if self.tools.services:
            prof = self.tools.services.profile.sections()
            goals = " ".join(prof.get(k, "") for k in ("Near-term goals",)).strip()
        user_line = next((m["content"] for m in reversed(self.messages) if m.get("role") == "user"), topic)
        context = (f"Now: {now.strftime('%A')} {format_clock(now)}. Open: "
                   + ", ".join(f"{format_clock(a)}-{format_clock(b)}" for a, b in windows[:3])
                   + f". {energy}{'Goals: ' + goals[:200] + '. ' if goals else ''}\nThey said: \"{user_line[:300]}\"\n"
                   + "Candidates:\n" + "\n".join(lines))
        try:
            raw = extract_json(self.picker.ask(context)) or {}
        except Exception as e:
            logger.warning("[SlmBrain] picker failed: %s", e)
            return []
        picks = [candidates[i - 1] for i in raw.get("picks", []) if isinstance(i, int) and 1 <= i <= len(candidates)]
        unique: List[TaskItem] = []
        for t in picks:
            if all(u.id != t.id for u in unique):
                unique.append(t)
        return unique[:3]

    # --- questions, memory, profile, energy ---

    def _do_question(self, a: Action) -> Tuple[str, bool]:
        topic = (a.get("topic") or "").lower()
        day = a.get("day")
        if "week" in topic:
            ok, out = self._run("week_overview", {})
            return "Your week:\n" + "\n".join(f"- {l}" for l in out.splitlines()), False
        if any(w in topic for w in ("free", "open", "available", "time left", "packed", "busy", "room")):
            saved = self.tools.day_plan
            try:
                self.tools.day_plan = self.tools.plan_for(day)
                start, end = self._window_for(topic) if not day else (self.tools.day_plan.day_start, self.tools.day_plan.day_end)
                windows = [(a, b) for a, b in self.tools.open_windows(after=start, before=end) if b - a >= timedelta(minutes=15)]
            finally:
                self.tools.day_plan = saved
            if not windows:
                return "You're fully booked for that stretch; nothing is open.", False

            def length(a, b) -> str:
                m = int((b - a).total_seconds() // 60)
                h, mm = divmod(m, 60)
                return (f"{h} hour{'s' if h != 1 else ''}" if h else "") + (" and " if h and mm else "") + (f"{mm} minutes" if mm else "")
            if len(windows) == 1:
                a, b = windows[0]
                return f"You're free from {format_clock(a)} to {format_clock(b)} ({length(a, b)}).", False
            return "You're free:\n" + "\n".join(f"- {format_clock(a)} – {format_clock(b)} ({length(a, b)})" for a, b in windows), False
        if any(w in topic for w in ("task", "backlog", "todo", "to-do", "list")):
            rest = " ".join(w for w in _words(topic) if w not in ("task", "tasks", "backlog", "todo", "list", "my"))
            if rest:
                ok, out = self._run("search_backlog", {"query": rest})
                return re.sub(r" \[.*?\]| task_id=\S+", "", out), False
            top = [t for t in self.tasks if not t.someday][:5]
            return "Some open items:\n" + "\n".join(f"- {t.title}" for t in top), False
        if any(w in topic for w in ("talked", "said", "last week", "yesterday we", "earlier")):
            ok, out = self._run("search_history", {"query": topic})
            return out, False
        args = {"day": day} if day else {}
        ok, out = self._run("list_schedule", args)
        lines = [l for l in out.splitlines() if l.startswith("- ") and "nothing yet" not in l]
        cleaned = [re.sub(r" \(\d.*?\) \[.*?\] id=\S+$", "", l) for l in lines]
        head = f"Here's {day or 'today'}:"
        free = next((l for l in out.splitlines() if l.startswith("Free")), "")
        return "\n".join([head] + cleaned + ([free] if free else [])), False

    def _do_remember(self, a: Action) -> Tuple[str, bool]:
        text = a.get("text") or a.get("topic")
        if not text:
            return "", False
        self._run("remember", {"note": text})
        return "Got it, I'll remember that.", False

    def _do_profile(self, a: Action) -> Tuple[str, bool]:
        text = a.get("text") or ""
        if not text:
            return "", False
        lowered = text.lower()
        section = ("Long-term goals" if any(w in lowered for w in ("year", "someday", "life", "long-term", "career"))
                   else "Near-term goals" if any(w in lowered for w in ("want to", "goal", "by ", "finish", "trying to"))
                   else "Habits & routines" if any(w in lowered for w in ("usually", "every", "always", "habit", "routine"))
                   else "Preferences")
        self._run("update_profile", {"section": section, "text": text})
        return f"Noted in your profile under {section}.", False

    def _do_energy(self, a: Action) -> Tuple[str, bool]:
        level = a.get("level")
        if not isinstance(level, int):
            return "", False
        self._run("log_energy", {"level": level})
        if level <= 2:
            return "Noted that you're low on energy. Want me to lighten the rest of today?", False
        return "Noted.", False

    def _chat(self, message: str) -> str:
        lowered = message.lower().strip(" !.")
        if lowered in ("thanks", "thank you", "thx", "ty", "cool", "great", "perfect", "ok", "okay", "sounds good"):
            return "You're welcome!" if lowered.startswith("than") or lowered in ("thx", "ty") else "Great."
        try:
            return self.runtime.text_call(CHAT_SYSTEM, message) or "Got it."
        except Exception:
            return "Got it."
