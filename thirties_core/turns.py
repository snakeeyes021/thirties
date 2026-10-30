"""Taking turns: one turn at a time per day's conversation, bursts answered once, lost turns kept, and the
desktop's turns mirrored to the phone. Used by ConversationManager, so the app and Telegram get it alike.

- **A burst of quick messages gets one reply** (A8: 10-26 10:17–10:18, three replies to three messages; 10-19
  22:32, two "Logged!"). Messages that arrive while a turn is under way (or within ``BURST_QUIET`` of each other,
  when a client asks for that) wait in the day's inbox, and the next turn takes them all.
- **One turn at a time across clients.** The window and the background service (which runs Telegram) each hold a
  ConversationManager for the same day; they share one gate, so their turns never interleave and overwrite each
  other's messages (10-28 15:55: two different replies to one message).
- **Model timeouts are retried, and a turn is never silently lost** (T1: 10-17 "Errno 7" ×8, the user's "Cats are
  fed" never landed; 10-27 17:57 "Try again? You timed out"). A call that fails in transport is tried again after
  ``RETRY_WAITS``; if it still fails, the user's message is kept, marked unanswered, and answered later
  (BackgroundService.answer_lost_turns).
- **The desktop chat is mirrored to the phone** (T2, 10-28 13:02: "messages I send in the thirties desktop app do
  not get sent to my phone"): mirrors registered here (the background service's Telegram bridge) get each turn
  that came from the app.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BURST_QUIET = 3.0       # seconds of quiet that end a burst (clients that wait for it: Telegram, the app)
BURST_MAX_WAIT = 12.0   # never wait longer than this for a burst to end
RETRY_WAITS = (5.0, 20.0)  # seconds before each retry of a model call that failed in transport
LOST_TURN_REPLY = ("Sorry, I couldn't reach the model just now ({why}). I've kept your message and will answer it "
                   "as soon as I can.")
LOST_TURN_PREFIX = "Sorry, I couldn't reach the model"
LOST_RETRY_MINUTES = (2, 5, 10, 20, 30)  # then every 30 minutes
LOST_GIVE_UP_HOURS = 6
QUOTA_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|too many requests|usage limit|"
                      r"limit (reached|exceeded)", re.IGNORECASE)


# --- the inbox and the gate ---

@dataclass
class Incoming:
    text: str
    via: str
    at: str  # ISO time it arrived: the user's message is stamped with this, not when the reply is saved
    arrived: float = 0.0
    taken: bool = False


class TurnGate:
    """One per day's conversation in this process: its lock (a turn, or a notice being added) and its inbox."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._inbox: List[Incoming] = []
        self._guard = threading.Condition()

    def enqueue(self, text: str, via: str, at: str) -> Incoming:
        item = Incoming(text, via, at, time.monotonic())
        with self._guard:
            self._inbox.append(item)
            self._guard.notify_all()
        return item

    def wait_quiet(self, quiet: float, max_wait: float = BURST_MAX_WAIT) -> None:
        """Wait until no message has arrived for ``quiet`` seconds (a burst is over), at most ``max_wait``."""
        if quiet <= 0:
            return
        deadline = time.monotonic() + max_wait
        with self._guard:
            while True:
                pending = [i for i in self._inbox if not i.taken]
                if not pending:
                    return
                left = pending[-1].arrived + quiet - time.monotonic()
                if left <= 0 or time.monotonic() >= deadline:
                    return
                self._guard.wait(min(left, deadline - time.monotonic()))

    def take(self) -> List[Incoming]:
        """Everything waiting, in the order it arrived; it's this turn's now."""
        with self._guard:
            batch = [i for i in self._inbox if not i.taken]
            for i in batch:
                i.taken = True
            self._inbox = []
        return batch

    def waiting(self) -> int:
        with self._guard:
            return sum(1 for i in self._inbox if not i.taken)


_GATES: Dict[Tuple[str, str], TurnGate] = {}
_GATES_LOCK = threading.Lock()


def gate_for(db_path: Optional[str], day: Optional[str]) -> TurnGate:
    """The gate for a day's conversation in a database; without a database, a gate of its own."""
    if not db_path or not day:
        return TurnGate()
    with _GATES_LOCK:
        return _GATES.setdefault((str(db_path), str(day)), TurnGate())


# --- model calls that fail in transport ---

class BrainUnavailable(RuntimeError):
    """The model couldn't be reached, even after retrying."""

    @property
    def why(self) -> str:
        text = str(self).lower()
        if "timed out" in text or "timeout" in text:
            return "it timed out"
        if QUOTA_RE.search(text):
            return "its usage limit is reached"
        return "the connection failed"


def transport_failure(response: Dict[str, Any]) -> Optional[str]:
    from thirties_core.inference import TRANSPORT_FAILURE_PREFIXES
    content = str((response or {}).get("content") or "")
    return content if content.startswith(TRANSPORT_FAILURE_PREFIXES) else None


class RetryingEngine:
    """Wraps an inference engine: a call that fails in transport (the engines return their error as the reply's
    text, which used to reach the user) is tried again, and if it keeps failing, BrainUnavailable is raised.
    A usage-limit failure isn't retried (it won't clear in seconds)."""

    _OWN = ("engine", "waits")

    def __init__(self, engine: Any, waits: Optional[Tuple[float, ...]] = None) -> None:
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "waits", RETRY_WAITS if waits is None else waits)

    @classmethod
    def wrap(cls, engine: Any) -> Any:
        return engine if engine is None or isinstance(engine, cls) else cls(engine)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "engine"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.engine, name, value)  # e.g. a test's scripted responses go to the real engine

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        failure = ""
        for attempt, wait in enumerate((*self.waits, None)):
            try:
                response = self.engine.chat(messages, tools)
                failure = transport_failure(response) or ""
            except (TimeoutError, ConnectionError, OSError) as e:
                failure = f"{type(e).__name__}: {e}"
            if not failure:
                return response
            if QUOTA_RE.search(failure) or wait is None:
                break
            logger.warning("Model call failed (%s); trying again in %.0fs (attempt %d).", failure[:160], wait,
                           attempt + 1)
            time.sleep(wait)
        raise BrainUnavailable(failure)


# --- mirroring the desktop's turns to the phone ---

Mirror = Callable[[List[str], str, bool], None]  # (the app's messages, the reply, reply already sent there?)
_MIRRORS: List[Mirror] = []


def add_mirror(fn: Mirror) -> None:
    if fn not in _MIRRORS:
        _MIRRORS.append(fn)


def remove_mirror(fn: Mirror) -> None:
    if fn in _MIRRORS:
        _MIRRORS.remove(fn)


def mirror_turn(batch: List[Incoming], reply: str, runner_via: str) -> None:
    """Tell the mirrors about a turn that had messages from the desktop app. When the turn was run for Telegram,
    the reply goes there anyway; only the desktop's messages are echoed. Runs in the background: the turn
    doesn't wait on the network."""
    from_app = [i.text for i in batch if i.via == "app"]
    if not from_app or not _MIRRORS:
        return
    reply_sent = runner_via == "telegram"
    for fn in list(_MIRRORS):
        def run(fn=fn) -> None:
            try:
                fn(from_app, reply, reply_sent)
            except Exception as e:
                logger.warning("Mirroring a desktop turn failed: %s", e)
        if reply_sent or not MIRROR_IN_BACKGROUND:
            run()  # on Telegram's own thread: the echo goes out before the reply does
        else:
            threading.Thread(target=run, daemon=True, name="MirrorTurn").start()


MIRROR_IN_BACKGROUND = True  # tests turn this off to see the mirror's sends in order


__all__ = ["BURST_QUIET", "BrainUnavailable", "Incoming", "LOST_TURN_PREFIX", "LOST_TURN_REPLY", "RetryingEngine",
           "TurnGate", "add_mirror", "gate_for", "mirror_turn", "remove_mirror", "transport_failure"]
