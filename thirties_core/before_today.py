"""Before today: what's left from earlier days, and what Thirties did with it (11-06, Matt's design).

The morning's leftover proposal takes effect by itself, with no yes needed (carry.py, wake.py):
- a Keep for today is put on today's schedule when the day is built;
- other Keeps wait at their horizon (this week, before Friday, next month, someday);
- a Drop stays proposed for that day, and is applied by itself once the day is over if the user didn't touch it.
It goes to the Archive, never deleted, and can be undropped.

The app's "Before today" tab and the chat act on the same records (the open pool, carry.OpenPool) through one
action, ``leftover_action``: accept, done, drop, keep, undrop.

The only time the morning asks anything is overflow: a Keep for today that doesn't fit today's free time.

Later, a week plan can feed ``place_today`` (what's meant for today) the same way the leftovers do now.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.carry import DROPPED, SOMEDAY, OpenPool, resolve_when

ACTIONS = ("accept", "done", "drop", "keep", "undrop")


class BeforeToday:

    def __init__(self, assistant) -> None:
        self.a = assistant

    @property
    def db(self):
        return self.a._db

    def _today(self) -> date:
        return self.a._today()

    def _week_start(self) -> str:
        sched = self.a.pt.scheduler
        return getattr(sched.config.general, "week_start", "mon") if sched is not None else "mon"

    # --- today's share: on the schedule when the day is built ---

    def _on_plan(self, rec: Dict[str, Any]):
        from thirties_core.models import SpanStatus
        title, task_id = rec["title"].strip().lower(), rec.get("task_id") or ""
        return next((s for s in self.a.pt.day_plan.spans if s.status != SpanStatus.SKIPPED and not s.is_envelope
                     and (s.title.strip().lower() == title or (task_id and s.task_id == task_id))), None)

    def place_today(self, items: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Each Keep meant for today goes on today's schedule, in the first free time it fits from now (the
        user can move it). Returns (titles placed, items that don't fit: the morning's one question)."""
        from thirties_core.clock import format_clock, thirty_ceil
        from thirties_core.day_proposal import usual_minutes
        from thirties_core.plan_tools import NOT_DONE
        pt, day = self.a.pt, self._today()
        placed, overflow = [], []
        pool = OpenPool(self.db)
        for item in items:
            if not item.get("end") or item["end"] > day.isoformat() or item.get("start", "") > day.isoformat():
                continue  # not today's
            span = self._on_plan(item)
            if span is not None:
                pool.set(item["title"], slot=span.start_dt.isoformat())
                continue
            task = next((t for t in self.a.s.tasks if item.get("task_id") and t.id == item["task_id"]), None)
            minutes = usual_minutes(self.a, task, item["title"])
            need = timedelta(minutes=minutes)
            now = pt._now()
            spot = None
            for a, b in pt.open_windows(after=now):
                a = max(a, thirty_ceil(now))
                if b - a >= need:
                    spot = a
                    break
            if spot is None:
                overflow.append({**item, "minutes": minutes})
                continue
            args = {"title": item["title"], "start": format_clock(spot), "duration": f"{minutes} minutes"}
            if task is not None:
                args.update(task_id=task.id, category="task")
            out = pt.execute("add_event", args)
            if out.startswith(NOT_DONE):
                overflow.append({**item, "minutes": minutes})
                continue
            pool.set(item["title"], slot=spot.isoformat())
            placed.append(item["title"])
        return placed, overflow

    def take_off_today(self, rec: Dict[str, Any]) -> None:
        """A leftover moved off today (or dropped, or done elsewhere): its slot on today's plan goes."""
        span = self._on_plan(rec)
        if span is not None and rec.get("slot"):
            self.a.pt.execute("remove_event", {"event": span.id})
        OpenPool(self.db).set(rec["title"], slot="")

    # --- what the tab shows ---

    def rows(self) -> Dict[str, Any]:
        """{"keep": [...], "drop": [...], "archive": [(day, [...]), ...]}; each row: title, when (a time today or
        a horizon), at (a datetime if it's on today's schedule), why."""
        day = self._today()
        pool = OpenPool(self.db)
        pool.apply_drops(day)
        try:
            self.reconcile()
        except Exception:
            pass
        keep, drop, archive = [], [], {}
        for rec in pool.all().values():
            if rec.get("horizon") == "gone":
                continue
            row = {"title": rec["title"], "why": rec.get("why") or "", "when": rec.get("when") or "this week",
                   "at": None}
            if rec.get("end") and rec["end"] < day.isoformat():  # its day went by: say which, not "today"
                row["when"] = "from " + date.fromisoformat(rec["end"]).strftime("%a")  # copy-madlibs T3-040
            if rec.get("horizon") == DROPPED:  # every applied drop, the app's or the user's (11-06)
                archive.setdefault(rec.get("dropped_on") or "", []).append(row)
                continue
            if rec.get("settled"):
                continue  # acted on (accepted, kept, undropped, moved): it's where it goes, not in Keep any more
            if rec.get("since", "") < (day - timedelta(days=14)).isoformat() and not rec.get("pending_drop"):
                continue  # two weeks waiting: it's still on its list, just not shown here
            if rec.get("pending_drop"):
                drop.append(row)
                continue
            span = self._on_plan(rec)
            if span is not None:
                row["at"] = span.start_dt
            keep.append(row)
        keep.sort(key=lambda r: (r["at"] is None, r["at"].timestamp() if r["at"] else 0, r["title"].lower()))
        drop.sort(key=lambda r: r["title"].lower())
        return {"keep": keep, "drop": drop, "archive": sorted(archive.items(), reverse=True)}

    # --- what the user does (from the tab or the chat) ---

    def action(self, item: str, action: str, when: str = "") -> str:
        """accept (it stands as proposed), done, drop, keep (a proposed drop kept instead, with a proposed slot),
        undrop (back from the archive into Keep)."""
        from thirties_core.assistant_tools import ToolError
        action = str(action or "").strip().lower()
        if action not in ACTIONS:
            raise ToolError("action must be accept, done, drop, keep or undrop.")
        pool = OpenPool(self.db)
        recs = pool.all()
        low = str(item or "").strip().lower()
        rec = recs.get(low) or next((v for k, v in recs.items() if low and low in k), None)
        if rec is None:
            raise ToolError(f"'{item}' isn't in what's left from before.")
        title = rec["title"]
        if action == "accept":
            if rec.get("pending_drop"):
                return self._drop(rec, by="user")
            pool.set(title, placed_by="user", settled=True)  # leaves Keep (11-06: Accept left rows standing)
            return f"{title}: stays {rec.get('when') or 'as proposed'}."  # copy-madlibs T3-020
        if action == "drop":
            return self._drop(rec, by="user")
        if action == "done":  # the normal done path: ticked in Joplin, filed in the month's Done note
            task = next((t for t in self.a.s.tasks if rec.get("task_id") and t.id == rec["task_id"]), None)
            if task is not None and self._on_plan(rec) is None:
                out = f"Marked {title} done " + self.a._complete_backlog_task(task, for_good=True) + "."
            else:
                out = self.a.pt.execute("log_outcome", {"event": title, "status": "done"})
            pool.drop([title])
            return out
        # keep / undrop: back into Keep with a proposed slot (Python's first guess, never someday); an applied
        # drop goes back to its list in Joplin first, where it was, open again
        from thirties_core.carry import default_when
        note = self.a._undrop_in_joplin(rec) if rec.get("horizon") == DROPPED else ""
        if when:  # the user named where (chat: "keep X, next week")
            try:
                window = resolve_when(when, self._today(), self._week_start())
            except ValueError as e:
                raise ToolError(str(e))
        elif rec.get("when") and (rec.get("start") or rec.get("when") == "someday"):
            # the horizon the row showed: what Keep was going to do is what it does (11-06: "full visibility into
            # what would and will happen")
            window = {k: rec.get(k, "") for k in ("when", "start", "end")}
            if window["end"] and window["end"] < self._today().isoformat():
                window = resolve_when("today", self._today(), self._week_start())  # its day went by: today
        else:
            h = rec.get("horizon") if rec.get("horizon") not in (DROPPED, SOMEDAY, "", None) else "week"
            window = default_when(h if h in ("today", "deadline") else "week", rec.get("due", ""), self._today(),
                                  self._week_start())
        pool.set(title, horizon=SOMEDAY if window["when"] == "someday" else (
                     "week" if window["end"] != self._today().isoformat() else "today"),
                 pending_drop=False, dropped_on="", dropped_by="", placed_by="user", settled=True, **window)
        rec = pool.all()[title.strip().lower()]
        placed, overflow = self.place_today([rec])
        if placed:
            return f"{title}: back, on today's plan." + (" " + note if note else "")  # T3-021
        return f"{title}: back, {window['when']}." + (" " + note if note else "")  # T3-022

    def reconcile(self) -> None:
        """Joplin is the truth (11-06), read from the notes themselves: an item ticked there is done (out of the
        pool); one moved or reworded into another note keeps its record (found by its wording); one under a
        someday section is someday; one gone from every note is gone here too. Only while Joplin is up and its
        notes are read (an empty read changes nothing)."""
        from thirties_core.carry import SOMEDAY
        from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX
        if not self.a._joplin_up():
            return
        notes = self.a._joplin_notes()
        if not notes:
            return
        open_items, ticked = set(), set()
        for n in notes:
            for line in (n.get("body") or "").splitlines():
                m = CHECKBOX_REGEX.match(line)
                if m:
                    open_items.add(m.group(2).strip().lower())
                    continue
                d = DONE_CHECKBOX_REGEX.match(line)
                if d:
                    ticked.add(d.group(2).strip().lower())
        pool = OpenPool(self.db)
        tasks = {t.title.strip().lower(): t for t in self.a.s.tasks if t.list_role != "today"}
        for rec in pool.all().values():
            if rec.get("horizon") in (DROPPED, "gone"):
                continue
            low = rec["title"].strip().lower()
            if low in open_items:
                task = tasks.get(low)
                if task is not None and task.id != rec.get("task_id"):
                    pool.set(rec["title"], task_id=task.id)  # moved in Joplin: the same item, its new place
                if task is not None and task.someday and rec.get("horizon") != SOMEDAY:
                    pool.set(rec["title"], horizon=SOMEDAY, when="someday", start="", end="")
            elif low in ticked:
                pool.drop([rec["title"]])  # ticked by hand: done
            else:
                pool.set(rec["title"], horizon="gone")  # taken out of every list by hand

    def _drop(self, rec: Dict[str, Any], by: str) -> str:
        self.take_off_today(rec)
        OpenPool(self.db).set(rec["title"], horizon=DROPPED, pending_drop=False, dropped_on=self._today().isoformat(),
                              dropped_by=by)
        self.a._drop_leftovers([{"title": rec["title"]}])  # off the Today note; its own list keeps it
        return f"Dropped {rec['title']} (it stays on its own list)."  # T3-011
