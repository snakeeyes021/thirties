"""Generated scenarios for the simulated-user eval.

Each scenario combines a kind of user, a talking style, a day setup (what time it
is, work, what's already planned, the calendar) and two to four goals. Goals are
built from structured templates, so every goal carries its own deterministic
checks, tagged with its kind ("[move] has Shower ...") for per-kind results.
Times are allocated from the day's actual free time, so goals never collide with
each other or with the setup, and the situation the simulated user is told is
generated from the same setup the brain sees.

    ~/.local/share/thirties/venv/bin/python -m tests.sim_eval --brain both --generated 120 --workers 4
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from thirties_core.clock import format_clock
from tests.sim_eval import (SimScenario, at, cal, has_item, memory_mentions, new_items, no_busy_plan_lines, no_item,
                            order, outcome, replies, work_is)

THU, FRI, SAT, SUN = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 10), date(2026, 10, 11)
HALF = timedelta(minutes=30)
BEDTIME_CUTOFF = "22:30"  # leave the last half hour before the 11 PM sleep alone

WHO = [
    "A composer and software developer in their thirties",
    "A nurse running a busy household",
    "A grad student juggling research and teaching",
    "A freelance designer who works from home",
    "A parent of two young kids",
    "A retiree with a lot of hobbies",
]

STYLES = {
    "terse": "Types short, clipped messages.",
    "chatty": "Friendly and chatty; adds small talk and context.",
    "rambler": "Voice-to-text stream of consciousness: long run-on messages, self-corrections ('wait no, make that...'), "
               "occasional typos.",
    "lowercase": "Types in lowercase with little punctuation and abbreviations (tmrw, rn, w/).",
    "formal": "Polite and precise, in full sentences.",
    "fatigued": "Tired and decision-fatigued; answers in one to three words when possible and is happy to let the "
                "assistant choose.",
    "backtracker": "Changes their mind about one detail mid-conversation before settling (the goals are the final answer).",
    "chunks": "Counts time in chunks (the app's 30-minute 'thirties'), e.g. 'two chunks' for an hour, and expects the "
              "assistant to as well.",
    "vague": "Gives loose times when they can ('around 5', 'after dinner') but accepts reasonable placements.",
    "multitask": "Packs several requests into one message.",
}

SETUPS = {
    "workday_morning": ([THU, FRI], ["07:15", "07:40", "08:00"], "on"),
    "workday_late_afternoon": ([THU, FRI], ["16:20", "16:40", "17:05"], "on"),
    "workday_evening": ([THU, FRI], ["17:45", "18:30", "19:15"], "on"),
    "day_off": ([THU, FRI], ["08:30", "10:00", "12:30"], "cleared"),
    "weekend_morning": ([SAT, SUN], ["08:30", "09:45", "10:30"], "none"),
    "weekend_afternoon": ([SAT, SUN], ["12:15", "13:30", "14:45"], "none"),
    "weekend_evening": ([SAT, SUN], ["17:30", "18:45", "19:30"], "none"),
}

ACTIVITIES = [("a walk", "walk"), ("piano practice", "piano"), ("laundry", "laundry"), ("a grocery run", "grocer"),
              ("a call with Mom", "mom"), ("yoga", "yoga"), ("the gym", "gym|workout"), ("meal prep", "meal|prep"),
              ("paying bills", "bill"), ("cleaning the kitchen", "kitchen"), ("guitar practice", "guitar"),
              ("a bike ride", "bike"), ("journaling", "journal"), ("watering the garden", "garden|water"),
              ("a nap", "nap")]
PREFILL = [("Assistant upgrades", "upgrade"), ("House chores", "chore"), ("Shower", "shower"),
           ("Budget review", "budget"), ("Practice scales", "scales"), ("Write blog post", "blog"),
           ("Sort the mail", "mail")]
PAST = [("File quarterly taxes", "tax"), ("Email the recruiter", "recruiter"), ("Call the pharmacy", "pharmacy"),
        ("Fix the bike tire", "tire"), ("Draft the newsletter", "newsletter")]
AUDIO = [("Project Hail Mary", "hail mary"), ("Dungeon Crawler Carl", "carl"),
         ("an episode of 99% Invisible", "invisible"), ("The Expanse audiobook", "expanse")]
INVITES = [("Team dinner", "team dinner"), ("Board game night", "board game"), ("Birthday drinks for Priya", "priya")]
APPOINTMENTS = [("Dentist", "dentist"), ("Physical therapy", "therapy"), ("Vet appointment for the cat", "vet")]
LONG_GOALS = [("finish the garden plan by spring", "garden"), ("run a 10k in March", "10k"),
              ("learn conversational Spanish this year", "spanish"), ("finish the choral commission by December", "choral")]
FACTS = [("items in the 'Someday' list are just ideas, never to-dos to push", "someday"),
         ("nothing gets scheduled before 9 AM on weekends", "weekend"),
         ("'deep work' means at least two uninterrupted hours", "deep work")]
LIST_WITH_TIMES = r"(?m)^\s*(?:[-*•]|\d+\.)\s.*\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)"


@dataclass
class Item:
    title: str
    kw: str
    start: datetime
    end: datetime
    touched: bool = False

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Goal:
    kind: str
    text: str
    checks: list
    order: int = 0  # 0 = anywhere, 1 = near the end (fill), 2 = last (show list)


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def tag(kind: str, check):
    check.__doc__ = f"[{kind}] {check.__doc__}"
    return check


class Ctx:
    """The day being generated: what's planned and which time is still free."""

    def __init__(self, rng: random.Random, setup: str, style: str):
        days, nows, work = SETUPS[setup]
        self.rng, self.setup_name, self.style = rng, setup, style
        self.day = rng.choice(days)
        self.now = at(self.day, rng.choice(nows))
        self.work = work
        self.setup: List[Tuple[str, dict]] = [("remove_event", {"event": "Work"})] if work == "cleared" else []
        self.events = []
        self.prefill: List[Item] = []
        self.past: List[Item] = []
        self.notes: List[str] = []  # extra situation facts
        self.used: set = set()
        start = self.now.replace(minute=0 if self.now.minute < 30 else 30, second=0) + HALF
        self.free: List[Tuple[datetime, datetime]] = [(start, at(self.day, BEDTIME_CUTOFF))]
        if work == "on":
            self.block(at(self.day, "08:30"), at(self.day, "16:00"))

    # --- time bookkeeping ---

    def block(self, s: datetime, e: datetime) -> None:
        out = []
        for a, b in self.free:
            if e <= a or s >= b:
                out.append((a, b))
                continue
            if a < s:
                out.append((a, s))
            if e < b:
                out.append((e, b))
        self.free = out

    def release(self, s: datetime, e: datetime) -> None:
        floor = self.now.replace(minute=0 if self.now.minute < 30 else 30, second=0) + HALF
        s = max(s, floor)
        if e <= s:
            return
        spans = sorted(self.free + [(s, e)])
        merged = [spans[0]]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        self.free = merged

    def is_free(self, s: datetime, e: datetime) -> bool:
        return any(a <= s and e <= b for a, b in self.free)

    def slot(self, minutes: int, earliest: str = "", latest: str = "") -> Optional[Tuple[datetime, datetime]]:
        lo = at(self.day, earliest) if earliest else None
        hi = at(self.day, latest) if latest else None
        length = timedelta(minutes=minutes)
        starts = []
        for a, b in self.free:
            t = a
            while t + length <= b:
                if (lo is None or t >= lo) and (hi is None or t + length <= hi):
                    starts.append(t)
                t += HALF
        if not starts:
            return None
        s = self.rng.choice(starts)
        self.block(s, s + length)
        return s, s + length

    # --- words ---

    def when(self, dt: datetime) -> str:
        return format_clock(dt)

    def length(self, minutes: int) -> str:
        if self.style == "chunks":
            n = minutes // 30
            return f"{n} chunk{'s' if n != 1 else ''}"
        return {30: "30 minutes", 60: "an hour", 90: "an hour and a half", 120: "two hours"}.get(minutes, f"{minutes} minutes")

    def fresh(self, pool):
        options = [p for p in pool if p[0] not in self.used]
        if not options:
            return None
        choice = self.rng.choice(options)
        self.used.add(choice[0])
        return choice

    def window(self, s: datetime, e: datetime, tol: int = 30) -> Tuple[str, str]:
        lo, hi = s - timedelta(minutes=tol), e + timedelta(minutes=tol)
        lo = max(lo, datetime.combine(self.day, datetime.min.time(), tzinfo=s.tzinfo))
        hi = min(hi, at(self.day, "23:59"))
        return hhmm(lo), hhmm(hi)

    # --- setup pieces, created on demand ---

    def ensure_prefill(self) -> List[Item]:
        if self.prefill:
            return self.prefill
        durations = [self.rng.choice([30, 60]) for _ in range(self.rng.choice([2, 3]))]
        total = sum(durations)
        found = self.slot(total + 60)  # an hour of room after it, for shifts and inserts
        if not found:
            return []
        s, _ = found
        self.release(s + timedelta(minutes=total), s + timedelta(minutes=total + 60))
        for minutes in durations:
            title, kw = self.fresh(PREFILL)
            item = Item(title, kw, s, s + timedelta(minutes=minutes))
            self.prefill.append(item)
            self.setup.append(("add_event", {"title": title, "start": format_clock(s), "end": format_clock(item.end)}))
            s = item.end
        return self.prefill

    def add_past(self) -> Optional[Item]:
        if self.now.hour < 11:
            return None
        latest_end = self.now.replace(minute=0, second=0) - HALF
        minutes = self.rng.choice([30, 60])
        options = []
        t = at(self.day, "09:00")
        while t + timedelta(minutes=minutes) <= latest_end:
            if not any(p.start < t + timedelta(minutes=minutes) and t < p.end for p in self.past):
                options.append(t)
            t += HALF
        picked = self.fresh(PAST)
        if not options or not picked:
            return None
        s = self.rng.choice(options)
        item = Item(picked[0], picked[1], s, s + timedelta(minutes=minutes))
        self.past.append(item)
        self.setup.append(("add_event", {"title": item.title, "start": format_clock(item.start),
                                         "end": format_clock(item.end)}))
        return item

    def situation(self) -> str:
        bits = [f"{self.day.strftime('%A')} {format_clock(self.now)}."]
        bits.append({"on": "Work is on the schedule 8:30 AM to 4 PM.",
                     "cleared": "A day off from work (already cleared from the schedule).",
                     "none": "No work today."}[self.work])
        if self.prefill:
            bits.append("Already planned: " + "; ".join(
                f"{p.title} {format_clock(p.start)} – {format_clock(p.end)}" for p in self.prefill) + ".")
        if self.past:
            bits.append("Earlier today the plan had: " + "; ".join(
                f"{p.title} {format_clock(p.start)} – {format_clock(p.end)}" for p in self.past) + ".")
        bits += self.notes
        if self.day.weekday() < 5 and self.now < at(self.day, "09:45"):
            bits.append("The team standup (9:30 – 9:45 AM) is on the calendar.")
        bits.append("Bedtime is 11 PM.")
        return " ".join(bits)


# --- goal templates: each returns a Goal, or None when it doesn't fit this day ---

def g_add_at(c: Ctx) -> Optional[Goal]:
    minutes = c.rng.choice([30, 60, 90])
    found, act = c.slot(minutes), c.fresh(ACTIVITIES)
    if not found or not act:
        return None
    s, e = found
    return Goal("add_at", f"Add {act[0]} at {c.when(s)} for {c.length(minutes)}.",
                [tag("add_at", has_item(act[1], c.window(s, e), minimum_minutes=minutes))])


def g_add_after(c: Ctx) -> Optional[Goal]:
    anchors = [p for p in c.ensure_prefill() if not p.touched]
    act = c.fresh(ACTIVITIES)
    minutes = c.rng.choice([30, 60])
    for a in c.rng.sample(anchors, len(anchors)):
        if act and c.is_free(a.end, a.end + timedelta(minutes=minutes)):
            c.block(a.end, a.end + timedelta(minutes=minutes))
            a.touched = True
            e = a.end + timedelta(minutes=minutes)
            return Goal("add_after", f"Right after {a.title}, add {act[0]} for {c.length(minutes)}.",
                        [tag("add_after", has_item(act[1], c.window(a.end, e, 15))), tag("add_after", order(a.kw, act[1]))])
    return None


def g_move(c: Ctx) -> Optional[Goal]:
    options = [p for p in c.ensure_prefill() if not p.touched]
    if not options:
        return None
    x = c.rng.choice(options)
    found = c.slot(x.minutes)
    if not found:
        return None
    x.touched = True
    c.release(x.start, x.end)
    s, e = found
    return Goal("move", f"Move {x.title} to {c.when(s)}.", [tag("move", has_item(x.kw, c.window(s, e, 15)))])


def g_shift(c: Ctx) -> Optional[Goal]:
    minutes = c.rng.choice([30, 60])
    for x in c.ensure_prefill():
        later = timedelta(minutes=minutes)
        if not x.touched and c.is_free(x.end, x.end + later) and x is c.prefill[-1]:
            x.touched = True
            c.block(x.end, x.end + later)
            return Goal("shift", f"Push {x.title} back {c.length(minutes)}.",
                        [tag("shift", has_item(x.kw, c.window(x.start + later, x.end + later, 15)))])
    return None


def g_remove(c: Ctx) -> Optional[Goal]:
    options = [p for p in c.ensure_prefill() if not p.touched]
    if not options:
        return None
    x = c.rng.choice(options)
    x.touched = True
    c.release(x.start, x.end)
    return Goal("remove", f"Take {x.title} off today's plan; it's not happening today.",
                [tag("remove", no_item(x.kw))])


def g_swap(c: Ctx) -> Optional[Goal]:
    items = c.ensure_prefill()
    if len(items) < 2 or any(p.touched for p in items):
        return None
    a, b = items[0], items[1]
    for p in items:
        p.touched = True
    return Goal("swap", f"Swap {a.title} and {b.title}, so {b.title} comes first.",
                [tag("swap", order(b.kw, a.kw))])


def g_insert_slide(c: Ctx) -> Optional[Goal]:
    items = c.ensure_prefill()
    act = c.fresh(ACTIVITIES)
    if not items or any(p.touched for p in items) or not act or not c.is_free(items[-1].end, items[-1].end + HALF):
        return None
    for p in items:
        p.touched = True
    c.block(items[-1].end, items[-1].end + HALF)
    x = items[0]
    return Goal("insert_slide", f"Add {act[0]} for {c.length(30)} at {c.when(x.start)}, with everything after it "
                                f"sliding later rather than overlapping.",
                [tag("insert_slide", has_item(act[1], c.window(x.start, x.start + HALF, 15))),
                 tag("insert_slide", order(act[1], x.kw))])


def g_outcome_done(c: Ctx) -> Optional[Goal]:
    p = c.add_past()
    if not p:
        return None
    extra = c.rng.choice(["", f" It actually ran until {c.when(p.end + HALF)}."])
    return Goal("outcome_done", f"Tell the assistant you finished {p.title}.{extra}",
                [tag("outcome_done", outcome(p.kw, "done"))])


def g_outcome_skip(c: Ctx) -> Optional[Goal]:
    p = c.add_past()
    if not p:
        return None
    tomorrow = c.day + timedelta(days=1)
    t = "08:00" if tomorrow.weekday() < 5 else "10:00"
    s = at(tomorrow, t)
    e = s + timedelta(minutes=p.minutes)
    return Goal("outcome_skip", f"Tell the assistant you didn't get to {p.title}, and ask to put it tomorrow at "
                                f"{format_clock(s)}.",
                [tag("outcome_skip", outcome(p.kw, "skipped")),
                 tag("outcome_skip", has_item(p.kw, (hhmm(s - HALF), hhmm(e + HALF)), day_offset=1))])


def g_work_late(c: Ctx) -> Optional[Goal]:
    if c.setup_name != "workday_late_afternoon":
        return None
    end = c.rng.choice([e for e in (at(c.day, "16:30"), at(c.day, "17:00")) if e <= c.now] or [at(c.day, "16:30")])
    return Goal("work_late", f"Tell the assistant work actually ran until {c.when(end)} (it's on the schedule until 4 PM).",
                [tag("work_late", work_is("8:30 AM", format_clock(end)))])


def g_work_start(c: Ctx) -> Optional[Goal]:
    if c.setup_name != "workday_morning" or "work" in c.used:
        return None
    c.used.add("work")
    start = at(c.day, c.rng.choice(["09:30", "10:00"]))
    c.release(at(c.day, "08:30"), start)
    c.block(at(c.day, "08:30"), start)  # the morning before work stays the user's own business
    return Goal("work_start", f"Tell the assistant you're starting work at {c.when(start)} today instead of 8:30.",
                [tag("work_start", work_is(format_clock(start), "4:00 PM"))])


def g_day_off(c: Ctx) -> Optional[Goal]:
    if c.setup_name != "workday_morning" or "work" in c.used:
        return None
    c.used.add("work")
    c.release(at(c.day, "08:30"), at(c.day, "16:00"))
    return Goal("day_off", "You've decided to take the day off: get work off today's schedule.",
                [tag("day_off", work_is(None, None))])


def g_alongside(c: Ctx) -> Optional[Goal]:
    found = c.slot(60, earliest="17:00", latest="21:00")
    audio = c.fresh(AUDIO)
    if not found or not audio:
        return None
    s, e = found
    return Goal("alongside", f"Make dinner and eat at {c.when(s)} for an hour, listening to {audio[0]} at the same time.",
                [tag("alongside", has_item("dinner|eat|cook", c.window(s, e))), tag("alongside", has_item(audio[1]))])


def g_invite(c: Ctx) -> Optional[Goal]:
    found = c.slot(150, earliest="17:30", latest="22:30")
    inv = c.fresh(INVITES)
    if not found or not inv:
        return None
    ready, s = found[0], found[0] + HALF
    e = s + timedelta(hours=2)
    c.events.append(cal(c.day, f"invite-{inv[1]}", inv[0], hhmm(s), hhmm(e), invite=True))
    c.notes.append(f"There's an unconfirmed calendar invite: {inv[0]} {format_clock(s)} – {format_clock(e)}.")
    return Goal("invite", f"Confirm you're going to {inv[0]}, and plan 30 minutes to get ready right before it.",
                [tag("invite", has_item(inv[1])), tag("invite", has_item("ready|shower|change|dress", (hhmm(ready - HALF), hhmm(s))))])


def g_conflict(c: Ctx) -> Optional[Goal]:
    found = c.slot(180, earliest="11:00", latest="21:00")
    appt, act = c.fresh(APPOINTMENTS), c.fresh(ACTIVITIES)
    if not found or not appt or not act:
        return None
    s = found[0]
    a_s, a_e = s + timedelta(hours=1), s + timedelta(hours=2)
    c.release(s, s + HALF)  # room for a fix before it
    c.events.append(cal(c.day, f"appt-{appt[1]}", appt[0], hhmm(a_s), hhmm(a_e)))
    c.notes.append(f"On the calendar: {appt[0]} {format_clock(a_s)} – {format_clock(a_e)}.")
    req_s, req_e = a_s - HALF, a_e - HALF
    return Goal("conflict", f"Ask for {act[0]} from {c.when(req_s)} to {c.when(req_e)}. You're keeping the "
                            f"{appt[0].lower()}; accept a sensible fix.",
                [tag("conflict", has_item(act[1])), tag("conflict", has_item(appt[1], c.window(a_s, a_e, 0)))])


def g_tomorrow(c: Ctx) -> Optional[Goal]:
    act = c.fresh(ACTIVITIES)
    if not act:
        return None
    tomorrow = c.day + timedelta(days=1)
    t = c.rng.choice(["07:30", "17:00", "18:30"] if tomorrow.weekday() < 5 else ["10:00", "13:00", "15:30"])
    minutes = c.rng.choice([30, 60])
    s = at(tomorrow, t)
    e = s + timedelta(minutes=minutes)
    return Goal("tomorrow", f"Plan {act[0]} tomorrow at {format_clock(s)} for {c.length(minutes)}.",
                [tag("tomorrow", has_item(act[1], (hhmm(s - HALF), hhmm(e + HALF)), day_offset=1)),
                 tag("tomorrow", no_item(act[1]))])


def g_profile(c: Ctx) -> Optional[Goal]:
    goal = c.fresh(LONG_GOALS)
    if not goal:
        return None
    return Goal("profile", f"Mention, in passing, that you want to {goal[0]}.", [tag("profile", memory_mentions(goal[1]))])


def g_remember(c: Ctx) -> Optional[Goal]:
    fact = c.fresh(FACTS)
    if not fact:
        return None
    return Goal("remember", f"Explain that {fact[0]}, and make sure the assistant will remember it.",
                [tag("remember", memory_mentions(fact[1]))])


def g_free_time(c: Ctx) -> Optional[Goal]:
    if "free_time" in c.used:
        return None
    c.used.add("free_time")
    checks = [tag("free_time", replies(r"\b(chunks?|thirties)\b"))] if c.style == "chunks" else []
    return Goal("free_time", "Ask how much free time you have left before bed.", checks)


def g_fill(c: Ctx) -> Optional[Goal]:
    if "fill" in c.used:
        return None
    windows = [(a, b) for a, b in c.free if b - a >= timedelta(hours=2)]
    if not windows:
        return None
    c.used.add("fill")
    a, b = max(windows, key=lambda w: w[1] - w[0])
    b = min(b, a + timedelta(hours=4))
    c.block(a, b)
    part = ("your morning" if b <= at(c.day, "12:30") else "your afternoon" if a.hour >= 12 and b <= at(c.day, "18:30")
            else "your evening" if a.hour >= 17 else "the time")
    planned = tuple(p.title for p in c.prefill + c.past)
    return Goal("fill", f"Ask the assistant to fill {part} from {c.when(a)} until {c.when(b)} with things from "
                        f"your backlog (you don't want to pick them yourself). Accept its plan, or swap out one item.",
                [tag("fill", new_items((hhmm(a), hhmm(b)), 2, planned))], order=1)


def g_show_list(c: Ctx) -> Optional[Goal]:
    return Goal("show_list", f"At the end, ask to see {c.rng.choice(['the rest of today', 'your day'])} as a clean list.",
                [tag("show_list", no_busy_plan_lines()),
                 tag("show_list", replies(LIST_WITH_TIMES))], order=2)


TEMPLATES: Dict[str, Callable[[Ctx], Optional[Goal]]] = {
    "add_at": g_add_at, "add_after": g_add_after, "move": g_move, "shift": g_shift, "remove": g_remove,
    "swap": g_swap, "insert_slide": g_insert_slide, "outcome_done": g_outcome_done, "outcome_skip": g_outcome_skip,
    "work_late": g_work_late, "work_start": g_work_start, "day_off": g_day_off, "alongside": g_alongside,
    "invite": g_invite, "conflict": g_conflict, "tomorrow": g_tomorrow, "profile": g_profile,
    "remember": g_remember, "free_time": g_free_time, "fill": g_fill,
}


def generate(count: int, seed: int = 1) -> List[SimScenario]:
    """`count` scenarios, reproducible for a seed, spreading goal kinds evenly."""
    rng = random.Random(seed)
    uses: Dict[str, int] = {k: 0 for k in TEMPLATES}
    scenarios = []
    setups, styles = list(SETUPS), list(STYLES)
    for i in range(count):
        setup = setups[i % len(setups)] if i < len(setups) else rng.choice(setups)
        style = styles[(i * 3) % len(styles)] if rng.random() < 0.5 else rng.choice(styles)
        c = Ctx(rng, setup, style)
        want = rng.choice([2, 3, 3, 4])
        goals: List[Goal] = []
        for kind in sorted(TEMPLATES, key=lambda k: (uses[k], rng.random())):
            if len(goals) >= want:
                break
            g = TEMPLATES[kind](c)
            if g:
                goals.append(g)
                uses[kind] += 1
        if rng.random() < 0.3:
            goals.append(g_show_list(c))
        rng.shuffle(goals)
        goals.sort(key=lambda g: g.order)
        scenarios.append(SimScenario(
            id=f"gen{seed}_{i:03d}_{setup}_{style}",
            day=c.day,
            now=hhmm(c.now),
            persona=f"{rng.choice(WHO)}. {STYLES[style]}",
            situation=c.situation(),
            goals=[g.text for g in goals],
            checks=[chk for g in goals for chk in g.checks],
            extra_events=c.events,
            setup=c.setup,
            max_turns=min(14, 4 + 2 * len(goals)),
            tags=[g.kind for g in goals],
        ))
    return scenarios


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    for scn in generate(n, seed=int(sys.argv[2]) if len(sys.argv) > 2 else 1):
        print(f"\n{scn.id}  now {scn.now}  tags {scn.tags}\n  persona: {scn.persona}\n  situation: {scn.situation}")
        for g in scn.goals:
            print(f"  - {g}")
        print(f"  checks: {[c.__doc__ for c in scn.checks]}\n  setup: {scn.setup}")
