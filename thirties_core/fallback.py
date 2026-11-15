"""When the usual model's usage limit is reached: a simpler assistant answers, and the usual one comes back.

The chosen model (the Gemini brain's own, through the user's sources) can run out of quota mid-day. Thirties then
switches by itself to the next assistant that still has some, cheapest first and never one that costs the user
extra, and keeps answering:

1. A second cloud model through the same subscription (``[assistant] fallback_model``): a different quota group,
   but a small one shared with other work, so it's capped (``fallback_calls_per_5h`` calls in any 5 hours) and
   left alone after its own first usage-limit error.
2. The on-device model, only when it's offered (brains.gemma_offered) and already installed: nothing is
   downloaded for this.
3. No model at all: the lists, the Day page, ticking things off and what's next keep working (they're Python);
   a chat message gets a plain "unavailable until about <time>" reply with what's next, and is kept, marked
   unanswered, for when the usual assistant is back (the lost-turn retry, conversation.py).

The user is told once, in the one conversation (the reply carries it, so the app and the phone get the same stored
message), and once more when the usual assistant answers again.

Recovery is never a loop: the chosen model is tried again only after the reset time its error gave (agy's "Resets
in 5h45m", the API's retry delay), else after a guess of 15, then 30, then 60 minutes, and only by a real turn (the
next message, or the retry of a kept one), never a probe call. While it's out, nothing calls it.

The state lives in the state database (sync meta), so the window and the background service share it and it
survives a restart. ``with_fallback`` wraps the app's engine; tests and evals that pass their own engine never get
it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from thirties_core.clock import format_clock, thirty_ceil
from thirties_core.turns import QUOTA_RE, transport_failure

logger = logging.getLogger(__name__)

KEY = "stand_in"
WINDOW = timedelta(hours=5)              # the fallback model's cap counts calls in this window
GUESS_MINUTES = (15, 30, 60)             # when the error gives no reset time: the waits before each new try
EXHAUSTED = "Error contacting model: usage limit reached, and no other assistant has any left."

# What the user reads (tests/test_user_text.py scans these)
SIMPLER = ("I've reached my usage limit, so a simpler assistant is answering until {when}, when the usual one "
           "should be back.")
BACK = "The usual assistant is back."
UNAVAILABLE = ("The assistant is unavailable until {when}. I've kept your message and will answer it then. Your "
               "lists, the Day page and ticking things off still work.")


# --- reading the error ---

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_RESETS_RE = re.compile(r"resets?\s+in\s+((?:\d+(?:\.\d+)?\s*[dhms]\s*)+)", re.IGNORECASE)
_RETRY_RE = re.compile(r"retry(?:[ _-]?delay)?[\"']?\s*[:=]?\s*[\"']?\s*(?:in\s+)?(\d+(?:\.\d+)?)\s*s\b",
                       re.IGNORECASE)


def reset_after(error: str) -> Optional[timedelta]:
    """How long until the quota resets, as the error says ("Resets in 5h45m", "retryDelay": "37s"), else None."""
    text = str(error or "")
    found = _RESETS_RE.search(text)
    if found:
        seconds = sum(float(n) * _UNITS[u.lower()]
                      for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", found.group(1), re.IGNORECASE))
        return timedelta(seconds=seconds) if seconds > 0 else None
    found = _RETRY_RE.search(text)
    if found and float(found.group(1)) > 0:
        return timedelta(seconds=float(found.group(1)))
    return None


def quota_failure(reply: Dict[str, Any]) -> str:
    """The error text when a reply is a usage-limit failure, else ""."""
    failure = transport_failure(reply) or ""
    return failure if failure and QUOTA_RE.search(failure) else ""


def when_text(at: datetime, now: datetime) -> str:
    """"about 4:30 PM" (rounded up to a thirty: it's an estimate), with the day when it isn't today."""
    shown = thirty_ceil(at)
    clock = f"about {format_clock(shown)}"
    days = (shown.date() - now.date()).days
    if days <= 0:
        return clock
    if days == 1:
        return f"tomorrow at {clock}"
    return f"{shown.strftime('%A')} at {clock}"


# --- the shared state ---

def _parse(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


class FallbackState:
    """What's out and until when, in the state database. Fields:
    try_again_at: when the chosen model may be tried again ("" while it isn't out); step: how many guessed waits
    were used this time; using: who answered last while it's out ("fallback", "device" or "none"); told: whether
    the conversation has been told this time; back: say once that it's back; calls: the fallback model's calls
    (times) in the window; fallback_until: the fallback model is out too, until then ("max": until the chosen
    one is back)."""

    def __init__(self, db) -> None:
        self.db = db

    def load(self) -> Dict[str, Any]:
        raw = self.db.get_sync_meta(KEY) if self.db is not None else None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def save(self, data: Dict[str, Any]) -> None:
        if self.db is not None:
            self.db.set_sync_meta(KEY, json.dumps(data, sort_keys=True))

    def update(self, **fields: Any) -> Dict[str, Any]:
        data = self.load()
        data.update(fields)
        self.save(data)
        return data

    # --- the chosen model ---

    def try_again_at(self) -> Optional[datetime]:
        return _parse(self.load().get("try_again_at"))

    def out(self, now: datetime) -> bool:
        """The chosen model's quota is used up and its reset time hasn't come yet: don't call it."""
        resume = self.try_again_at()
        return resume is not None and now < resume

    def active(self) -> bool:
        """It ran out and hasn't answered since (even if its reset time has come)."""
        return self.try_again_at() is not None

    def chosen_out(self, now: datetime, error: str) -> datetime:
        """The chosen model said its usage limit is reached: when to try it again."""
        data = self.load()
        wait = reset_after(error)
        step = int(data.get("step") or 0)
        if wait is None:
            wait = timedelta(minutes=GUESS_MINUTES[min(step, len(GUESS_MINUTES) - 1)])
            step += 1
        resume = now + wait
        data.update(try_again_at=resume.isoformat(), step=step)
        self.save(data)
        logger.warning("The chosen model's usage limit is reached; trying it again after %s.", resume.isoformat())
        return resume

    def chosen_back(self) -> None:
        """The chosen model answered again: everything is as usual; say so once if the user was told."""
        data = self.load()
        if not data.get("try_again_at"):
            return
        told = bool(data.get("told"))
        until = data.get("fallback_until") or ""
        self.save({"calls": data.get("calls") or [], "back": told,
                   **({"fallback_until": until} if until and until != "max" else {})})
        logger.info("The chosen model answered again.")

    # --- the fallback model ---

    def calls_in_window(self, now: datetime) -> List[str]:
        return [c for c in self.load().get("calls") or [] if (_parse(c) or now) > now - WINDOW]

    def fallback_ready(self, now: datetime, cap: int) -> bool:
        data = self.load()
        until = data.get("fallback_until") or ""
        if until == "max" or (until and (_parse(until) or now) > now):
            return False
        return len(self.calls_in_window(now)) < max(0, int(cap))

    def count_fallback(self, now: datetime) -> None:
        self.update(calls=self.calls_in_window(now) + [now.isoformat()])

    def fallback_out(self, now: datetime, error: str) -> None:
        wait = reset_after(error)
        self.update(fallback_until=(now + wait).isoformat() if wait else "max")
        logger.warning("The fallback model's usage limit is reached too.")

    def using(self, who: str) -> None:
        if self.load().get("using") != who:
            self.update(using=who)


# --- the engine ---

class FallbackEngine:
    """Wraps the app's engine: while the chosen model is out, calls go to the fallback model (within its cap);
    when nothing is left, the reply is a usage-limit failure (EXHAUSTED) that the turn keeps for later. Anything
    it doesn't own (model, model_name, a test's responses) is the wrapped engine's."""

    _OWN = ("primary", "config", "state", "clock", "_fallback", "_device_ready")

    def __init__(self, primary: Any, config, state_db, clock: Callable[[], datetime],
                 fallback: Any = None, device_ready: Optional[Callable[[], bool]] = None) -> None:
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "state", FallbackState(state_db))
        object.__setattr__(self, "clock", clock)
        object.__setattr__(self, "_fallback", fallback)
        object.__setattr__(self, "_device_ready", device_ready)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "primary"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.primary, name, value)

    @property
    def cap(self) -> int:
        return int(getattr(self.config.assistant, "fallback_calls_per_5h", 20) or 0)

    def fallback_engine(self) -> Any:
        """The fallback model through agy (the subscription), or None when agy isn't here."""
        if self._fallback is None:
            from thirties_core.inference import AntigravityInferenceEngine
            engine = AntigravityInferenceEngine(self.config)
            engine.model = getattr(self.config.assistant, "fallback_model", "") or "gemini-4-argon-low"
            object.__setattr__(self, "_fallback", engine)
        engine = self._fallback
        available = getattr(engine, "is_available", None)
        return engine if available is None or available() else None

    def fallback_ready(self, now: datetime) -> bool:
        return self.state.fallback_ready(now, self.cap) and self.fallback_engine() is not None

    def device_ready(self) -> bool:
        if self._device_ready is not None:
            return bool(self._device_ready())
        return device_brain_ready(self.config)

    def is_available(self) -> bool:
        available = getattr(self.primary, "is_available", None)
        return bool(available() if available else True)

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        now = self.clock()
        if not self.state.out(now):  # as usual, or its reset time has come: this real call tries it again
            reply = self.primary.chat(messages, tools)
            error = quota_failure(reply)
            if not error:
                if self.state.active() and not transport_failure(reply):
                    self.state.chosen_back()
                return reply
            self.state.chosen_out(now, error)
        if self.fallback_ready(now):
            self.state.count_fallback(now)
            reply = self.fallback_engine().chat(messages, tools)
            error = quota_failure(reply)
            if not error:
                if not transport_failure(reply):
                    self.state.using("fallback")
                return reply  # a timeout goes back as it is: the retrying wrapper tries again
            self.state.fallback_out(now, error)
        return {"role": "assistant", "content": EXHAUSTED, "tool_calls": []}


def device_brain_ready(config) -> bool:
    """The on-device model may stand in: offered (the developer switch) and installed. Never downloads it."""
    from thirties_core.brains import gemma_offered
    if not gemma_offered(config):
        return False
    try:
        from thirties_core.brains.slm.runtime import slm_unavailable_reason
        return slm_unavailable_reason(config) is None
    except Exception as e:
        logger.debug("The on-device model can't stand in: %s", e)
        return False


def usual_model_out(state_db) -> bool:
    """The usual model ran out and no real turn has had an answer from it since. Work that only tidies up (sorting
    the lists) waits meanwhile: it never calls the usual model (that would be a probe) or the fallback model (its
    small quota is for the conversation). Past the reset time it still waits, until a turn has its answer."""
    return state_db is not None and FallbackState(state_db).active()


def with_fallback(engine: Any, config, state_db, tz) -> Any:
    """The app's engine with the automatic fallback (``[assistant] quota_fallback``), for the Gemini sources only:
    a mock, the on-device model or a test engine is returned as it is."""
    from thirties_core.astronomy import get_current_time
    from thirties_core.inference import AntigravityInferenceEngine, GeminiInferenceEngine, GeminiSourcesEngine
    if not getattr(config.assistant, "quota_fallback", True) or state_db is None:
        return engine
    if not isinstance(engine, (GeminiSourcesEngine, AntigravityInferenceEngine, GeminiInferenceEngine)):
        return engine
    return FallbackEngine(engine, config, state_db, lambda: get_current_time(tz))


def engine_of(manager) -> Optional[FallbackEngine]:
    """The fallback engine under a conversation's engine (inside the retrying wrapper), if it has one."""
    args = getattr(manager, "_brain_args", None)
    engine = args[1] if args else None
    for _ in range(3):
        if engine is None or isinstance(engine, FallbackEngine):
            return engine
        try:
            engine = object.__getattribute__(engine, "engine")
        except AttributeError:
            return None
    return None


# --- the turn (ConversationManager calls these) ---

def _now(manager) -> datetime:
    from thirties_core.astronomy import get_current_time
    return get_current_time(manager.day_plan.day_start.tzinfo)


def before_turn(manager, batch, via: str) -> Optional[str]:
    """Pick who answers this turn. While the chosen model is out: the fallback model (the engine does it), else the
    on-device brain (switched to for the turn, never saved as the user's choice), else nobody: the messages are
    kept for later and the reply says until when (returned; the turn doesn't run). Once the reset time has come,
    the turn goes to the chosen brain again: that's the real call that tries it."""
    engine = engine_of(manager)
    if engine is None:
        return None
    now = _now(manager)
    plan = "chosen"
    if engine.state.out(now) and not engine.fallback_ready(now):
        plan = "device" if engine.device_ready() else "none"
    standing_in = getattr(manager, "_device_standing_in", False)
    if plan == "device" and manager.brain_name != "slm":
        manager._use_brain("slm")
        manager._device_standing_in = True
        engine.state.using("device")
    elif plan != "device" and standing_in:
        from thirties_core.brains import brain_choice
        manager._use_brain(brain_choice(manager.scheduler.config if manager.scheduler else None,
                                        manager.scheduler.state_db if manager.scheduler else None))
        manager._device_standing_in = False
    if plan == "none":
        return keep_for_later(manager, batch, via)
    return None


def after_lost(manager, error) -> bool:
    """A turn was lost to the usage limit (nothing changed yet). True when the on-device brain can take it now
    (the turn is run again on it); the caller has already taken the turn's messages back out."""
    engine = engine_of(manager)
    if engine is None or not QUOTA_RE.search(str(error)) or getattr(manager, "_device_standing_in", False):
        return False
    now = _now(manager)
    if engine.state.out(now) and not engine.fallback_ready(now) and engine.device_ready():
        manager._use_brain("slm")
        manager._device_standing_in = True
        engine.state.using("device")
        return True
    return False


def lost_wait(manager, error, retry_after: str, note: str) -> tuple:
    """For a turn lost to the usage limit with no other assistant left: wait until the chosen model's reset time,
    and say until when. Anything else keeps the usual wait and words."""
    engine = engine_of(manager)
    if engine is None or not QUOTA_RE.search(str(error)):
        return retry_after, note
    resume = engine.state.try_again_at()
    now = _now(manager)
    if resume is None or now >= resume:
        return retry_after, note
    engine.state.using("none")
    return resume.isoformat(), unavailable_reply(manager, resume, now)


def unavailable_reply(manager, resume: datetime, now: datetime) -> str:
    """Until when the assistant is unavailable, with what's next (Python's slice, as the button shows it)."""
    from thirties_core import whats_next
    from thirties_core.messages import join_sections
    try:
        slice_ = whats_next.summary(manager.tools, now, "next") if manager.tools.is_current(manager.day_plan) else ""
    except Exception as e:
        logger.warning("Couldn't add what's next: %s", e)
        slice_ = ""
    return join_sections(UNAVAILABLE.format(when=when_text(resume, now)), slice_)


def keep_for_later(manager, batch, via: str) -> str:
    """No assistant has any quota left: the messages are kept, marked unanswered, until the chosen model's reset
    time (the lost-turn retry answers them then); the reply says until when, with what's next (Python's)."""
    engine = engine_of(manager)
    now = _now(manager)
    resume = engine.state.try_again_at() or now
    engine.state.using("none")
    manager._catch_up()
    waiting = manager.unanswered()
    attempts = max([int(m.get("attempts") or 1) for m in waiting] or [1])
    for i in batch:
        manager.brain.messages.append({"role": "user", "content": i.text, "at": i.at, "via": i.via,
                                       "unanswered": True, "attempts": attempts, "retry_after": resume.isoformat()})
    for m in waiting:  # the earlier ones wait for the same time
        m["retry_after"] = resume.isoformat()
    if via == "retry":  # the background trying a kept message again: nothing new was said, so nothing is added
        manager._save()
        return ""
    reply = unavailable_reply(manager, resume, now)
    manager.brain.messages.append({"role": "assistant", "content": reply, "display_only": True, "kind": "status"})
    manager._stamp(via)
    manager._save()
    return reply


def with_notice(manager, reply: str) -> str:
    """The reply with the one-time word that a simpler assistant is answering (or that the usual one is back)."""
    from thirties_core.messages import join_sections
    engine = engine_of(manager)
    if engine is None:
        return reply
    data = engine.state.load()
    if data.get("back"):
        engine.state.update(back=False)
        return join_sections(BACK, reply)
    if data.get("try_again_at") and data.get("using") in ("fallback", "device") and not data.get("told"):
        resume = _parse(data["try_again_at"]) or _now(manager)
        engine.state.update(told=True)
        return join_sections(SIMPLER.format(when=when_text(resume, _now(manager))), reply)
    return reply


__all__ = ["BACK", "EXHAUSTED", "FallbackEngine", "FallbackState", "SIMPLER", "UNAVAILABLE", "after_lost",
           "before_turn", "device_brain_ready", "engine_of", "keep_for_later", "lost_wait", "quota_failure",
           "reset_after", "unavailable_reply", "usual_model_out", "when_text", "with_fallback", "with_notice"]
