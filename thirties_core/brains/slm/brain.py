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
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from thirties_core.assistant_tools import _stem, _words, rank_tasks
from thirties_core.astronomy import get_current_time
from thirties_core.brains.slm.runtime import SlmUnavailable
from thirties_core.brains.slm.parse import (ACTION_SCHEMA, BREAKDOWN_PROMPT, BREAKDOWN_SCHEMA, PICKER_PROMPT, SYSTEM_PROMPT, VOICE_PROMPT, Action,
                                            build_context, extract_json, parse_actions)
from thirties_core.clock import (ClockError, agenda_lines, format_clock, parse_clock, parse_duration_minutes,
                                 thirty_ceil, thirty_of)
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

logger = logging.getLogger(__name__)

PRONOUNS = {"it", "that", "this", "them", "those", "the thing", "that one", "this one"}
ENVELOPE_WORDS = {"work": SpanCategory.WORK, "job": SpanCategory.WORK, "shift": SpanCategory.WORK,
                  "sleep": SpanCategory.SLEEP, "bed": SpanCategory.SLEEP, "bedtime": SpanCategory.SLEEP}
CHAT_SYSTEM = ("You are Thirties, a warm, concise personal planning assistant that can see and change the user's "
               "schedule. Reply in one or two short sentences. Never say you lack access to their schedule. Do not "
               "claim to have changed anything and do not invent plans; if they seem to want a change or information, "
               "ask them to say it plainly.")


_VOCAB = ("work", "dinner", "lunch", "breakfast", "meditation", "podcast", "audiobook", "laundry", "walk", "reading",
          "composing", "piano", "guitar", "practice", "shower", "chores", "groceries", "grocery", "exercise", "workout",
          "meeting", "email", "call", "appointment", "doctor", "dentist", "homework", "cleaning", "cooking")


def _close_words(words: List[str]) -> List[str]:
    """Stems of common planning words that the message misspells ("wokr", "podcsat")."""
    import difflib
    out = []
    for w in words:
        if len(w) >= 4:
            out += [_stem(v) for v in difflib.get_close_matches(w, _VOCAB, n=1, cutoff=0.75)]
    return out


def _fuzzy_overlap(title: str, message: str) -> bool:
    """Some word of the title appears in the message, allowing for typos."""
    import difflib
    said = [w for w in _words(message) if len(w) >= 4]
    return any(difflib.get_close_matches(w, said, n=1, cutoff=0.75) for w in _words(title) if len(w) >= 4)


@dataclass
class DialogState:
    last_ids: List[str] = field(default_factory=list)       # what "it" refers to
    proposal: List[Dict[str, Any]] = field(default_factory=list)  # add_event argument dicts awaiting yes/no
    proposal_kind: str = ""                                  # "plan" (all) or "options" (pick one)
    awaiting_time: Dict[str, Any] = field(default_factory=dict)  # an add we asked "when?" about

    def to_dict(self) -> Dict[str, Any]:
        return {"last_ids": self.last_ids, "proposal": self.proposal, "proposal_kind": self.proposal_kind,
                "awaiting_time": self.awaiting_time}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DialogState":
        return cls(list(data.get("last_ids", [])), list(data.get("proposal", [])), data.get("proposal_kind", ""),
                   dict(data.get("awaiting_time", {})))


REDRAW_RE = re.compile(r"\b(re-?plan|redraw|re-?do (the|my) (plan|day|schedule)|push (everything|it all|the rest)"
                       r"( else)? (back|later)|just figure it out|undo the (re-?plan|redraw))\b", re.I)
NOT_RE = re.compile(r"\b(don'?t|do not|no need to|not|never|can'?t|won'?t)\b([^\w.!?]+\w+){0,3}[^\w.!?]+"
                    r"(re-?plan|redraw|re-?do|push|just figure)", re.I)  # within one sentence


MAX_MESSAGE_CHARS = 2000
AGAIN_RE = re.compile(r"\b(again|another|twice|second (?:session|time|one))\b", re.IGNORECASE)
FIGURE_IT_OUT_RE = re.compile(r"\b(figure (it|that|them) out|wherever|you (pick|choose|decide)|whenever works|"
                              r"lay (it|them|the day|everything) out|fit (it|them) in)\b", re.I)
BUILD_PLAN_RE = re.compile(r"\b(lay (it |them |the day |everything )?out|put (down|in)|add|schedule|plan (out )?(the|my) "
                           r"(day|morning|afternoon|evening)|include|whole (day|proposed day))\b", re.I)
TODAY_LIST_RE = re.compile(r"\b(?:add|put)\s+(?P<items>.+?)\s+(?:on|to)\s+(?:my\s+|the\s+)?today'?s?(?:\s+list|\s+note)?\b",
                           re.I)
URGENCY_RE = re.compile(r"^(?:the\s+)?(?P<what>[^.?!]{3,60}?)\s+(?P<how>is urgent|is pressing|isn'?t urgent|is not urgent|"
                        r"can wait|is no rush|is a someday thing|has to happen by \w+|needs to happen by \w+|"
                        r"is due by \w+|by \w+ at the latest)\b", re.I)
STARTING_RE = re.compile(r"\b(starting|about to|heading (out|to|into)|going to start|getting started|now doing|"
                         r"on my way|beginning|jumping (in|into)|hopping (in|into))\b", re.I)
FINISHED_RE = re.compile(r"\b(done|finished|did|completed|wrapped up|got through|ate|fed|made|sent|submitted|"
                         r"placed|ordered|picked up|got (it|back|home)|back from|returned|is in|are in|went)\b", re.I)
DAY_WORD_RE = re.compile(r"\b(tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
LEFT_TODAY_RE = re.compile(r"\b(left|remaining|still (to do|got|have|need)|what else)\b", re.I)


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
        if "\n" in draft.strip() or len(draft) > 180:
            return draft  # long replies cost ~15s to rewrite on an iGPU; their template is clear enough
        try:
            out = self.voice.ask(f'User said: "{user_text[:300]}"\nDRAFT:\n{draft}').strip().strip('"')
        except Exception as e:
            logger.warning("[SlmBrain] voice pass failed: %s", e)
            return draft
        if not out or len(out) > 2 * len(draft) + 120:
            return draft
        def norm(times):
            return {t.replace(" ", "").upper() for t in times}
        required = allowed = norm(self._TIME_RE.findall(draft))
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
        user_text = user_text[:MAX_MESSAGE_CHARS]  # a huge paste can't fit Gemma's context: read the start of it
        self._message = user_text
        shortcut = self._list_shortcut(user_text)
        if shortcut is not None:  # today's list and urgency: plain enough for Python, no parse needed
            self.messages.append({"role": "assistant", "content": shortcut, "slm_state": self.state.to_dict(),
                                  "slm_actions": [{"type": "list_shortcut"}]})
            return shortcut
        if REDRAW_RE.search(user_text) and not NOT_RE.search(user_text) and not BUILD_PLAN_RE.search(user_text):
            # rearrange what's planned: Python does it, no parse. Asking to lay out a day goes to the parse instead
            undo = bool(re.search(r"\bundo\b|\bput (it|everything) back\b", user_text, re.I))
            ok, reply = self._run("undo_redraw" if undo else "redraw", {})
            if undo or (ok and not reply.startswith("Nothing to redraw")):
                self.messages.append({"role": "assistant", "content": reply, "slm_state": self.state.to_dict(),
                                      "slm_actions": [{"type": "undo_redraw" if undo else "redraw"}]})
                return reply
        try:
            actions = self._parse(user_text)
        except SlmUnavailable as e:
            reply = f"Gemma can't run yet: {e} Until then, open Preferences → Assistant to set it up."
            self.messages.append({"role": "assistant", "content": reply})
            return reply
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
            if self._is_today() and s.end_dt <= now - timedelta(hours=1) and not s.is_envelope:
                continue
            note = " (unconfirmed invite)" if s.tentative else ""
            lines.append(f"{s.title} {format_clock(s.start_dt)}-{format_clock(s.end_dt)}{note}")
        return lines[:10]

    def _last_assistant(self) -> str:
        for m in reversed(self.messages[:-1]):
            if m.get("role") == "assistant" and m.get("kind") != "notice":  # not a start-of-block notice
                return str(m.get("content", ""))
        return ""

    def _pending_text(self) -> Optional[str]:
        if self.state.awaiting_time:
            return f"a time for {self.state.awaiting_time.get('title')} (answer with an add action)"
        if not self.state.proposal:
            return None
        if self.state.proposal_kind == "breakdown":
            return f"whether to add these steps to Joplin under {self.state.proposal[0]['title']}"
        items = "; ".join(f"{p['title']} at {p['start']}" if p.get("start") else p["title"] for p in self.state.proposal)
        kind = "pick one of" if self.state.proposal_kind == "options" else "whether to add"
        return f"{kind}: {items}"

    def _parse(self, message: str) -> List[Action]:
        now = self._now()
        related = [t.title for t in rank_tasks([t for t in self.tasks if not t.someday], message, limit=3)]
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
                    near: Optional[datetime] = None, recent_past: bool = False) -> datetime:
        """A clock time on the planned day; adds AM/PM sensibly when the user left it out."""
        plan = self.tools.day_plan
        raw = str(text).strip().lower().replace("in the ", "").replace("this ", "")
        raw = re.sub(r"^(later|sometime|some time|around|about|by)\s+", "", raw).strip()
        raw = {"later morning": "late morning"}.get(raw, raw)
        named = {"early morning": "7:00", "morning": "9:00", "mid morning": "10:00", "mid-morning": "10:00",
                 "late morning": "10:30", "lunch": "12:00", "lunchtime": "12:00", "midday": "12:00",
                 "early afternoon": "13:00", "afternoon": "14:00", "mid afternoon": "15:00", "mid-afternoon": "15:00",
                 "late afternoon": "16:30", "evening": "18:00", "early evening": "17:30", "late evening": "21:00",
                 "tonight": "19:00", "night": "20:00", "bedtime": "22:00"}
        if raw in named:
            raw = named[raw]
        elif raw in ("after work", "when work ends", "after my shift"):
            work = next((s for s in self.tools.day_plan.active_spans() if s.category == SpanCategory.WORK), None)
            if work:
                return work.end_dt
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
        if recent_past and self._is_today():  # "finished at 6:30", said at 7 PM: 6:30 PM, not this morning
            now = self._now()
            past = [dt for dt in candidates if dt <= now + timedelta(minutes=15)]
            if past:
                return max(past)
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

    @staticmethod
    def _humane(error: str) -> str:
        """Tool errors are written for models; keep the fact, drop the instructions and ids."""
        error = re.sub(r"\s*\[id [0-9a-z:@.-]+\]", "", error)
        error = re.sub(r"\s*Ask the user, then retry.*$", "", error)
        error = re.sub(r"\s*\(\d+h(?: \d+m)?\)|\s*\(\d+m\)", "", error)
        return error.strip()

    def _run(self, name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        out = self.tools.execute(name, args)
        if out.startswith(NOT_DONE):
            return False, self._humane(out[len(NOT_DONE) + 2:])
        return True, out

    # --- 3. execution ---

    _LENGTH_ONLY_RE = re.compile(r"(?:for\s+)?(?:an?|one|two|three|four|half an?|\d+(?:\.\d+)?)\s*"
                                 r"(?:hours?|hrs?|minutes?|mins?|chunks?|thirties)(?:\s+long)?", re.IGNORECASE)

    def _sanity(self, actions: List[Action], message: str) -> List[Action]:
        """Drop or repair actions the message can't support (a small model sometimes invents)."""
        said = {_stem(w) for w in _words(message)}
        said |= {w for w in _close_words(_words(message))}  # "meditatoin" still says meditation
        refers_back = bool(re.search(r"\b(it|that|those|them|these|this|same|again)\b", message.lower()))
        lowered = message.lower()
        slide = bool(re.search(r"\b(slide|push|shift|bump|move)\b.{0,30}\b(everything|the rest|all|things|what'?s)\b.{0,20}\b(after|later|back|down)\b"
                               r"|\beverything (after|else) (it |that )?(slides?|moves?|gets pushed)", lowered))
        mentions_sleep = bool(re.search(r"\b(sleep|bed|bedtime|wake|asleep)\b", lowered))
        mentions_work = bool(re.search(r"\b(work|shift|job|office)\b", lowered)) or "work" in said
        starts_no = bool(self._NO_RE.search(lowered)) and \
            not re.search(r"\b(actually|wait|on second thought)\b.*\b(yes|yeah|go ahead|do it|sure|ok)\b", lowered)
        day_off = bool(re.search(r"\bday off\b|\boff (work|today)\b|\b(pto|vacation|sick day|calling in sick)\b", lowered))
        clear_all = bool(re.search(r"\b(everything|all of it|clear (the|my) (day|schedule|evening|afternoon|morning))\b", lowered))
        again = bool(re.search(r"\b(another|again|second|more|extra|round|too)\b", lowered))
        if slide and any(a.type in ("add", "move") and a.get("start") for a in actions):
            for a in actions:
                if a.type in ("add", "move") and a.get("start") and not a.get("shift"):
                    a.fields["push"] = True
            # The model often spells "slide" as shifting each later item; the push does that properly.
            actions = [a for a in actions if not (a.type == "move" and a.get("shift"))]
        kept: List[Action] = []
        for a in actions:
            if a.type in ("add", "move"):
                bare = re.fullmatch(r"\s*(\d{1,2}(?::\d{2})?)\s*(am|pm|a\.m\.|p\.m\.)?\s*", str(a.get("duration") or ""), re.IGNORECASE)
                if bare and (":" in bare.group(1) or bare.group(2) or int(bare.group(1)) <= 12):
                    a.fields.pop("duration")
                    if not a.get("start") and not a.get("shift"):
                        a.fields["start"] = bare.group(0).strip()  # "move scales to 3" is 3 o'clock, not 3 minutes
            if starts_no and a.type == "confirm":
                a = Action("reject", {})  # "nah, don't do that" is never a yes
            elif starts_no and a.type == "outcome" and a.get("status") == "skipped" and \
                    not self._span_candidates(a.get("target") or ""):
                a = Action("reject", {})  # "nah" to a suggestion, not a report about the day
            if a.type == "outcome":
                for wrong, right in (("start", "started"), ("end", "finished")):
                    if a.get(wrong) and not a.get(right):
                        a.fields[right] = a.fields.pop(wrong)
            if a.type in ("add", "move"):
                m = re.match(r"^\s*(before|after|right before|right after|just before|just after)\s+(.+)$",
                             str(a.get("start") or ""), re.IGNORECASE)
                if m and not re.search(r"\d", m.group(2)):
                    a.fields.pop("start")
                    a.fields[m.group(1).split()[-1].lower()] = m.group(2)
            shift = str(a.get("shift") or "").strip()
            if a.type == "move" and shift[:1] in "+-" and shift:
                later = bool(re.search(r"\b(back|later|push|delay|bump)\b", lowered))
                earlier = bool(re.search(r"\b(earlier|sooner|forward|up)\b", lowered))
                if shift[0] == "-" and later and not earlier:
                    a.fields["shift"] = "+" + shift[1:]
                elif shift[0] == "+" and earlier and not later:
                    a.fields["shift"] = "-" + shift[1:]
            if a.type == "remove" and not refers_back and not clear_all:
                target_words = {_stem(w) for w in _words(a.get("target") or "")}
                cats = {s.category for s in self._span_candidates(a.get("target") or "")}
                if not (target_words & said) and not (SpanCategory.WORK in cats and day_off):
                    logger.info("[SlmBrain] dropping a removal the user didn't mention: %s", a.fields)
                    continue
            placed = any(a.get(k) for k in ("start", "end", "after", "before", "duration"))
            if a.type == "add" and placed and not again and not a.get("day") and not a.get("alongside"):
                same = [s for s in self.tools.day_plan.spans_in_view()
                        if not s.is_envelope and s.title.lower() == (a.get("title") or "").strip().lower()]
                if len(same) == 1:  # "put composing after dinner" when Composing is already planned: move it
                    a = Action("move", {**{k: v for k, v in a.fields.items() if k != "title"}, "target": same[0].title})
            if a.type == "add" and self._LENGTH_ONLY_RE.fullmatch((a.get("title") or "").strip()) and not a.get("start"):
                a = Action("move", {"target": "it", "duration": re.sub(r"^for\s+", "", a.get("title").strip(), flags=re.I)})
            target_cats = {s.category for s in self._span_candidates(a.get("target") or "")} if a.type in ("move", "remove") else set()
            if SpanCategory.SLEEP in target_cats and not mentions_sleep:
                logger.info("[SlmBrain] ignoring a sleep change the user didn't ask for: %s", a.fields)
                continue
            if SpanCategory.WORK in target_cats and not mentions_work and a.type == "move" and a.get("shift"):
                continue
            duration = str(a.get("duration") or "")
            m = re.match(r"^(?:until|til|till|to|ending at|ends at)\s+(.+)$", duration, re.IGNORECASE)
            if m:
                a.fields.pop("duration", None)
                a.fields.setdefault("end", m.group(1))
            m = re.match(r"^(?:from\s+)?(.+?)\s*(?:-|–|to|until)\s*(.+)$", str(a.get("start") or ""), re.IGNORECASE)
            if m and re.search(r"\d", m.group(2)) and not a.get("end"):
                a.fields["start"], a.fields["end"] = m.group(1), m.group(2)
            if a.type == "add" and not refers_back:
                title_words = {_stem(w) for w in _words(a.get("title") or "")}
                if title_words and not (title_words & said) and not _fuzzy_overlap(a.get("title") or "", message):
                    logger.info("[SlmBrain] dropping invented add: %s", a.fields)
                    continue
            if a.type == "remove" and self.state.proposal and not self._span_candidates(a.get("target") or ""):
                target = {_stem(w) for w in _words(a.get("target") or "")}
                if any(target & {_stem(w) for w in _words(p["title"])} for p in self.state.proposal):
                    a = Action("reject", {})  # turning down a suggestion, not removing anything
                    if any(k.type == "reject" for k in kept):
                        continue
            if a.type == "move" and not self._span_candidates(a.get("target") or "") and \
                    not (a.get("day") or self._find_on_other_day(a.get("target") or "")[0]) and \
                    (a.get("start") or a.get("after") or a.get("before")):
                same = {_stem(w) for w in _words(a.get("target") or "")}
                if any(b.type == "add" and same & {_stem(w) for w in _words(b.get("title") or "")} for b in actions):
                    continue  # the add in this message already covers it
                fields = {k: v for k, v in a.fields.items() if k not in ("target", "to_day")}
                if a.get("to_day"):
                    fields["day"] = a.get("to_day")
                a = Action("add", {**fields, "title": a.get("target")})
            kept.append(a)
        return kept

    _SHOW_DAY_RE = re.compile(r"\b(show|list|lay out|layout|see|recap|overview|summar\w*|what'?s on|whole day|my day|"
                              r"schedule look)\b.*\b(day|schedule|plan|today|tonight|tomorrow|list)\b|"
                              r"\b(clean|full|whole) (list|day|schedule)\b", re.IGNORECASE)

    _TIME_IN_TEXT = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|noon|now|right now)\b", re.IGNORECASE)

    _YES_RE = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|sounds good|perfect|great|do it|go for it|please do|"
                         r"all of them|all of these|both|let'?s do (it|that|those)|yes please)\b", re.IGNORECASE)
    _ALL_YES_RE = re.compile(r"\b(let'?s do (all|them|those|these|it|that)|do (them|those|these) all|all of (them|those|these)|"
                             r"(those|these|that|it) (all )?(sounds?|looks?) (good|great|fine|perfect)|sounds? good|looks? good|"
                             r"works for me|(those|these) work)\b", re.IGNORECASE)
    _NO_RE = re.compile(r"^\s*(no+(?! (problem|worries|rush|biggie|doubt))|nah+|nope|negative|not really|neither|"
                        r"none of (those|them|these)|pass|skip|i'?ll pass)\b", re.IGNORECASE)
    _ORDINAL_WORDS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3}

    # Handing the planning over: "fill up my afternoon", "you pick", "where could it go?"
    _FILL_RE = re.compile(r"\b(fill|plan out|line up|load up|put together|block out|pack)\b.{0,50}"
                          r"\b(afternoon|morning|evening|tonight|day|rest|time|schedule|until|til|till|hours?|gap|window|"
                          r"chunks?|thirties|block|slot|from|between)\b"
                          r"|\bfill (it|that|the rest|in)\b",
                          re.IGNORECASE)
    _PICK_RE = re.compile(r"\b(you pick|just pick|pick (for me|whatever|a few|some|any|something)|whatever (you think|makes sense|works)|"
                          r"up to you|your call|surprise me|you decide|you choose|dealer'?s choice)\b", re.IGNORECASE)
    _PLACE_RE = re.compile(r"\bwhere (could|can|should|would|might|will|does|do) [\w' ]{1,30}? ?(go|fit|put|squeeze)\b|"
                           r"\bwhen (can|could|should) (i|we) (fit|do|put|squeeze|schedule)\b|"
                           r"\bwhat times? (are|is|do i have) (free|open)\b", re.IGNORECASE)
    _UNTIL_RE = re.compile(r"\b(?:until|til|till|up to|before)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|dinner|bed(?:time)?)\b",
                           re.IGNORECASE)
    _T = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|noon"
    _SPAN_RE = re.compile(rf"\b(?:from|between)\s+({_T})\s*(?:to|until|til|till|and|-|–)\s*({_T})\b", re.IGNORECASE)
    _GENERIC_TITLE_RE = re.compile(r"^(?:some |my )?(?:backlog|tasks?|items?|stuff|things|something|todos?|to-dos?)"
                                   r"(?: (?:tasks?|items?|stuff|things))?$", re.IGNORECASE)
    _LENGTH_RE = re.compile(r"\b(\d+(?:\.\d+)?|an?|one|two|three|four|five|six)\s+(chunks?|thirties|hours?|hrs?|minutes?|mins?)\b",
                            re.IGNORECASE)
    _PARTS = ("morning", "afternoon", "evening", "tonight")

    _ADD_TO_LIST_RE = re.compile(r"\b(?:add|put|throw|stick|write|jot)\s+(?:down\s+)?(?P<task>.+?)\s+(?:to|on|onto|in|into)\s+"
                                 r"(?:my\s+|the\s+)?(?P<list>[\w/&' -]+?)\s+(?:list|note|backlog|to-?do(?:s| list)?)\b",
                                 re.IGNORECASE)
    # "make sure that's on my errands list too"
    _ON_LIST_RE = re.compile(r"\b(?P<task>it|that|this)(?:'s| is)?\s+(?:also\s+)?(?:on|in|onto|into)\s+(?:my\s+|the\s+)?"
                             r"(?P<list>[\w/&' -]+?)\s+(?:list|note|backlog|to-?do(?:s| list)?)\b", re.IGNORECASE)
    _BREAKDOWN_RE = re.compile(r"\bbreak\s+(?:(?P<what>.+?)\s+)?(?:down|up|apart)\b|\b(?:too (?:big|much|hard|daunting|overwhelming)"
                               r"|hard to (?:start|get started)|can'?t (?:get )?start(?:ed)?|don'?t know where to start"
                               r"|smaller (?:steps|parts|pieces|chunks|bits))\b", re.IGNORECASE)

    _REMIND_RE = re.compile(r"\b(?:remind|ping|nudge|text|message) me\b", re.IGNORECASE)
    _MUTE_RE = re.compile(r"\b(?:no need to|don'?t|do not|stop|quit|no more)\s+(?:keep\s+|keeping\s+)?(?:on\s+)?"
                          r"(?:ask(?:ing)?|check(?:ing)?[ -]?in(?:s|g)?|remind(?:ing)?|bug(?:ging)?|nag(?:ging)?|"
                          r"pester(?:ing)?|message(?:s)?)\b", re.IGNORECASE)
    _WEEKDAY_RE = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)"

    def _reminder_from(self, message: str) -> Optional[Action]:
        lowered = message.lower()
        m = self._REMIND_RE.search(lowered)
        if not m or re.match(r"\s*(?:me\s+)?(?:what|how|when|where|which|who|why|if|whether|of what|about what)\b",
                             lowered[m.end():]):
            return None  # "remind me what we discussed" is a question about the past
        rest = message[m.end():]
        day_m = re.search(rf"\b(today|tonight|tomorrow|next week|(?:next |this |on )?{self._WEEKDAY_RE}|"
                          r"(?:on )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2})\b", rest, re.I)
        time_m = re.search(r"\bin (\d+|an?|half an) (minutes?|mins?|hours?|hrs?)\b|"
                           r"\b(?:at|around|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|noon)\b|"
                           r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", rest, re.I)
        fields: Dict[str, Any] = {}
        cut = []
        if day_m:
            fields["date"] = re.sub(r"^(on|this) ", "", day_m.group(1).lower()).replace("tonight", "today")
            cut.append(day_m.group(0))
            if day_m.group(1).lower() == "tonight" and not time_m:
                fields["time"] = "7:00 PM"
        if time_m:
            cut.append(time_m.group(0))
            if time_m.group(1):
                fields["time"] = f"in {time_m.group(1)} {time_m.group(2)}"
            else:
                clock = (time_m.group(3) or time_m.group(4)).strip().lower()
                if clock != "noon" and not re.search(r"[ap]\.?m", clock):
                    hour = int(clock.split(":")[0])
                    clock += " am" if 7 <= hour <= 11 else " pm"
                fields["time"] = clock
        text = rest
        for c in cut:
            text = text.replace(c, " ")
        text = re.sub(r"^\s*(?:to|about|that|of)\s+", "", re.sub(r"\s+", " ", text).strip(" ,.!?"), flags=re.I)
        text = re.sub(r"\s+(?:to|about|on|at)$", "", text).strip(" ,.!?")
        text = re.sub(r"\s+(?:sometime|some time|at some point|later|soon|please)$", "", text, flags=re.I).strip(" ,.!?")
        fields["text"] = text[:1].upper() + text[1:] if text else "Reminder"
        return Action("remind", fields)

    _SWAP_OUT_RE = re.compile(r"\b(swap|replace|switch out|instead|not feeling|skip|drop|rather not|don'?t want|"
                              r"something else|other than|not in the mood|not today)\b", re.IGNORECASE)

    def _proposal_swap(self, message: str) -> Optional[Action]:
        """'Looks good, but swap the unpacking for something else': change one item of a pending plan."""
        if self.state.proposal_kind != "plan" or not self.state.proposal or not self._SWAP_OUT_RE.search(message):
            return None
        filler = {"looks", "good", "great", "fine", "feeling", "today", "swap", "something", "else", "instead",
                  "replace", "skip", "drop", "rather", "want", "mood", "that", "this", "with", "other", "than"}
        said = {_stem(w) for w in _words(message) if len(w) > 3 and w not in filler}
        named = [i for i, p in enumerate(self.state.proposal)
                 if said & {_stem(w) for w in _words(p["title"]) if len(w) > 3}]
        m = re.search(r"\b(?:number|#|item)\s*(\d)\b|\bthe (first|second|third|fourth|fifth|last) one\b", message.lower())
        if m and not named:
            words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": len(self.state.proposal)}
            n = int(m.group(1)) if m.group(1) else words[m.group(2)]
            named = [n - 1] if 0 < n <= len(self.state.proposal) else []
        return Action("proposal_swap", {"indexes": named}) if named else None

    def _route(self, actions: List[Action], message: str) -> List[Action]:
        """Recognize requests that hand the planning over, which the parser tends to misfile."""
        lowered = message.lower()
        swap = self._proposal_swap(message)
        if swap is not None:
            return [swap]
        reminder = self._reminder_from(message)
        if reminder is not None:
            return [reminder]
        if self._MUTE_RE.search(message):
            return [a for a in actions if a.type == "outcome"] + [Action("mute", {})]
        m = self._ADD_TO_LIST_RE.search(message) or self._ON_LIST_RE.search(message)
        if m and not re.search(r"\b(calendar|schedule|plan)\b", m.group("list"), re.IGNORECASE):
            task = re.sub(r"^(?:a|an|the)\s+", "", m.group("task").strip(" '\""), flags=re.IGNORECASE)
            if task.lower() in PRONOUNS:  # "put it on my errands list": the item just talked about
                last = self.tools.day_plan.get_span(self.state.last_ids[0]) if self.state.last_ids else None
                if last is None:
                    return [Action("chat", {})]
                task = last.title
            return [Action("task_add", {"title": task[:1].upper() + task[1:], "list": m.group("list")})]
        m = self._BREAKDOWN_RE.search(message)
        if m and not any(a.type in ("add", "move", "remove", "swap") for a in actions):
            return [Action("breakdown", {"target": (m.group("what") or "").strip()})]
        part = next((p for p in self._PARTS if p in lowered), "")
        edits = [a for a in actions if a.type in ("add", "move", "remove", "swap", "outcome", "confirm", "reject", "choose")]
        if self._PLACE_RE.search(message) and not any(a.type in ("add", "move") for a in edits):
            q = Action("question", {"topic": f"free time {part}".strip()})
            m = self._LENGTH_RE.search(message)
            if m:
                q.fields["duration"] = m.group(0)
            return edits + [q]
        fill = bool(self._FILL_RE.search(message))
        pick = bool(self._PICK_RE.search(message))
        if fill or (pick and not self.state.proposal and not edits):
            suggest = Action("suggest", {"topic": part or "today", "fill": True, "apply": pick})
            m = self._SPAN_RE.search(message)
            if m:
                suggest.fields["from"], suggest.fields["until"] = m.group(1), m.group(2)
            else:
                m = self._UNTIL_RE.search(message)
                if m:
                    suggest.fields["until"] = m.group(1)
            keep = [a for a in actions if a.type not in ("suggest", "chat") and
                    not (a.type == "question" and not self._SHOW_DAY_RE.search(message)) and
                    not (a.type == "add" and self._GENERIC_TITLE_RE.match((a.get("title") or "").strip()))]
            return [a for a in keep if a.type != "question"] + [suggest] + [a for a in keep if a.type == "question"]
        return actions

    def _accept_readds(self, actions: List[Action]) -> List[Action]:
        """Adding items we just proposed means taking them as proposed, at the proposed times."""
        if not self.state.proposal or self.state.proposal_kind != "plan":
            return actions
        by_title = {p["title"].lower(): p for p in self.state.proposal}
        readds = [a for a in actions if a.type == "add" and (a.get("title") or "").lower() in by_title]
        if not readds:
            return actions
        chosen = [by_title[(a.get("title") or "").lower()] for a in readds]
        rest = [a for a in actions if a not in readds and a.type not in ("confirm", "choose")]
        return [Action("accept", {"items": chosen})] + rest

    def _rollover_pending(self) -> bool:
        assistant = getattr(self.tools, "assistant", None)
        return bool(assistant is not None and assistant.rollover_pending())

    def _dialog_act(self, message: str) -> Optional[Action]:
        """Answers to our own pending question: yes / no / which option."""
        if not self.state.proposal:
            if self._rollover_pending():  # the morning offer to roll yesterday into today
                lowered = message.lower().strip()
                if self._NO_RE.search(lowered) and len(lowered) < 60:
                    return Action("reject", {})
                if self._YES_RE.search(lowered) or self._ALL_YES_RE.search(lowered):
                    return Action("confirm", {})
            return None
        lowered = message.lower().strip()
        m = re.search(r"\b(?:option|number|#)\s*(\d)\b|^\s*(\d)\s*(?:[.!),:]|$|\s+(?:please|works|sounds|is good|then|and|also))", lowered)
        if m:
            return Action("choose", {"option": int(m.group(1) or m.group(2))})
        m = re.search(r"\bthe (first|second|third|1st|2nd|3rd) (?:one|option)\b|^\s*(first|second|third) one\b", lowered)
        if m:
            return Action("choose", {"option": self._ORDINAL_WORDS[m.group(1) or m.group(2)]})
        if self._PICK_RE.search(lowered) and not self._FILL_RE.search(lowered):
            return Action("confirm", {})
        if self._NO_RE.search(lowered) and (len(lowered) < 60 or not re.search(r"\b(actually|wait)\b.*\b(yes|yeah|do it)\b", lowered)):
            return Action("reject", {})
        if self._YES_RE.search(lowered) and len(lowered) < 60:
            return Action("confirm", {})
        if self._ALL_YES_RE.search(lowered) and not re.search(r"\b(but|except|instead|swap|not|don'?t|no)\b", lowered):
            return Action("confirm", {})
        return None

    def _execute(self, actions: List[Action], message: str) -> str:
        actions = self._interpret(actions, message)
        if not actions:
            return self._chat(message)
        changes = self.tools.successful_changes
        reply = self._run_actions(actions, message)
        if self.tools.successful_changes > changes:
            reply = self._with_capacity_note(reply)
        return reply

    def _with_capacity_note(self, reply: str) -> str:
        """Once a day, say so when the plan holds well beyond what the user usually gets through."""
        assistant = getattr(self.tools, "assistant", None)
        db = self.tools.scheduler.state_db if self.tools.scheduler else None
        if assistant is None or db is None:
            return reply
        key = f"capacity_warned:{self.tools.day_plan.target_date.isoformat()}"
        if db.get_sync_meta(key):
            return reply
        note = assistant.capacity_note()
        if not note:
            return reply
        db.set_sync_meta(key, "1")
        return f"{reply}\n{note} Want me to move something to another day?"

    def _interpret(self, actions: List[Action], message: str) -> List[Action]:
        """Python's reading of the model's actions: dialog acts, repairs and routing. Changes nothing."""
        act = self._dialog_act(message)
        if act is not None and not any(a.type in ("confirm", "reject", "choose") for a in actions):
            proposed = {p["title"].lower() for p in self.state.proposal}
            # A re-add of a proposed item (or nothing at all) is the model's way of saying yes / no.
            actions = [act] + [a for a in actions if not (a.type == "add" and (a.get("title") or "").lower() in proposed)
                               and a.type != "chat"]
        actions = self._sanity(actions, message)
        actions = self._route(actions, message)
        actions = self._accept_readds(actions)
        if any(a.type in ("confirm", "accept", "choose") for a in actions) and self.state.proposal:
            # accepting a plan already lists it ("Done: ..."); a "show me" alongside would repeat it
            actions = [a for a in actions if not (a.type == "question" and "schedule" in (a.get("topic") or ""))]
        waiting = self.state.awaiting_time
        if waiting and not any(a.type in ("add", "move", "remove") for a in actions):
            m = self._TIME_IN_TEXT.search(message)
            if m:
                actions = [Action("add", {**waiting, "start": m.group(1)})] + [a for a in actions if a.type != "chat"]
        elif waiting:
            for a in actions:  # the answer often comes back as an add without the title
                if a.type == "add" and not a.get("title"):
                    a.fields["title"] = waiting.get("title")
        if all(a.type == "chat" for a in actions):
            lowered = message.lower()
            day = "tomorrow" if "tomorrow" in lowered else None
            if re.search(r"\b(free|open|available|time left|left before|room)\b", lowered) and "?" in message:
                actions = [Action("question", {"topic": "free time " + ("tonight" if "tonight" in lowered else ""),
                                               **({"day": day} if day else {})})]
            elif self._SHOW_DAY_RE.search(message):
                actions = [Action("question", {"topic": "schedule", **({"day": day} if day else {})})]
            elif self._WEATHER_RE.search(message) and "?" in message:
                actions = [Action("question", {"topic": "weather"})]
        # Also answer a free-time question asked alongside other requests
        if re.search(r"\bhow much (free )?time\b|\bwhen am i free\b|\bfree time\b", message.lower()) and \
                not any(a.type in ("question", "suggest") for a in actions):
            tonight = bool(re.search(r"\b(tonight|this evening|before bed|bedtime)\b", message.lower()))
            actions = actions + [Action("question", {"topic": "free time" + (" tonight" if tonight else ""),
                                                     "day": None})]
        return actions

    def _run_actions(self, actions: List[Action], message: str) -> str:
        self._message = message
        lines: List[str] = []
        changed = False
        handlers: Dict[str, Callable[[Action], Tuple[str, bool]]] = {
            "add": self._do_add, "move": self._do_move, "remove": self._do_remove, "swap": self._do_swap,
            "outcome": self._do_outcome,
            "confirm": self._do_confirm, "reject": self._do_reject, "choose": self._do_choose,
            "suggest": self._do_suggest, "question": self._do_question, "remember": self._do_remember,
            "accept": lambda a: self._apply_proposals(a.get("items")),
            "profile": self._do_profile, "energy": self._do_energy,
            "task_add": self._do_task_add, "breakdown": self._do_breakdown,
            "remind": self._do_remind, "mute": self._do_mute, "proposal_swap": self._do_proposal_swap,
        }
        untimed = [a for a in actions if a.type == "add" and not any(a.get(k) for k in ("start", "end", "after", "before"))]
        if len(untimed) >= 2 or (untimed and FIGURE_IT_OUT_RE.search(message)):
            # several things and no times ("put down Brad, Torquio, the walk, lunch and dinner"): Python lays them
            # out rather than asking "what time?" for each one
            lines.append(self._lay_out(untimed))
            changed = True
            actions = [a for a in actions if a not in untimed]
        retry: List[Action] = []
        for action in actions:
            if action.type == "chat":
                if not lines and len(actions) == 1:
                    return self._chat(message)
                continue

            snapshot = self.state.to_dict()
            text, did_change = handlers[action.type](action)
            failed = action.type in ("add", "move", "swap") and not did_change and \
                any(k in text for k in ("would overlap", "I don't see", "couldn't"))
            if failed and len(actions) > 1:
                # It may depend on something later in the same message; try again at the end.
                self.state = DialogState.from_dict(snapshot)
                retry.append(action)
                continue
            if text:
                lines.append(text)
            changed = changed or did_change
        for action in retry:
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
            self.state.awaiting_time = {k: v for k, v in a.fields.items() if k in ("title", "duration", "day", "alongside")}
            self.state.awaiting_time.setdefault("title", title)
            return f"When should I put {title}?", False
        self.state.awaiting_time = {}
        guessed = False
        if a.get("duration"):
            try:
                parse_duration_minutes(a.get("duration"))
            except ClockError:
                a.fields["duration"], guessed = "60 minutes", True  # "unknown", "a while": guess and say so
        learned = None
        if (not a.get("duration") and not a.get("end")) or guessed:
            assistant = getattr(self.tools, "assistant", None)
            learned = assistant.typical_duration(title) if assistant else None
            if learned:  # how long it really takes the user, from what they've reported
                a.fields["duration"], guessed = f"{learned} minutes", False
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
        except ClockError:
            self.state.awaiting_time = {k: v for k, v in a.fields.items() if k in ("title", "duration", "day", "alongside")}
            self.state.awaiting_time.setdefault("title", title)
            return f"What time should I put {title}?", False
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
        if AGAIN_RE.search(getattr(self, "_message", "")):
            args["again"] = True  # "upgrades at 7 and again at 9": a second session, not a move
        ok, out = self._run("add_event", args)
        if ok:
            return self._added_reply(out, day, guessed, learned)
        if "would overlap" in out and "fixed calendar commitment" not in out and not args.get("push_later"):
            ok, pushed = self._run("add_event", {**args, "push_later": True})  # slide what follows, like Gemini
            if ok:
                return self._added_reply(pushed, day, guessed, learned)
        if alternatives and "would overlap" in out:
            return self._offer_alternatives(args, out), False
        return f"I couldn't add {args['title']}: {out}", False

    def _added_reply(self, out: str, day: Optional[str], guessed: bool, learned: bool) -> Tuple[str, bool]:
        span = self._span_from_result(out, day)
        self._remember(span)
        text = self._added_line(out)
        heads_up = re.search(r"Weather heads-up: (.+?)\.(?:\s|$)", out)
        if heads_up:
            text += f" Heads-up: {heads_up.group(1)}."
        if guessed:
            text += " (I guessed an hour; tell me if it's different.)"
        elif learned:
            text += " (about as long as it usually takes you)"
        slid = re.findall(r"(?:; |\()([^;()]+? now \d[^;()]*?(?:AM|PM) – \d[^;()]*?(?:AM|PM))", out)
        if slid:
            text += "\nTo make room: " + "; ".join(slid) + "."
        return text, True

    def _span_from_result(self, out: str, day: Optional[str]) -> Optional[EventSpan]:
        m = re.search(r"\[id ([0-9a-f]+)\]", out)
        if not m:
            return None
        plan = self.tools.plan_for(day) if day else self.tools.day_plan
        return plan.get_span(m.group(1))

    def _user_unit(self) -> Optional[str]:
        """'chunks' or 'thirties' when the user is counting in them, so answers can too."""
        m = re.search(r"\b(chunks?|thirties)\b", getattr(self, "_message", "").lower())
        if not m:
            return None
        return "chunks" if m.group(1).startswith("chunk") else "blocks"  # "1 thirty" next to a time misreads

    def _in_user_units(self, minutes: int) -> str:
        n, unit = minutes // 30, self._user_unit()
        return f"{n} {unit if n != 1 else {'chunks': 'chunk', 'blocks': 'block'}[unit]}"

    def _added_line(self, out: str) -> str:
        day = ""
        m = re.match(r"\[(.+?)\] ", out)
        if m:
            day, out = f" on {m.group(1)}", out[m.end():]
        span = self._span_from_result(out, None) if not day else None
        if span is not None and span.duration_minutes < 30:  # short items are told by their thirty
            block = thirty_of(span.start_dt)
            before = [s.title for s in self.tools.day_plan.spans_in_view()
                      if s.id != span.id and not s.is_envelope and s.duration_minutes < 30
                      and thirty_of(s.start_dt) == block and s.end_dt <= span.start_dt]
            return f"Added {span.title} at {format_clock(block)}" + \
                (f", after {' and '.join(before)}." if before else ".")
        m = re.match(r"Added (.+?), (.+? – .+?) \((.+?)\)", out)
        if not m:
            return out
        length = m.group(3)
        if self._user_unit():
            try:
                minutes = parse_duration_minutes(length.replace(" ", ""))
            except ClockError:
                minutes = 0
            if minutes and minutes % 30 == 0:
                length = self._in_user_units(minutes)
        return f"Added {m.group(1)}{day}: {m.group(2)} ({length})."

    def _offer_alternatives(self, args: Dict[str, Any], why: str) -> str:
        """The requested time clashes: offer concrete options instead of failing."""
        saved = self.tools.day_plan
        self.tools.day_plan = self.tools.plan_for(args.get("day"))
        try:
            return self._alternatives_on_plan(args)
        finally:
            self.tools.day_plan = saved

    def _alternatives_on_plan(self, args: Dict[str, Any]) -> str:
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
        if not found and not a.get("day"):
            found, day = self._find_on_other_day(ref)
            if found:
                a.fields["day"] = day
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

    _DAY_WORD_RE = re.compile(r"\b(tomorrow|tmrw|tmr|tomorow|yesterday|monday|tuesday|wednesday|thursday|friday|"
                              r"saturday|sunday)\b", re.IGNORECASE)

    def _find_on_other_day(self, ref: str) -> Tuple[List[EventSpan], Optional[str]]:
        """An item that isn't on today's plan: on a day the message names, or one we just touched."""
        days = []
        m = self._DAY_WORD_RE.search(getattr(self, "_message", ""))
        if m:
            word = m.group(1).lower()
            days.append("tomorrow" if word.startswith("tm") or word == "tomorow" else word)
        days += [d for d in ("tomorrow", "yesterday") if d not in days]
        saved = self.tools.day_plan
        try:
            for day in days:
                try:
                    self.tools.day_plan = self.tools.plan_for(day)
                except Exception:
                    continue
                found = self._span_candidates(ref)
                if ref.lower() in PRONOUNS or not ref:
                    found = [s for s in found if s.id in self.state.last_ids]
                elif not m:  # without a day in the message, only follow something we just touched
                    found = [s for s in found if s.id in self.state.last_ids]
                if found:
                    return found, day
        finally:
            self.tools.day_plan = saved
        return [], None

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
        if not ok and "Nothing to change" in out:  # already where they asked (an earlier step slid it there)
            self._remember(span)
            return f"{span.title} is at {format_clock(span.start_dt)} – {format_clock(span.end_dt)}.", False
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
        return "Swapped:\n" + "\n".join(agenda_lines([(s.start_dt, s.end_dt, s.title, "") for s in (a_, b_)])), True

    def _do_remove(self, a: Action) -> Tuple[str, bool]:
        ref = a.get("target") or a.get("title") or ""
        found = self._span_candidates(ref) if ref and ref.lower() not in PRONOUNS else []
        if len(found) > 1 and all(s.source == "calendar" for s in found) and len({s.title.lower() for s in found}) == 1:
            span, problem = found[0], ""  # two invites with one name ("decline both SB Shows"): all of them
        else:
            span, problem = self._resolve_one(a)
        if span is None:
            return problem, False
        if span.source == "calendar":  # not going: decline it (and its namesakes: "decline both SB Shows")
            same = [s for s in self.tools.day_plan.spans_in_view()
                    if s.source == "calendar" and s.title.lower() == span.title.lower()]
            declined = []
            for s in same:
                ok, out = self._run("resolve_calendar_event", {"event": s.id, "attend": False})
                if ok:
                    declined.append(format_clock(s.start_dt))
            if not declined:
                return out, False
            return f"Declined {span.title}" + (f" ({' and '.join(declined)})" if len(declined) > 1 else "") + \
                ": that time is free.", True
        args = {"event": span.id, **({"day": a.get("day")} if a.get("day") else {})}
        ok, out = self._run("remove_event", args)
        if not ok:
            return f"I couldn't remove {span.title}: {out}", False
        self.state.last_ids = [i for i in self.state.last_ids if i != span.id]
        return f"Removed {span.title} ({format_clock(span.start_dt)} – {format_clock(span.end_dt)}).", True

    def _do_outcome(self, a: Action) -> Tuple[str, bool]:
        ref = a.get("target") or a.get("title") or ""
        found = self._span_candidates(ref) if ref.lower() not in PRONOUNS or self.state.last_ids else []
        if found and all(s.source == "calendar" for s in found):
            return f"{found[0].title} is on your calendar, so there's nothing to check off.", False
        found = [s for s in found if not s.is_envelope and s.source != "calendar"]
        message = getattr(self, "_message", "") or ""
        if (a.get("status") or "done") == "done" and STARTING_RE.search(message) and not FINISHED_RE.search(message):
            name = found[0].title if len(found) == 1 else ref
            return f"Got it, {name} is underway.", False  # starting something isn't finishing it
        event = found[0].id if len(found) == 1 else ref
        args: Dict[str, Any] = {"event": event, "status": a.get("status") or "done"}
        for key in ("started", "finished"):
            if a.get(key):
                try:
                    args[key] = self._clock_arg(self._loose_time(a.get(key), prefer_future=False, recent_past=True))
                except ClockError:
                    pass
        if a.get("duration"):
            args["duration"] = a.get("duration")
        if not (args.get("started") and args.get("finished")) and (args.get("started") or args.get("finished")):
            args["estimated"] = True
        ok, out = self._run("log_outcome", args)
        if not ok:
            return f"I couldn't find {ref} on today's plan to mark it.", False
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
        return text, False  # recorded, but the free time didn't change

    def _apply_proposals(self, items: List[Dict[str, Any]]) -> Tuple[str, bool]:
        added, lines, failures = [], [], []
        for args in items:
            ok, out = self._run("add_event", args)
            if ok:
                span = self._span_from_result(out, args.get("day"))
                self._remember(span)
                added.append(span)
                lines.append(self._added_line(out))
            else:
                failures.append(f"Couldn't add {args['title']}: {out}")
        self.state.proposal, self.state.proposal_kind = [], ""
        if len(added) >= 2 and all(added):
            lines = ["Done:"] + agenda_lines([(s.start_dt, s.end_dt, s.title, "")
                                               for s in sorted(added, key=lambda s: s.start_dt)])
        return "\n".join(lines + failures), bool(added)

    def _do_confirm(self, a: Action) -> Tuple[str, bool]:
        target = {_stem(w) for w in _words(a.get("target") or "")}
        invites = [s for s in self.tools.day_plan.spans_in_view() if s.tentative]
        named = [s for s in invites if target & {_stem(w) for w in _words(s.title)}]
        if not named and not self.state.proposal and self._rollover_pending():
            invites = []  # a plain "yes" answers the morning offer, not an invite nobody mentioned
        if named or (invites and not self.state.proposal):
            invite = (named or invites)[0]
            ok, out = self._run("resolve_calendar_event", {"event": invite.id, "attend": True})
            return (f"Great, {invite.title} is confirmed: {format_clock(invite.start_dt)} – "
                    f"{format_clock(invite.end_dt)}." if ok else out), ok
        if not self.state.proposal and self._rollover_pending():
            m = re.search(r"\b(?:except|but not|but skip|skip|without|leave out|not the)\s+(.+)$", getattr(self, "_message", ""), re.I)
            ok, out = self._run("apply_rollover", {"skip": m.group(1)} if m else {})
            return out, ok
        if not self.state.proposal:
            return "", False
        if self.state.proposal_kind == "breakdown":
            return self._write_breakdown(self.state.proposal[0])
        if self.state.proposal_kind == "options":
            return self._apply_proposals(self.state.proposal[:1])
        return self._apply_proposals(self.state.proposal)

    def _do_proposal_swap(self, a: Action) -> Tuple[str, bool]:
        proposal = [dict(p) for p in self.state.proposal]
        avoid = {p["title"].lower() for p in proposal}
        scheduled = {sp.title.lower() for sp in self.tools.day_plan.spans}
        ranked = sorted((t for t in self.tasks if not t.someday and not t.parent_task_id
                         and t.title.lower() not in avoid and t.title.lower() not in scheduled),
                        key=lambda t: -self._score_task(t, set(), set(), scheduled))
        out_titles = []
        for i in a.get("indexes"):
            out_titles.append(proposal[i]["title"])
            minutes = parse_duration_minutes(proposal[i]["duration"])
            pick = next((t for t in ranked if t.title.lower() not in avoid), None)
            if pick is None:
                proposal[i] = None
                continue
            avoid.add(pick.title.lower())
            proposal[i] = {**proposal[i], "title": pick.title, "task_id": pick.id, "duration": f"{minutes} minutes"}
        proposal = [p for p in proposal if p]
        self.state.proposal = proposal
        items = []
        for p in proposal:
            p_start = self._loose_time(p["start"])
            items.append((p_start, p_start + timedelta(minutes=parse_duration_minutes(p["duration"])), p["title"], ""))
        lines = [f"Here's the plan with {', '.join(out_titles)} swapped out:"] + agenda_lines(items, numbered=True)
        lines.append("Want me to put these on your schedule, or change something else?")
        return "\n".join(lines), False

    # --- messages ---

    def _do_remind(self, a: Action) -> Tuple[str, bool]:
        fields = {k: v for k, v in a.fields.items() if k in ("text", "date", "time")}
        ping = bool(re.search(r"\b(?:ping|text|message) me\b", getattr(self, "_message", ""), re.I))
        ok, out = self._run("set_reminder", {**fields, "ping_only": ping})  # "remind me to" is a thing to do
        if not ok:
            return out, False
        m = re.match(r"Ping set for (.+?) at (.+?): (.+) \(id \w+\)\.$", out)
        if m:
            return f"Okay, I'll ping you {m.group(1)} at {m.group(2)}: {m.group(3)}", False
        m = re.match(r"On the plan for (.+?) at (.+?)( and on today's list)?, with a ping when it's time: (.+) "
                     r"\(item id \w+\)\.$", out)
        return (f"Okay: {m.group(4)}, {m.group(1)} at {m.group(2)}, on the plan" + (" and today's list" if m.group(3)
                else "") + ", and I'll ping you then." if m else out), False

    def _do_mute(self, a: Action) -> Tuple[str, bool]:
        message = getattr(self, "_message", "").lower()
        everything = bool(re.search(r"\b(all|any|anything|every|today|tonight|for (the|a) (day|while))\b", message))
        target = None
        if not everything:
            last = self._last_assistant()
            asked = re.search(r"did you get to (.+?)(?:, or are you still on it|\?)", last)
            ref = asked.group(1) if asked else ""
            named = [s for s in self._span_candidates(message) if not s.is_envelope] if not ref else []
            target = (self._span_candidates(ref) or named or [None])[0] if (ref or named) else None
        ok, out = self._run("mute_checkins", {"event": target.id} if target else {})
        return out, False

    # --- the user's lists in Joplin ---

    def _do_task_add(self, a: Action) -> Tuple[str, bool]:
        ok, out = self._run("add_task", {"title": a.get("title"), "list": a.get("list")})
        return re.sub(r"\s*To schedule it, use task_id=\S+", "", out), False

    def _breakdown_target(self, ref: str) -> Optional[TaskItem]:
        if ref and ref.lower() not in PRONOUNS:
            ranked = rank_tasks(self.tasks, ref, limit=1)
            if ranked:
                return ranked[0]
        for span_id in self.state.last_ids:  # "it": the item we just talked about
            span = self.tools.day_plan.get_span(span_id)
            if span is not None and span.task_id:
                task = next((t for t in self.tasks if t.id == span.task_id), None)
                if task:
                    return task
            if span is not None:
                ranked = rank_tasks(self.tasks, span.title, limit=1)
                if ranked:
                    return ranked[0]
        for p in self.state.proposal:  # one of the items we just suggested
            task = next((t for t in self.tasks if t.id == p.get("task_id")), None)
            if task:
                return task
        return None

    def _do_breakdown(self, a: Action) -> Tuple[str, bool]:
        task = self._breakdown_target(a.get("target") or "")
        if task is None:
            return "Which task should I break down?", False
        where = " > ".join(x for x in (task.parent_note_title, task.section) if x)
        raw = self.runtime.json_call(BREAKDOWN_PROMPT, f"Task: {task.title}" + (f"\nFrom: {where}" if where else ""),
                                     BREAKDOWN_SCHEMA, max_output_tokens=200)
        steps = [str(x).strip() for x in (raw.get("steps") or []) if str(x).strip()][:6]
        if len(steps) < 2:
            return f"I couldn't come up with good steps for {task.title}. What's the first thing it needs?", False
        self.state.proposal = [{"breakdown": task.id, "title": task.title, "steps": steps}]
        self.state.proposal_kind = "breakdown"
        lines = [f"Here's {task.title} in smaller steps:"] + [f"{i}. {x}" for i, x in enumerate(steps, 1)]
        lines.append("Want me to add these to Joplin as subtasks?")
        return "\n".join(lines), False

    def _write_breakdown(self, p: Dict[str, Any]) -> Tuple[str, bool]:
        self.state.proposal, self.state.proposal_kind = [], ""
        ok, out = self._run("break_down_task", {"task": p["breakdown"], "steps": p["steps"]})
        if not ok:
            return out, False
        first = next((t for t in self.tools.services.tasks if t.title == p["steps"][0]), None) \
            if self.tools.services else None
        windows = [(s, e) for s, e in self.tools.open_windows(after=self._now()) if e - s >= timedelta(minutes=30)]
        if first is None or not windows:
            return f"Added {len(p['steps'])} steps under {p['title']} in Joplin.", False
        start = windows[0][0]
        if start.minute % 30 or start.second:
            start = start.replace(minute=0 if start.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
        self.state.proposal = [{"title": first.title, "start": format_clock(start), "duration": "30 minutes",
                                "task_id": first.id}]
        self.state.proposal_kind = "plan"
        return (f"Added {len(p['steps'])} steps under {p['title']} in Joplin. "
                f"Want to start with \"{first.title}\" at {format_clock(start)}?"), False

    def _do_reject(self, a: Action) -> Tuple[str, bool]:
        if not self.state.proposal and self._rollover_pending():
            self.tools.assistant.decline_rollover()
            return "Okay, I'll leave yesterday's items where they are.", False
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
            start, end = datetime.combine(day, time(12, 0), tzinfo=tz), datetime.combine(day, time(18, 0), tzinfo=tz)
        elif "morning" in t:
            end = datetime.combine(day, time(12, 0), tzinfo=tz)
        if self._is_today():
            start = max(start, now)
        sleep = [s for s in plan.active_spans() if s.category == SpanCategory.SLEEP and s.start_dt > start]
        if sleep:
            end = min(end, sleep[0].start_dt)
        return start, end

    def _score_task(self, task: TaskItem, topic_words: set, goal_words: set, scheduled_titles: set) -> float:
        if task.someday or task.list_role in ("reference", "backlog") or task.title.lower() in scheduled_titles:
            return -1.0
        words = {_stem(w) for w in _words(f"{task.title} {task.parent_note_title or ''} {task.section}")}
        score = 1.0
        score += 4.0 * len(topic_words & words)
        score += 2.0 * len(goal_words & words)
        score += min(task.deferred_count, 4) * 0.75
        if task.list_role == "today" or (not task.list_role and (task.parent_note_title or "").lower() in ("today to-do", "today")):
            score += 3.0
        score += {"now": 3.0, "soon": 1.5}.get(task.urgency, 0.0)
        if task.due:
            try:
                days = (date.fromisoformat(task.due) - self.tools.today()).days
                score += 3.0 if days <= 2 else 1.5 if days <= 7 else 0.0
            except ValueError:
                pass
        if task.parent_task_id:
            score -= 0.5  # prefer whole tasks over sub-steps
        if not task.is_checkbox_item and not task.id.startswith("media:"):
            score -= 0.5  # a whole note ("Assorted Quickies") is usually a bucket, not an action
        if "media" in task.tags or task.id.startswith("media:"):
            score -= 1.5 if not (topic_words & {"watch", "read", "relax", "movi", "show", "book"}) else -1.0
        return score

    def _project_blocks(self) -> List[TaskItem]:
        """A project backlog is offered as time on the project ("Thirties dev"), never as single tickets."""
        seen, out = set(), []
        for t in self.tasks:
            if t.list_role == "backlog" and t.parent_note_id and t.parent_note_id not in seen:
                seen.add(t.parent_note_id)
                out.append(TaskItem(id=f"project:{t.parent_note_id}", source_notebook=t.source_notebook,
                                    title=f"{t.parent_note_title} dev", list_role="list", estimated_thirties=2))
        return out

    def _do_suggest(self, a: Action) -> Tuple[str, bool]:
        topic = a.get("topic") or ""
        start, end = self._window_for(topic)
        fill = bool(a.get("fill"))
        if a.get("from"):
            try:
                start = self._loose_time(str(a.get("from")), near=start if start > self.tools.day_plan.day_start else None)
            except ClockError:
                pass
            if self._is_today():
                start = max(start, self._now())
        if a.get("until"):
            until = str(a.get("until")).lower()
            dinner = next((s for s in self.tools.day_plan.active_spans() if "dinner" in s.title.lower() and s.start_dt > start), None)
            try:
                end = dinner.start_dt if until == "dinner" and dinner else \
                    self._loose_time({"dinner": "6 PM", "bed": "bedtime"}.get(until, until), after=start)
            except ClockError:
                pass
            if fill and end > start and not a.get("from") and not any(p in topic for p in self._PARTS):
                start = max(self._now(), self.tools.day_plan.day_start) if self._is_today() else start
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
        assistant = getattr(self.tools, "assistant", None)
        weather = assistant._weather() if assistant else None
        try:
            rainy = bool(weather and any(weather.bad_for_outdoors(ws, we) for ws, we in windows[:3]))
        except Exception:
            rainy = False
        from thirties_core.weather import OUTDOOR_RE
        scored = sorted(((self._score_task(t, topic_words, goal_words, scheduled)
                          - (6.0 if rainy and OUTDOOR_RE.search(t.title) else 0.0), i, t)  # nothing outdoors in the rain
                         for i, t in enumerate(self.tasks + self._project_blocks())),
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
        picked = self._pick(ranked, topic, windows) or ranked[:3]
        ranked = picked + [t for t in ranked if t not in picked] if fill else picked
        try:
            total = parse_duration_minutes(a.get("duration")) if a.get("duration") else None
        except ClockError:
            total = None
        want = None
        proposal: List[Dict[str, Any]] = []
        cursor_windows = list(windows)
        budget = total
        for task in ranked:
            minutes = want or max(60 if fill else 30, 30 * (task.estimated_thirties or 1))
            if budget is not None:
                if budget < 30:
                    break
                minutes = min(minutes, budget)
                budget -= minutes
            for i, (ws, we) in enumerate(cursor_windows):
                slot_start = ws if ws.minute in (0, 30) and ws.second == 0 else \
                    ws.replace(minute=0 if ws.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
                if fill and timedelta(minutes=30) <= we - slot_start < timedelta(minutes=minutes):
                    minutes = int((we - slot_start).total_seconds() // 1800) * 30  # use up the end of a window
                if we - slot_start >= timedelta(minutes=minutes):
                    proposal.append({"title": task.title, "start": format_clock(slot_start),
                                     "duration": f"{minutes} minutes", "task_id": task.id})
                    cursor_windows[i] = (slot_start + timedelta(minutes=minutes), we)
                    break
            if len(proposal) >= (10 if fill else 3):
                break
        if not proposal:
            return "I don't have a good backlog item for that. What are you in the mood for?", False
        self.state.proposal, self.state.proposal_kind = proposal, "plan"
        if a.get("apply"):  # "you pick": the user handed the decision over, so make it
            text, changed = self._apply_proposals(proposal)
            return text + "\nSay the word if you want to swap anything out.", changed
        items = []
        for p in proposal:
            p_start = self._loose_time(p["start"])
            p_end = p_start + timedelta(minutes=parse_duration_minutes(p["duration"]))
            task = next((t for t in self.tasks if t.id == p["task_id"]), None)
            why = f"put off {task.deferred_count}x" if task and task.deferred_count >= 2 else ""
            items.append((p_start, p_end, p["title"], why))
        lines = ["How about:"] + agenda_lines(items, numbered=True)
        lines.append("Want all of these, just one (say the number), or something else?")
        stuck = next((t for t in self.tasks if t.deferred_count >= 3 and any(p["task_id"] == t.id for p in proposal)), None)
        if stuck:
            lines.append(f"If {stuck.title} feels too big, I can break it into smaller steps.")
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

    _WEATHER_RE = re.compile(r"\b(weather|forecast|rain\w*|sunny|snow\w*|umbrella|temperature|how (hot|cold|warm)|"
                             r"nice out|storm\w*)\b", re.IGNORECASE)

    def _do_question(self, a: Action) -> Tuple[str, bool]:
        topic = (a.get("topic") or "").lower()
        day = a.get("day")
        named_day = DAY_WORD_RE.search(getattr(self, "_message", "") or "")
        if not day and named_day:  # "what does tomorrow look like?": the parse sometimes drops the day
            day = named_day.group(1).lower()
        if self._WEATHER_RE.search(topic) or self._WEATHER_RE.search(getattr(self, "_message", "")):
            ok, out = self._run("get_weather", {})
            return out, False
        if "week" in topic or re.search(r"\b(my|this|the|next) week\b", getattr(self, "_message", "").lower()):
            ok, out = self._run("week_overview", {})
            return "Your week:\n" + "\n".join(f"- {l}" for l in out.splitlines()), False
        if any(w in topic for w in ("free", "open", "available", "time left", "packed", "busy", "room")):
            saved = self.tools.day_plan
            try:
                self.tools.day_plan = self.tools.plan_for(day)
                start, end = self._window_for(topic) if not day else (self.tools.day_plan.day_start, self.tools.day_plan.day_end)
                try:
                    need = parse_duration_minutes(a.get("duration")) if a.get("duration") else 15
                except ClockError:
                    need = 15
                windows = [(w0, w1) for w0, w1 in self.tools.open_windows(after=start, before=end) if w1 - w0 >= timedelta(minutes=need)]
            finally:
                self.tools.day_plan = saved
            if not windows:
                return "You're fully booked for that stretch; nothing is open.", False

            def length(a, b) -> str:
                m = int((b - a).total_seconds() // 60)
                if self._user_unit() and m >= 30:
                    return self._in_user_units(m)
                h, mm = divmod(m, 60)
                return (f"{h} hour{'s' if h != 1 else ''}" if h else "") + (" and " if h and mm else "") + (f"{mm} minutes" if mm else "")
            if len(windows) == 1:
                a, b = windows[0]
                return f"You're free from {format_clock(a)} to {format_clock(b)} ({length(a, b)}).", False
            return "You're free:\n" + "\n".join(f"- {format_clock(a)} – {format_clock(b)} ({length(a, b)})" for a, b in windows), False
        message = getattr(self, "_message", "")
        if (not day or day == "today") and LEFT_TODAY_RE.search(f"{topic} {message}"):
            return self._left_today(), False
        named_list = self._list_named(message)
        if named_list:  # "what's on my Home list?": that list's open items
            items = [t.title for t in self.tasks if t.parent_note_title == named_list and not t.someday
                     and not t.parent_task_id]
            if not items:
                return f"Nothing open on {named_list}.", False
            return f"On {named_list}:\n" + "\n".join(f"- {t}" for t in items[:12]) + \
                (f"\n(and {len(items) - 12} more)" if len(items) > 12 else ""), False
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
        return self._schedule_answer(day, topic + " " + getattr(self, "_message", "")), False

    MEAL_WINDOWS = {"breakfast": (time(7, 0), time(11, 0), 30), "lunch": (time(11, 30), time(14, 30), 30),
                    "dinner": (time(17, 30), time(20, 30), 60), "supper": (time(17, 30), time(20, 30), 60)}

    def _lay_out(self, adds: List[Action]) -> str:
        """Place several things with no times, in Python: meals in their usual windows, a routine as its own items
        in order, everything else from now in the order given, with a free block after anything an hour or
        longer (breathing room). Says what didn't fit."""
        plan = self.tools.day_plan
        tz = plan.day_start.tzinfo
        now = self._now()
        cursor = thirty_ceil(now) if self._is_today() and now > plan.day_start else plan.day_start
        assistant = getattr(self.tools, "assistant", None)
        items: List[Tuple[str, int, Optional[Tuple[time, time]]]] = []  # title, minutes, meal window
        for a in adds:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            title = title[:1].upper() + title[1:] if title.islower() else title
            minutes = 0
            try:
                minutes = parse_duration_minutes(a.get("duration")) if a.get("duration") else 0
            except ClockError:
                minutes = 0
            meal = next((w for k, w in self.MEAL_WINDOWS.items() if k in title.lower()), None)
            routine = self._routine_items(title) if "routine" in title.lower() else []
            if routine:
                each = max(5, (minutes or 60) // len(routine))
                items += [(step, each, None) for step in routine]
                continue
            if not minutes and assistant:
                learned = assistant.typical_duration(title)
                minutes = learned or 0
            items.append((title, minutes or (meal[2] if meal else 60), (meal[0], meal[1]) if meal else None))
        placed, missed = [], []
        for title, minutes, window in sorted(items, key=lambda i: i[2] is None):  # meals first: they're anchored
            length = timedelta(minutes=minutes)
            lo = max(cursor, datetime.combine(plan.day_start.date(), window[0], tzinfo=tz)) if window else cursor
            hi = datetime.combine(plan.day_start.date(), window[1], tzinfo=tz) if window else plan.day_end
            ok, out = False, ""
            if minutes < 30 and window is None:  # a short thing goes right after the last one: they share a block
                ok, out = self._run("add_event", {"title": title, "start": self._clock_arg(cursor),
                                                  "duration": f"{minutes} minutes"})
            if not ok:
                slot = next((max(a, lo) for a, b in self.tools.open_windows(after=lo)
                             if min(b, hi) - max(a, lo) >= length), None)
                if slot is None:
                    missed.append(title)
                    continue
                ok, out = self._run("add_event", {"title": title, "start": self._clock_arg(slot),
                                                  "duration": f"{minutes} minutes"})
            if not ok:
                missed.append(title)
                continue
            span = self._span_from_result(out, None)
            if span is not None:
                self._remember(span)
                placed.append((span.start_dt, span.end_dt, span.title, ""))
                if window is None:
                    cursor = span.end_dt + (timedelta(minutes=30) if minutes >= 60 else timedelta())
        lines = ["Here's the day:"] + agenda_lines(sorted(placed)) if placed else []
        if missed:
            lines.append("Didn't fit: " + ", ".join(missed) + ". Tomorrow, or let them go?")
        return "\n".join(lines) or "I couldn't find room for those today."

    def _routine_items(self, title: str) -> List[str]:
        """The open items of the routine note a title names ("morning routine"), in order."""
        assistant = getattr(self.tools, "assistant", None)
        if assistant is None or self.tools.scheduler is None:
            return []
        from thirties_core.lists import ListCatalog
        from thirties_core.today import note_items, notes_with_role
        roles = ListCatalog(self.tools.scheduler.state_db).roles()
        words = {_stem(w) for w in _words(title)} - {"routin"}
        for note in notes_with_role(roles, assistant._joplin_notes(), "routine"):
            if not words or words & {_stem(w) for w in _words(note["title"])}:
                return [text for text, ticked in note_items(note) if not ticked]
        return []

    def _list_shortcut(self, text: str) -> Optional[str]:
        """"Add X to today's list" and "X isn't urgent" / "X is urgent" / "X has to happen by Friday", handled in
        Python. None when the message isn't one of these."""
        m = TODAY_LIST_RE.search(text)
        if m and m.group("items").strip(" .").lower() not in PRONOUNS | {"those", "these", "them", "everything", "all"}:
            items = [x.strip(" .") for x in re.split(r",\s*|\s+and\s+", m.group("items")) if x.strip(" .")]
            items = [x[:1].upper() + x[1:] if x.islower() else x for x in items]
            ok, out = self._run("add_to_today", {"items": items})
            return out if ok else f"I couldn't add that to today's list: {out}"
        m = URGENCY_RE.search(text)
        if m:
            what, how = m.group("what").strip(" ."), m.group("how").lower()
            args: Dict[str, Any] = {"task": what}
            due = re.search(r"\bby (\w+)", how)
            if due:
                args["due"] = due.group(1)
            else:
                args["urgency"] = ("someday" if "someday" in how else
                                   "whenever" if re.search(r"n[o']t|no rush|can wait", how) else "now")
            ok, out = self._run("set_urgency", args)
            return out if ok else None  # not a list item after all: let the parse handle it
        return None

    def _list_named(self, message: str) -> Optional[str]:
        """The list a question names ("my Home list", "the errands list", "on Medical"), if any."""
        if not re.search(r"\b(list|note|on my|on the)\b", message, re.I):
            return None
        said = {_stem(w) for w in _words(message)}
        names = {t.parent_note_title for t in self.tasks if t.parent_note_title}
        hits = [n for n in names if {_stem(w) for w in _words(n)} and {_stem(w) for w in _words(n)} <= said]
        return max(hits, key=len) if hits else None

    def _left_today(self) -> str:
        """What's left today: what's past and not reported (first, as everywhere: C2), then what's ahead, and
        what's on today's list (the Today note) with no time yet."""
        from thirties_core.messages import MissedTracker, missed_block, missed_for
        from thirties_core.today import note_items
        now = self._now()
        plan = self.tools.plan_for(None) if self._is_today() else self.tools.day_plan
        flexible = [s for s in plan.active_spans() if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
                    and s.status == SpanStatus.PLANNED and not s.tentative]
        behind = missed_for(self.tools, now) if plan is self.tools.day_plan else []
        if behind:
            MissedTracker(self.tools._state_db).mark(now, behind)
        ahead = sorted((s for s in flexible if s.end_dt > now and s not in behind), key=lambda s: s.start_dt)
        planned = {s.title.lower() for s in flexible}
        assistant = getattr(self.tools, "assistant", None)
        note = assistant._today_note() if assistant else None
        listed = [text for text, ticked in note_items(note) if not ticked and text.lower() not in planned]
        lines = []
        if ahead:
            lines.append("Still planned today:")
            lines += [f"- {format_clock(thirty_of(s.start_dt))} – {s.title}" for s in ahead]
        if listed:
            lines.append("On today's list, no time yet: " + ", ".join(listed) + ".")
        parts = [missed_block(behind)] if behind else []  # first: one per line, no times, not the current block
        parts += ["\n".join(lines)] if lines else []
        return "\n\n".join(parts) or "Nothing left for today: the plan and today's list are clear."

    def _schedule_answer(self, day: Optional[str], wording: str) -> str:
        """A clean, line-per-item view of a day (or part of it), without sleep or ids."""
        saved = self.tools.day_plan
        try:
            plan = self.tools.plan_for(day)
            self.tools.day_plan = plan
            lowered = wording.lower()
            start, end = plan.day_start, plan.day_end
            label = day or "today"
            whole_day = bool(re.search(r"\b(whole|entire|full|complete|all)\s+(day|schedule|list)\b|\ball day\b|"
                                       r"\bthrough (tonight|the evening|the night)\b|\bonward\b", lowered))
            for part in ("morning", "afternoon", "evening", "tonight"):
                if part in lowered and not whole_day:
                    start, end = self._window_for(part) if not day else (start, end)
                    label = f"the {part}" if part != "tonight" else "tonight"
                    if self._is_today() and part in ("evening", "tonight"):
                        start = max(datetime.combine(plan.day_start.date(), time(17, 0), tzinfo=start.tzinfo), plan.day_start)
                    if part in ("afternoon", "morning"):
                        start = min(start, self._window_for(part)[0])
                    break
            if label == "today" and not whole_day and self._is_today() and plan is saved:
                # The day being lived reads from now on: where they are, what's not marked yet, the rest (A5, C2)
                from thirties_core.messages import MissedTracker, missed_for, render_schedule
                now = self._now()
                if plan.day_start <= now < plan.day_end:
                    missed = missed_for(self.tools, now)
                    if missed:
                        MissedTracker(self.tools._state_db).mark(now, missed)
                    return render_schedule(plan, now, missed=missed)
            items = [s for s in plan.spans_overlapping(start, end, include_tentative=True)
                     if s.category != SpanCategory.SLEEP]
            if not items:
                return f"Nothing is planned for {label} yet."
            rows = [(s.start_dt, s.end_dt, s.title,
                     "unconfirmed" if s.tentative else (s.status.value if s.status.value != "planned" else ""))
                    for s in items]
            return "\n".join([f"Here's {label}:"] + agenda_lines(rows))
        finally:
            self.tools.day_plan = saved

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
