"""Scripted one-prompt turns that touch every part of the turn bookkeeping, for pinning the switch's "off" to the turn
loop as it was before (test_turn_bookkeeping). Each scenario returns what a user and the model would see: the
final reply, how many model calls the turn took, and the texts sent to the model on the last call. Invented items.

Run directly to print the outcomes as JSON (that's how the pinned baseline was recorded):

    python3 -m tests.turn_scenarios
"""

import json
import re
import sys

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine, parse_model_directives


def _reply(text):
    content, calls = parse_model_directives(text)
    return {"role": "assistant", "content": content, "tool_calls": calls}


def _manager(engine):
    sched = make_scheduler()
    plan, pending = sched.build_day_plan(DAY)
    return ConversationManager(plan, [], pending, sched, inference_engine=engine, active_notes=[])


SCENARIOS = {
    # a refusal, then a success on something else, then a reply claiming both
    "failure_then_other_success": (
        "move the chore to 6 and add ferns at 7",
        ['CALL update_event {"event": "Nonexistent chore", "start": "6:00 PM"}\n'
         'CALL add_event {"title": "Water the ferns", "start": "7:00 PM", "duration": "30 min"}',
         "All set: moved and added.", "All set: moved and added."]),
    # no reply at all, then a refusal in the retry, then a reply
    "empty_then_refusal": (
        "move the chore to 6",
        ["", 'CALL update_event {"event": "Nonexistent chore", "start": "6:00 PM"}',
         "I couldn't find that chore. Which one did you mean?", "Still looking."]),
    # a claim with no call, its correction, then the next turn
    "claim_then_next_turn": (
        "hello",
        ["Your plan has been updated.", "Nothing changed yet; what should I add?", "Sure."]),
    # a reply glued onto a call
    "glued_call": (
        "what's on?",
        ["CALL list_schedule {}Here is the day.", "Here is the day, again."]),
    # lookups only, every round
    "rounds_capped": (
        "show me",
        ["CALL list_schedule"] * 6),
}


def run(name: str) -> dict:
    message, script = SCENARIOS[name]
    set_debug_time(at("09:00"))
    try:
        engine = MockInferenceEngine([_reply(t) for t in script])
        manager = _manager(engine)
        out = manager.send_user_message(message)
        if name == "claim_then_next_turn":
            out = manager.send_user_message("thanks")
        last = engine.call_history[-1] if engine.call_history else []
        return {"reply": out, "calls": len(engine.call_history),
                "last_sent": [re.sub(r"\[id [0-9a-f]+\]", "[id]", str(m.get("content") or ""))[:120]
                              for m in last if m.get("role") != "system"]}
    finally:
        set_debug_time(None)


if __name__ == "__main__":
    json.dump({name: run(name) for name in SCENARIOS}, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
