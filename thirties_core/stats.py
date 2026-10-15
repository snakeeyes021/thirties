"""Statistics from what actually happened, as plain facts for brains and the profile.

Everything is computed here, deterministically; brains receive finished sentences
("Composing usually runs about 1.5x what you plan") and never do the math.
Only time data the user reported or confirmed in a check-in feeds duration
statistics; estimated or planned times are counted for completion but not for
how long things take.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from thirties_core.models import EventSpan, SpanCategory, SpanStatus

RELIABLE_TIME_SOURCES = ("reported", "checkin")
_STOPWORDS = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "with", "my", "some", "more"}


def activity_key(title: str) -> str:
    """Group similar titles: 'Composing (Natalie piece)' and 'composing' share 'composing'."""
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _STOPWORDS]
    return " ".join(words[:2]) if words else title.lower()


def part_of_day(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


@dataclass
class OutcomeStats:
    reviewed: int
    done: int
    partial: int
    skipped: int
    unreviewed: int

    @property
    def completion_rate(self) -> Optional[float]:
        if not self.reviewed:
            return None
        return (self.done + 0.5 * self.partial) / self.reviewed


def _trackable(span: EventSpan) -> bool:
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)


def outcome_stats(spans: List[EventSpan], now: datetime) -> OutcomeStats:
    past = [s for s in spans if _trackable(s) and s.end_dt.timestamp() <= now.timestamp()]
    by = {status: sum(1 for s in past if s.status == status) for status in SpanStatus}
    reviewed = by[SpanStatus.DONE] + by[SpanStatus.PARTIAL] + by[SpanStatus.SKIPPED]
    return OutcomeStats(reviewed, by[SpanStatus.DONE], by[SpanStatus.PARTIAL], by[SpanStatus.SKIPPED],
                        by[SpanStatus.PLANNED])


def duration_ratios(spans: List[EventSpan]) -> Dict[str, List[float]]:
    """actual/planned duration per activity key, from reliable times only."""
    ratios: Dict[str, List[float]] = {}
    for s in spans:
        if not (_trackable(s) and s.status in (SpanStatus.DONE, SpanStatus.PARTIAL)):
            continue
        if s.time_source not in RELIABLE_TIME_SOURCES or not (s.actual_start and s.actual_end):
            continue
        planned = s.end_dt.timestamp() - s.start_dt.timestamp()
        actual = s.actual_end.timestamp() - s.actual_start.timestamp()
        if planned > 0 and actual > 0:
            ratios.setdefault(activity_key(s.title), []).append(actual / planned)
    return ratios


def completion_by(spans: List[EventSpan], now: datetime, bucket) -> Dict[str, OutcomeStats]:
    groups: Dict[str, List[EventSpan]] = {}
    for s in spans:
        if _trackable(s):
            groups.setdefault(bucket(s), []).append(s)
    return {k: outcome_stats(v, now) for k, v in groups.items()}


def energy_at(entries: List[Dict], t: datetime, window: timedelta = timedelta(hours=3)) -> Optional[int]:
    best = None
    for e in entries:
        gap = abs(e["at"].timestamp() - t.timestamp())
        if gap <= window.total_seconds() and (best is None or gap < best[0]):
            best = (gap, e["level"])
    return best[1] if best else None


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def fact_lines(spans: List[EventSpan], now: datetime, energy_entries: Optional[List[Dict]] = None,
               deferred: Optional[List[tuple]] = None, min_samples: int = 3, max_facts: int = 10) -> List[str]:
    """Readable statistics, strongest evidence first. Silent where data is thin."""
    facts: List[str] = []
    overall = outcome_stats(spans, now)
    if overall.reviewed >= min_samples:
        facts.append(f"Of {overall.reviewed} planned activities with a known outcome, {overall.done} were done, "
                     f"{overall.partial} partly done and {overall.skipped} skipped "
                     f"({_pct(overall.completion_rate)} completion).")
    if overall.unreviewed:
        facts.append(f"{overall.unreviewed} past planned activities have no recorded outcome yet.")

    for key, values in sorted(duration_ratios(spans).items(), key=lambda kv: -len(kv[1])):
        if len(values) < min_samples:
            continue
        median = statistics.median(values)
        if median >= 1.15 or median <= 0.85:
            facts.append(f"'{key}' usually takes about {median:.1f}x the planned time ({len(values)} times measured).")
        else:
            facts.append(f"'{key}' usually takes about as long as planned ({len(values)} times measured).")

    by_part = completion_by(spans, now, lambda s: part_of_day(s.start_dt))
    rated = {k: v for k, v in by_part.items() if v.reviewed >= min_samples}
    if len(rated) >= 2:
        best = max(rated.items(), key=lambda kv: kv[1].completion_rate)
        worst = min(rated.items(), key=lambda kv: kv[1].completion_rate)
        if best[1].completion_rate - worst[1].completion_rate >= 0.2:
            facts.append(f"Plans in the {best[0]} get done {_pct(best[1].completion_rate)} of the time, "
                         f"versus {_pct(worst[1].completion_rate)} in the {worst[0]}.")

    if energy_entries:
        def energy_bucket(s: EventSpan) -> str:
            level = energy_at(energy_entries, s.start_dt)
            return "unknown" if level is None else ("low energy" if level <= 2 else "good energy")
        by_energy = {k: v for k, v in completion_by(spans, now, energy_bucket).items()
                     if k != "unknown" and v.reviewed >= min_samples}
        if len(by_energy) == 2:
            facts.append(f"Completion is {_pct(by_energy['good energy'].completion_rate)} with good energy "
                         f"and {_pct(by_energy['low energy'].completion_rate)} with low energy.")

    for title, count in (deferred or [])[:3]:
        facts.append(f"'{title}' has been put off {count} times.")
    return facts[:max_facts]


def unreviewed_spans(spans: List[EventSpan], now: datetime, since: datetime) -> List[EventSpan]:
    """Planned activities that have ended since ``since`` with no outcome recorded."""
    return sorted(
        (s for s in spans if _trackable(s) and s.status == SpanStatus.PLANNED
         and since.timestamp() <= s.end_dt.timestamp() <= now.timestamp()),
        key=lambda s: s.start_dt,
    )
