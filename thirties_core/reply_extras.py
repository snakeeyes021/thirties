"""What Python adds to a reply, and the person's say over it.

Three things are Python's, never the model's: what's next after a change (whats_next.py), its "Not marked yet"
section, and the line saying which block the person is in ("You're in the 10:00 block: …", messages.status_line).
Each is a shared preference that Preferences shows too, so the app and chat agree. Asked in chat to stop one, the
assistant sets it with set_reply_extras: from now on, or only for today if the person says so. Asking for the
schedule or what's next still shows it.

The status line also goes quiet on its own: it's shown again only once the block has changed since it last was.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from thirties_core.clock import thirty_of

PARTS = {  # the part, and its shared setting
    "whats_next": "assistant.after_change",
    "not_marked": "assistant.after_change_unmarked",
    "status_line": "assistant.status_line",
}
NAMES = {"whats_next": "what's next after a change", "not_marked": "the Not marked yet list after a change",
         "status_line": "the line saying which block you're in"}
TODAY_KEY = "reply_extras_today"  # this day's own choices: {"day": "<iso>", "values": {<part>: <value>}}

REPLY_EXTRAS_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "set_reply_extras",
    "description": "Stop, trim or bring back what Python adds to replies, only when the user's message asks for that "
                   "outright: 'what's next' after a change (its Now and Next lines), the 'Not marked yet' list in it, "
                   "or the 'You're in the … block' line. For today unless they say it's for good ('from now on'). The "
                   "one way to honour 'stop sending the list' or 'stop saying block'; never say it's stopped without "
                   "this call, and never call it for a complaint about the schedule itself.",
    "parameters": {"part": "'whats_next', 'not_marked', 'status_line', or 'all'.",
                   "show": "'off' or 'on'; for whats_next also 'few' (only the next few things, when they ask for "
                           "less) or 'rest' (the rest of today, every row: the usual).",
                   "for_today": "true when the user says it's only for today."},
    "required": ["part", "show"],
}]


# The user's own message has to name one of the lines and ask for it to stop (or come back): a complaint about the
# schedule ("make the list right") or a wish not to have something explained is neither.
_ABOUT = re.compile(
    r"what'?s next|\bnext:?\s*(?:section|list|lines?|part|bit|stuff)|[\"'“‘]next:?[\"'”’]|not marked|unmarked|"
    r"(?:which|what) block|block (?:line|talk|thing|stuff|bit)|saying (?:the )?block|you'?re in the|status line|"
    r"(?:re)?send(?:ing)? (?:me )?the list|the list (?:at|on) the (?:end|bottom)|"
    r"(?:extra|added) (?:lines|stuff|bits)|lines? (?:at|on) the (?:end|bottom)|"
    r"(?:half[- ]hour|block)\b[^.?!\n]{0,24}\bat the top|"
    r"\b(?:stop|no|drop|skip|quit|lose|cut)\s+(?:the |those |these |all the )?(?:lists?|lines)\b", re.IGNORECASE)
_STOP = re.compile(r"\b(?:stop|don'?t|dont|no more|no need|quit|leave (?:it|them|that) out|without|turn (?:it |them )?off|"
                   r"hide|drop|skip|remove|delete|cut|lose|get rid|no (?:the )?(?:lists?|lines))\b", re.IGNORECASE)
_BACK = re.compile(r"\b(?:again|back|turn (?:it |them )?on|start (?:showing|sending)|i want (?:the|it|them))\b",
                   re.IGNORECASE)
# Asking for less or more of what's next: "just the next few things after a change", "the whole rest of the day
# under each reply"; the message names the list (or what comes after a reply) and how much of it
_RESIZE_ABOUT = re.compile(r"what'?s next|next few|\bthe list\b|(?:after|under|at the (?:end|bottom) of) "
                           r"(?:each|every|a|the|your|my) (?:change|reply|replies|message|answer)", re.IGNORECASE)
_RESIZE = re.compile(r"\b(?:just|only|shorter|short|fewer|less|trim|next (?:few|couple|two|three)|whole|full|"
                     r"entire|all of|every(?:thing| row)|rest of (?:the |to|my )?day|longer)\b", re.IGNORECASE)
_LASTING = re.compile(r"\b(?:from now on|always|for good|permanently|anymore|any more|ever again|every (?:day|time)|"
                      r"going forward|in future|never)\b", re.IGNORECASE)


class NotAsked(ValueError):
    """The user didn't ask for this change in their message."""


def asked(said: Optional[str], show: str, for_today: Any) -> bool:
    """Whether the user's message asks for this change; raises NotAsked if it doesn't. Returns True when the change is
    for today only: it lasts only when the message says so, whatever the call asked for."""
    said = said or ""
    show = str(show or "").strip().lower()
    off = show in ("off", "false", "no", "0", "nothing", "stop", "hide")
    resize = show in ("few", "next", "rest")
    if not (_ABOUT.search(said) and (_STOP if off else _BACK).search(said)) \
            and not (resize and _RESIZE_ABOUT.search(said) and _RESIZE.search(said)):
        raise NotAsked("The user's message doesn't ask to stop or bring back what's added to replies (what's next, "
                       "Not marked yet, or the block line). Leave it as it is, and answer what they did ask.")
    only_today = str(for_today).strip().lower() in ("true", "1", "yes", "today")
    return only_today or not _LASTING.search(said)


def _today_values(db, today: date) -> Dict[str, Any]:
    raw = db.get_sync_meta(TODAY_KEY) if db is not None else None
    try:
        held = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return held.get("values", {}) if held.get("day") == today.isoformat() else {}


def effective(config, db, today: date) -> Dict[str, Any]:
    """What applies now: today's own choices over the shared preferences."""
    from thirties_core.whats_next import mode_of
    assistant = getattr(config, "assistant", None)
    values = {"whats_next": getattr(assistant, "after_change", "rest"),
              "not_marked": bool(getattr(assistant, "after_change_unmarked", True)),
              "status_line": bool(getattr(assistant, "status_line", True))}
    values.update(_today_values(db, today))
    values["whats_next"] = mode_of(values["whats_next"])
    return values


def _value(part: str, show: str, current: Any) -> Any:
    show = str(show or "").strip().lower()
    off = show in ("off", "false", "no", "0", "nothing", "stop", "hide")
    if part == "whats_next":
        if off:
            return "nothing"
        if show in ("few", "next"):
            return "few"
        return "rest" if show == "rest" else (current if current in ("few", "rest") else "rest")
    return not off


def set_extras(config, settings, db, today: date, part: str, show: str, for_today: Any = False) -> str:
    """Set a part (or all three) and say what now applies, in the user's terms."""
    part = str(part or "").strip().lower().replace(" ", "_").replace("'", "")
    parts = list(PARTS) if part == "all" else [part]
    if any(p not in PARTS for p in parts):
        raise ValueError("part is whats_next, not_marked, status_line or all")
    only_today = str(for_today).strip().lower() in ("true", "1", "yes", "today")
    now = effective(config, db, today)
    held = _today_values(db, today)
    for p in parts:
        value = _value(p, show, now[p] if now[p] != "nothing" else getattr(config.assistant, "after_change", "rest"))
        if only_today:
            held[p] = value
        else:
            setattr(config.assistant, PARTS[p].split(".", 1)[1], value)
            if settings is not None:
                settings.set(PARTS[p], value)
            held.pop(p, None)  # the lasting choice replaces any for today
    if db is not None:
        db.set_sync_meta(TODAY_KEY, json.dumps({"day": today.isoformat(), "values": held}))
    after = effective(config, db, today)
    shown = [NAMES[p] for p in parts if after[p] not in ("nothing", False)]
    hidden = [NAMES[p] for p in parts if after[p] in ("nothing", False)]
    when = "for the rest of today" if only_today else "from now on, on every device (Preferences shows it too)"
    out = []
    if hidden:
        out.append("Replies leave out " + " and ".join(hidden) + f" {when}. A request for the schedule or what's next "
                   "still shows it.")
    if shown:
        out.append("Replies show " + " and ".join(shown) + f" again {when}.")
    if "whats_next" in parts and after["whats_next"] in ("few", "rest"):
        out.append("What's next lists " + ("only the next few things" if after["whats_next"] == "few"
                                           else "the rest of today, every block") + ".")
    return " ".join(out)


def clear_today(db, part: str) -> None:
    """A choice made in Preferences replaces today's own choice for that part: the app and chat agree."""
    raw = db.get_sync_meta(TODAY_KEY) if db is not None else None
    try:
        held = json.loads(raw) if raw else {}
    except ValueError:
        held = {}
    if part not in held.get("values", {}):
        return
    held["values"].pop(part, None)
    db.set_sync_meta(TODAY_KEY, json.dumps(held))


def clear_all_today(db) -> None:
    if db is not None and db.get_sync_meta(TODAY_KEY):
        db.set_sync_meta(TODAY_KEY, json.dumps({"day": "", "values": {}}))


STATUS_PREFIX = "You're in the "


def status_shown_in(reply: str) -> bool:
    return any(line.strip().startswith(STATUS_PREFIX) for line in (reply or "").splitlines())


def without_status(reply: str) -> str:
    """The reply without the status line (and the blank line it leaves)."""
    lines = (reply or "").splitlines()
    kept: List[str] = []
    for line in lines:
        if line.strip().startswith(STATUS_PREFIX) and " block" in line:
            continue
        kept.append(line)
    text = "\n".join(kept)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def status_block_last_shown(messages: Iterable[Dict[str, Any]]) -> Optional[datetime]:
    """The block of the last reply that said which block it was ("at" stamped), if any."""
    for m in reversed(list(messages)):
        if m.get("role") == "assistant" and status_shown_in(str(m.get("content") or "")) and m.get("at"):
            try:
                return thirty_of(datetime.fromisoformat(m["at"]))
            except (TypeError, ValueError):
                return None
    return None


__all__ = ["PARTS", "REPLY_EXTRAS_TOOL_SPECS", "TODAY_KEY", "clear_all_today", "clear_today", "effective",
           "set_extras", "status_block_last_shown",
           "status_shown_in", "without_status"]
