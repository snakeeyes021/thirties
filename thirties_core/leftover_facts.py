"""What the morning says about the leftovers from before: one set of facts, read from the plan as it stands.

A leftover kept this morning is "on today's plan" when today's plan has it (a planned row for its item, or with its
title), never because of the window it was kept for. A kept item can already be on the plan (put there before the
morning was written), and one meant for today can be off it (resting after missed days, no room, taken off since).
Counting by the window made the morning contradict itself: "0 on today's plan" above a line that gave the item's
time today, or a count of items "on today's plan" that weren't.

So everything said about the leftovers comes from ``facts``, read when it's said, after the day's placements:
the notice Python writes (``notice``), the leftover list the model reads, and what propose_leftovers answers. The model's
own words are checked against the same facts (``wrong``): a count of kept, on-the-plan or proposed-drop items that
doesn't match, or a kept item said to be on today's plan when it isn't (or not today when it is), goes back for
one rewrite; if the rewrite still disagrees, those sentences go and Python's notice stands in for them
(``set_straight``).

Invented example: a potter kept "Sand the spare paddle" for the week, and it was already on today's plan at
2:30 PM. The morning says one kept, one on today's plan, never none.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _record(assistant: Any) -> Dict[str, Any]:
    db = getattr(assistant, "_db", None)
    raw = db.get_sync_meta(assistant._rollover_key()) if db is not None else None
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def span_of(plan: Any, item: Dict[str, Any]) -> Any:
    """The item's planned row on ``plan`` (its first), or None. One not marked yet (not_marked.py: stored as unknown
    too) is still on the plan: the user can still mark it."""
    from thirties_core.models import SpanStatus
    from thirties_core.not_marked import unmarked_spans
    if plan is None:
        return None
    title = str(item.get("title") or "").strip().lower()
    task_id = item.get("task_id") or ""
    rows = [s for s in plan.active_spans() if not s.is_envelope]
    unmarked = {s.id for s in unmarked_spans(rows, None)}
    on = sorted((s for s in rows if (s.status == SpanStatus.PLANNED or s.id in unmarked)
                 and ((task_id and s.task_id == task_id) or s.title.strip().lower() == title)),
                key=lambda s: s.start_dt)
    return on[0] if on else None


def facts(assistant: Any) -> Optional[Dict[str, Any]]:
    """This morning's leftovers as the plan has them now, or None when the morning kept or dropped nothing.
    {"keep": [{"title", "name", "span", "where", "when"}], "on_plan": the kept rows on today's plan,
    "drop": names proposed for dropping, "overflow": names that don't fit today (and aren't on it)}."""
    if assistant is None:
        return None
    data = _record(assistant)
    lo = data.get("leftovers") or {}
    keep, drop = list(lo.get("keep") or []), list(lo.get("drop") or [])
    ended = list(lo.get("end") or [])
    if not keep and not drop and not ended and not data.get("overflow"):
        return None
    from thirties_core.item_path import named
    from thirties_core.messages import carried_where
    plan = getattr(assistant.pt, "day_plan", None)
    tasks = list(getattr(assistant.s, "tasks", []) or [])
    rows = []
    # --- not marked yet (no day, asked about) and ended (its day is over) are neither kept nor dropped ---
    unknown = [named(tasks, k["title"], k.get("task_id")) for k in keep
               if k.get("unknown") and span_of(plan, k) is None]
    keep = [k for k in keep if not (k.get("unknown") and span_of(plan, k) is None)]
    # --- end not marked yet ---
    for k in keep:
        span = span_of(plan, k)
        rows.append({"title": k["title"], "name": named(tasks, k["title"], k.get("task_id")), "span": span,
                     "where": carried_where(plan, k), "when": str(k.get("when") or "")})
    overflow = [named(tasks, t) for t in data.get("overflow") or [] if span_of(plan, {"title": t}) is None]
    return {"keep": rows, "on_plan": [r for r in rows if r["span"] is not None],
            "drop": [named(tasks, d["title"], d.get("task_id")) for d in drop], "overflow": overflow,
            "unknown": unknown, "ended": [named(tasks, e["title"], e.get("task_id")) for e in ended]}


def notice(assistant: Any) -> str:
    """The morning's word on the leftovers, as Python says it: counts and lines from the plan as it stands."""
    from thirties_core.carry import notice_text
    f = facts(assistant)
    if f is None:
        return ""
    out = notice_text(len(f["keep"]), len(f["on_plan"]), len(f["drop"]), f["overflow"],
                      [(r["name"], r["where"]) for r in f["keep"] if r["when"] or r["span"] is not None],
                      unknown=f.get("unknown") or [], ended=f.get("ended") or [])
    assistant.leftover_notice_said = out  # so the morning's facts can say it again once the day is built
    return out


def restate(assistant: Any, text: str) -> str:
    """The morning's facts with the leftovers' notice read again, now that every placement of the day is made
    (morning.prepare): the notice written mid-way is swapped for one from the plan as it finally stands."""
    said = getattr(assistant, "leftover_notice_said", "") if assistant is not None else ""
    if not said or not text or said not in text:
        return text
    try:
        fresh = notice(assistant)
    except Exception as e:  # never costs the user the morning
        logger.warning("Couldn't read the leftovers again: %s", e)
        return text
    return text.replace(said, fresh) if fresh else text


def summary(f: Optional[Dict[str, Any]], names: bool = True) -> str:
    """The counts (and names, unless the lines beside it give them) for the model, in one line (not for the
    user: the model gets it through ``for_model``, and a copy in a reply is rewritten, internal_text.py)."""
    if not f:
        return ""
    from thirties_core.clock import format_clock, thirty_of
    on = [f"{r['name']} at {format_clock(thirty_of(r['span'].start_dt))}" for r in f["on_plan"]]
    off = [f"{r['name']} ({r['where']})" for r in f["keep"] if r["span"] is None]
    if not names:
        out = (f"kept {len(f['keep'])} from before; {len(on)} of them on today's plan; {len(off)} not on today's "
               f"plan; {len(f['drop'])} proposed for dropping")
        return out + (f"; {len(f['overflow'])} that doesn't fit today" if f["overflow"] else "") \
            + _unknown_ended(f, names) + "."
    out = f"kept {len(f['keep'])} from before; {len(on)} of them on today's plan" + (f" ({', '.join(on)})" if on else "")
    out += f"; {len(off)} not on today's plan" + (f" ({', '.join(off)})" if off else "")
    out += f"; {len(f['drop'])} proposed for dropping" + (f" ({', '.join(f['drop'])})" if f["drop"] else "")
    if f["overflow"]:
        out += "; doesn't fit today: " + ", ".join(f["overflow"])
    out += _unknown_ended(f, names)
    return out + "."


def _unknown_ended(f: Dict[str, Any], names: bool = True) -> str:
    """Not marked yet (asked about, no day) and ended (its day is over), for the model's counts."""
    out = ""
    for key, said in (("unknown", "not marked yet, asked about, no day"), ("ended", "events ended silently, never mention them")):
        if f.get(key):
            out += f"; {len(f[key])} {said}" + (f" ({', '.join(f[key])})" if names else "")
    return out


def plain(f: Optional[Dict[str, Any]]) -> str:
    """The most the user hears about the leftovers' counts, in one plain sentence: how many wait in Before today
    (kept items off today's plan, and proposed drops). "" when nothing waits there."""
    if not f:
        return ""
    from thirties_core.internal_text import waiting_sentence
    return waiting_sentence(sum(1 for r in f["keep"] if r["span"] is None) + len(f["drop"]))


def for_model(f: Optional[Dict[str, Any]], names: bool = True) -> str:
    """The counts for the model, marked as its own (a copy of the line is taken out of the reply,
    internal_text.py), with the plain sentence it may say instead."""
    if not f:
        return ""
    say = plain(f)
    return ("(for the assistant only, never copy it: " + summary(f, names) + ") "
            + (f"If you mention what's left from before, say at most: \"{say}\"" if say else
               "Nothing from before waits in Before today."))


def where_now(f: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Each kept item's title (lower case) with where it is now, for the model's leftover list."""
    return {r["title"].lower(): r["where"] for r in (f or {}).get("keep", [])}


def tool_result(assistant: Any) -> str:
    """What propose_leftovers answers: what's in effect, read from the plan after its placements."""
    f = facts(assistant) or {"keep": [], "on_plan": [], "drop": [], "overflow": [], "unknown": [], "ended": []}
    lines = []
    if f["keep"]:
        lines.append("Keep:")
        lines += [f"{r['name']} – {r['where'] if r['span'] is not None or r['when'] in ('', 'today') else r['when']}"
                  for r in f["keep"]]
    if f["drop"]:
        lines.append("Drop:")
        lines += f["drop"]
    # --- not marked yet and ended, with what each leftover carries (past_one_offs.py) ---
    if f.get("unknown"):
        lines.append("Not marked yet (no day; the one question asks whether it happened):")
        lines += f["unknown"]
    if f.get("ended"):
        lines.append("Ended silently (an event whose time is over; stored as unknown; never mention it):")
        lines += f["ended"]
    carried = _carried(assistant)
    if carried:
        lines += ["What each one carries:"] + carried
    # --- end not marked yet and ended ---
    return ("In effect now, no approval needed. As the plan stands now " + for_model(f, names=False) + " What isn't on "
            "today's plan waits in Before today for a later day; drops are applied by themselves after today unless "
            "the user says otherwise. Say it as a short notice, not a question, and never as a tally of kept, on-the-plan and "
            "dropped items. Where each one is now:\n" + "\n".join(lines))


def _carried(assistant: Any) -> List[str]:
    """Each leftover with what it carries, as the morning's list gave it (past_one_offs.line)."""
    from thirties_core.past_one_offs import line
    lo = _record(assistant).get("leftovers") or {}
    try:
        day = assistant._today()
    except Exception:
        return []
    out = []
    for k in (lo.get("keep") or []) + (lo.get("drop") or []) + (lo.get("end") or []):
        said = line(k.get("facts") or {}, day)
        if said:
            out.append(f"{k['title']}: " + "; ".join(said))
    return out


# --- the model's words, checked against the facts ---

_WORD_NUMS = {"no": 0, "none": 0, "zero": 0, "nothing": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "both": 2,
              "a single": 1, "all": -1}
_NUM = r"(\d+|a single|no|none|zero|nothing|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both|all)"
_UNIT = r"(?:(?:of (?:them|those|these)|items?|things?|leftovers?|tasks?|ones?)\s+)?"
_PLAN = r"(?:today's|the|your|today’s) (?:plan|schedule)"
ON_COUNT_RE = re.compile(rf"(?<![:\d])\b{_NUM}\s+{_UNIT}(?:(?:is|are|went|go|now|already|sit|sits|got|were|was|"
                         rf"put|placed|landed|made it)\s+){{0,3}}(?:on|onto) {_PLAN}(?!\s+for)", re.IGNORECASE)
KEPT_COUNT_RE = re.compile(rf"\bkept\s+(?:all\s+)?{_NUM}\b(?!\s*(?::\d|[ap]\.?m\b))"
                           rf"|(?<![:\d])\b{_NUM}\s+{_UNIT}(?:(?:were|are|is|I've|I have|I)\s+)?kept\b", re.IGNORECASE)
DROP_COUNT_RE = re.compile(rf"\b(?:drop|dropping)\s+{_NUM}\b(?!\s*(?::\d|[ap]\.?m\b|hours?|minutes?))"
                           rf"|(?<![:\d])\b{_NUM}\s+{_UNIT}(?:(?:to drop|I'd drop|I would drop|for dropping|"
                           rf"(?:are |were )?(?:proposed )?(?:to be |for )?dropped))", re.IGNORECASE)
# a leftover named with where it is: on today's plan, or not
ON_RE = re.compile(rf"\b(?:on|onto) {_PLAN}\b(?!\s+for)|\btoday at \d|\bon for today\b|\bplanned for today\b",
                   re.IGNORECASE)
OFF_RE = re.compile(rf"\bnot today\b|\b(?:isn't|is not|aren't|are not|not|none|nothing)\b[^.;\n]{{0,25}}\bon {_PLAN}"
                    rf"|\boff {_PLAN}|\blater (?:this|next) week\b|\b(?:tomorrow|this weekend|next week|next month|"
                    rf"someday|doesn't fit today|don't fit today)\b", re.IGNORECASE)
# a sentence about the leftovers at all (a count elsewhere in the morning is about something else)
ABOUT_RE = re.compile(r"\bkept\b|\bkeep\b|\bfrom (?:before|yesterday|earlier)\b|\bleft ?overs?\b|\bleft over\b|"
                      r"\bcarried\b|\bdrop", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"(?<=[.!?])\s+|\n+|;\s*")


def _num(word: str, total: int) -> int:
    n = _WORD_NUMS.get(word.strip().lower())
    if n is None:
        return int(word)
    return total if n == -1 else n


def _clauses(text: str) -> List[str]:
    return [c for c in _CLAUSE_RE.split(text or "") if c.strip()]


def _named(clause: str, f: Dict[str, Any]) -> List[Dict[str, Any]]:
    from thirties_core.item_facts import mentions
    names = [r["name"] for r in f["keep"]] + list(f["drop"])
    return [r for r in f["keep"] if mentions(clause, r["name"], names) or mentions(clause, r["title"], names)]


def _clause_wrong(clause: str, f: Dict[str, Any]) -> List[str]:
    from thirties_core.clock import format_clock, thirty_of
    out: List[str] = []
    kept, on, drops = len(f["keep"]), len(f["on_plan"]), len(f["drop"])
    if ABOUT_RE.search(clause):
        for m in ON_COUNT_RE.finditer(clause):
            said = _num(m.group(1), kept)
            if said != on:
                out.append(f"{on} of the kept items {'is' if on == 1 else 'are'} on today's plan, not {m.group(1)}")
        for m in KEPT_COUNT_RE.finditer(clause):
            word = m.group(1) or m.group(2)
            if word and _num(word, kept) != kept:
                out.append(f"{kept} {'was' if kept == 1 else 'were'} kept from before, not {word}")
        for m in DROP_COUNT_RE.finditer(clause):
            word = m.group(1) or m.group(2)
            if word and _num(word, drops) != drops:
                out.append(f"{drops} {'is' if drops == 1 else 'are'} proposed for dropping, not {word}")
    named = _named(clause, f)
    if not named:
        return out
    says_on, says_off = bool(ON_RE.search(clause)), bool(OFF_RE.search(clause))
    if says_on and not says_off:
        out += [f"{r['name']} isn't on today's plan ({r['where']})" for r in named if r["span"] is None]
    elif says_off and not says_on:
        out += [f"{r['name']} is on today's plan at {format_clock(thirty_of(r['span'].start_dt))}"
                for r in named if r["span"] is not None]
    return out


def wrong(assistant: Any, reply: str) -> List[str]:
    """What the reply says about this morning's leftovers that the plan doesn't bear out, each as a fact."""
    f = facts(assistant)
    if f is None or not reply:
        return []
    out: List[str] = []
    for clause in _clauses(reply):
        out += _clause_wrong(clause, f)
    return list(dict.fromkeys(out))


def applies(brain: Any, turn_start: int) -> bool:
    """The reply is the morning's, or the turn changed the leftovers (propose_leftovers)."""
    from thirties_core import morning_day
    from thirties_core.reply_checks import _called
    if getattr(brain.tools, "assistant", None) is None:
        return False
    return morning_day.is_morning_turn(brain, turn_start) or _called(brain, turn_start, "propose_leftovers")


def problems(brain: Any, turn_start: int, reply: str) -> List[str]:
    """For the rewrite: what's off, with the facts to say instead (empty when the reply agrees)."""
    try:
        if not applies(brain, turn_start):
            return []
        assistant = brain.tools.assistant
        bad = wrong(assistant, reply)
        if not bad:
            return []
        return ["what it says about what's left from before doesn't match the plan as it stands (" + "; ".join(bad)
                + "). Say it again in plain words from these facts " + for_model(facts(assistant))]
    except Exception as e:  # never costs the user the reply
        logger.warning("Couldn't check the leftovers' notice: %s", e)
        return []


def set_straight(brain: Any, turn_start: int, reply: str) -> str:
    """After its rewrite, a reply that still miscounts or misplaces the leftovers loses those sentences, and
    Python's notice (from the plan as it stands) stands in for them."""
    try:
        if not applies(brain, turn_start):
            return reply
        assistant = brain.tools.assistant
        f = facts(assistant)
        if f is None or not wrong(assistant, reply):
            return reply
        lines = []
        for line in (reply or "").split("\n"):
            if line.strip().rstrip(":").lower() in ("keep", "drop"):
                continue  # a heading whose lines are said again below
            kept = [s for s in re.split(r"(?<=[.!?])\s+", line)
                    if not (_clause_wrong(s, f) or (_named(s, f) and (ON_RE.search(s) or OFF_RE.search(s)))
                            or (ABOUT_RE.search(s) and (ON_COUNT_RE.search(s) or KEPT_COUNT_RE.search(s)
                                                        or DROP_COUNT_RE.search(s))))]
            if kept or not line.strip():
                lines.append(" ".join(kept))
        out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        said = notice(assistant)
        if not said:
            return out or reply
        from thirties_core.messages import join_sections
        return join_sections(out, said) if out else said
    except Exception as e:  # never costs the user the reply
        logger.warning("Couldn't set the leftovers' notice straight: %s", e)
        return reply


__all__ = ["facts", "notice", "restate", "summary", "plain", "for_model", "where_now", "tool_result", "wrong",
           "problems", "set_straight", "span_of"]
