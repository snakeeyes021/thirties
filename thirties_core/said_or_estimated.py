"""The times of an outcome: the ones the model passes from the user's words stand; Python only checks and says.

A time the user gave for something they finished counts however they gave it: a clock time, or a time worked out
from another item ("twenty minutes after the kiln check"). The model reads the words and passes the time; Python
never drops it for not being a clock time the user typed.

Python checks only that the times are possible (not in the future: ``assistant_tools._not_in_future``; the start
before the end: ``start_before_end``) and estimates only a time the model passes none for. The tool's answer says
which of the times it recorded were the user's and which were estimated (``which_times``), so the reply can tell
the two apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from thirties_core.clock import format_clock


def start_before_end(start: Optional[datetime], end: Optional[datetime]) -> None:
    """A start that isn't before the end is refused, with the times in the answer."""
    if start is not None and end is not None and end <= start:
        from thirties_core.assistant_tools import ToolError
        raise ToolError(f"The start ({format_clock(start)}) isn't before the end ({format_clock(end)}). Give a start "
                        "that is earlier than the end, or only the one the user said.")


def which_times(start: Optional[datetime], end: Optional[datetime], said_start: bool, said_end: bool,
                guessed: bool = False) -> str:
    """The sentence after an outcome's times saying which were the user's and which Thirties estimated."""
    if start is None or end is None:
        return ""
    if not (said_start or said_end):
        return " Neither time came from the user; both are estimated."
    mine = "the user's rough figure" if guessed else "the user's"

    def one(name: str, dt: datetime, said: bool) -> str:
        return f"{name} {format_clock(dt)} is {mine if said else 'estimated'}"
    return f" {one('Start', start, said_start)}; {one('end', end, said_end)}."


__all__ = ["start_before_end", "which_times"]
