"""How long each message would take with a given on-device model, measured on this device.

The user's idea (2026-10-30): offer the most capable model a device can handle,
and let people pick a bigger one knowing the cost: "you can pick this bigger
model; here's how long every message will take". This probe answers that for
whatever runtime is loaded (LiteRT-LM or llama.cpp), using only the calls the
SLM brain itself makes, in the same shape: the parser session reads its
instruction once, then each message costs a parse plus a short voice rewrite.

It takes about as long as the warm-up plus six messages (seconds on a GPU), so
the app can run it in the background after a model download.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable, Dict, List, Optional

from thirties_core.brains.slm.parse import ACTION_SCHEMA, SYSTEM_PROMPT, build_context, extract_json

PROBE_SCHEDULE = ["Work 8:30 AM-4:00 PM", "Dinner 6:00 PM-7:00 PM", "Composing 7:00 PM-8:30 PM",
                  "Sleep 11:00 PM-7:00 AM"]

# (message, the draft reply the voice pass would rewrite): ordinary messages from real days
PROBE_MESSAGES = [
    ("push composing back half an hour", "Composing now runs 7:30 PM-9:00 PM."),
    ("finished the taxes around 4", "Marked Taxes done at 4:00 PM."),
    ("dinner at 6:30 for an hour then a 30 minute walk", "Added Dinner 6:30 PM-7:30 PM and Walk 7:30 PM-8:00 PM."),
    ("what's on tomorrow?", "Tomorrow: Work 8:30 AM-4:00 PM, then free until Sleep at 11:00 PM."),
    ("yes please", "Added Reading 9:00 PM-9:30 PM."),
    ("work went til 4:45", "Work now ends at 4:45 PM."),
]

def _voice_prompt() -> str:
    from thirties_core.brains.slm.brain import VOICE_PROMPT  # lazily: the brain module is large
    return VOICE_PROMPT


def probe(runtime: Any, messages: Optional[List[tuple]] = None, voice: bool = True,
          clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Time the brain's real call pattern on ``runtime``; returns seconds (see ``describe``)."""
    messages = messages or PROBE_MESSAGES
    result: Dict[str, Any] = {"load_seconds": round(float(getattr(runtime, "load_seconds", 0.0) or 0.0), 2)}

    start = clock()
    parser = runtime.parser(SYSTEM_PROMPT)
    parser.warm()
    voice_session = runtime.parser(_voice_prompt()) if voice else None
    if voice_session is not None:
        voice_session.ask('User said: "thanks"\nDRAFT:\nYou\'re welcome!')
    result["warm_seconds"] = round(clock() - start, 2)

    parse_times, voice_times, totals, parsed = [], [], [], 0
    for message, draft in messages:
        context = build_context("Thursday 3:00 PM", PROBE_SCHEDULE, "", None, [], message)
        t0 = clock()
        raw = extract_json(parser.ask(context))
        t1 = clock()
        parsed += bool(raw and "actions" in raw)
        if voice_session is not None:
            voice_session.ask(f'User said: "{message}"\nDRAFT:\n{draft}')
        t2 = clock()
        parse_times.append(t1 - t0)
        voice_times.append(t2 - t1)
        totals.append(t2 - t0)

    t0 = clock()
    constrained = runtime.json_call(SYSTEM_PROMPT, build_context("Thursday 3:00 PM", PROBE_SCHEDULE, "", None, [],
                                                                 messages[0][0]), ACTION_SCHEMA)
    result["json_call_seconds"] = round(clock() - t0, 2)
    result["json_call_ok"] = isinstance(constrained, dict) and isinstance(constrained.get("actions"), list)
    for close in (parser.close, voice_session.close if voice_session else None):
        if close:
            close()

    result.update({
        "messages": len(messages),
        "parsed": parsed,
        "parse_seconds_median": round(statistics.median(parse_times), 2),
        "voice_seconds_median": round(statistics.median(voice_times), 2) if voice else 0.0,
        "message_seconds_median": round(statistics.median(totals), 2),
        "message_seconds_max": round(max(totals), 2),
    })
    return result


def describe(result: Dict[str, Any]) -> str:
    """One line for a settings page: what a message costs with this model on this device."""
    per = result["message_seconds_median"]
    first = result.get("load_seconds", 0) + result.get("warm_seconds", 0)
    per_text = "under a second" if per < 1 else f"about {per:.0f} second{'s' if round(per) != 1 else ''}"
    return f"Replies take {per_text} each, after about {max(1, round(first))} s to start the model."
