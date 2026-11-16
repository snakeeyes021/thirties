"""Before today: what's left from earlier days, and what Thirties did with it (11-06, the user's design).

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

import re

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.carry import DROPPED, SOMEDAY, OpenPool, resolve_when

ACTIONS = ("accept", "done", "drop", "keep", "undrop", "archive")
# An archive takes the item off its list in Joplin: allowed in chat only when the user's words ask for it (11-11:
# "drop" means stop rolling it over; a real removal only on an explicit ask). Anything else is a plain drop.
from thirties_core.intents import EXPLICIT_REMOVAL  # noqa: E402  (one source: intents.py)


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
        from thirties_core.backoff import MissStreaks, stated_cant_wait
        streaks = MissStreaks(self.db)
        for item in items:
            if not item.get("end") or item["end"] > day.isoformat() or item.get("start", "") > day.isoformat():
                continue  # not today's
            task = next((t for t in self.a.s.tasks if item.get("task_id") and t.id == item["task_id"]), None)
            if streaks.held(day, item["title"], item.get("task_id") or "", due=item.get("due", "")
                            or (task.due if task is not None else ""),
                            urgent=stated_cant_wait(self.db, task, item["title"])):
                continue  # resting after missed days (backoff.py): Thirties doesn't put it on, from any list
            span = self._on_plan(item)
            if span is not None:
                pool.set(item["title"], slot=span.start_dt.isoformat())
                continue
            task = next((t for t in self.a.s.tasks if item.get("task_id") and t.id == item["task_id"]), None)
            minutes = usual_minutes(self.a, task, item["title"])
            need = timedelta(minutes=minutes)
            now = pt._now()
            spot = None
            from thirties_core.clock import first_fitting_thirty
            for a, b in pt.open_windows(after=now):
                a = max(a, first_fitting_thirty(now, minutes))  # the current thirty only if it fits in what's left
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
        from thirties_core.item_path import named  # how a row reads: a nested list item with its parents
        tasks = list(self.a.s.tasks)
        for rec in pool.all().values():
            if rec.get("horizon") == "gone":
                continue
            row = {"title": rec["title"], "label": named(tasks, rec["title"], rec.get("task_id")),
                   "why": rec.get("why") or "", "when": rec.get("when") or "this week", "at": None}
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
        from thirties_core.backoff import MissStreaks
        ask = [{"title": r["title"], "label": named(tasks, r["title"], r.get("task_id")),
                "why": f"missed {r['misses']} days running"}
               for r in MissStreaks(self.db).asked_open()] if self.db is not None else []
        return {"keep": keep, "drop": drop, "archive": sorted(archive.items(), reverse=True),
                "restore": [r["title"] for r in self.old_filed_drops()], "ask": ask}

    def still_want(self, item: str, choice: str) -> str:
        """The answer to "Still want this?" (backoff.py): keep for this week, park for later, or drop."""
        from thirties_core.assistant_tools import ToolError
        from thirties_core.backoff import CHOICES, MissStreaks
        from thirties_core.break_down import answer as break_answer  # "Break it into smaller pieces"
        said = break_answer(self.a, item, choice)
        if said is not None:
            return said
        choice = str(choice or "").strip().lower()
        choice = {"keep for this week": "keep", "park for later": "park", "later": "park"}.get(choice, choice)
        if choice not in CHOICES:
            raise ToolError("choice must be keep, park or drop.")
        rec = MissStreaks(self.db).answer(item, choice)
        if rec is None:
            raise ToolError(f"'{item}' isn't one Thirties asked about.")
        title = rec["title"]
        pool = OpenPool(self.db)
        left = pool.all().get(title.strip().lower())
        if choice == "keep":
            if left is not None and left.get("horizon") not in (DROPPED, "gone"):
                pool.place(title, "week", resolve_when("this week", self._today(), self._week_start()))
            return f"{title}: kept for this week."
        if choice == "drop":
            if left is not None and left.get("horizon") not in (DROPPED, "gone"):
                return self._drop(left, by="user")
            pool.add([{"title": title, "task_id": rec.get("task_id") or ""}], self._today())
            return self._drop(pool.all()[title.strip().lower()], by="user")
        task = next((t for t in self.a.s.tasks if (rec.get("task_id") and t.id == rec["task_id"])
                     or t.title.strip().lower() == title.strip().lower()), None)
        if task is not None and self.a.pt.scheduler is not None:
            from thirties_core.horizons import horizon_handlers
            horizon_handlers(self.a)["set_horizon"](horizon="month", task=task.id)
        if left is not None:
            self.take_off_today(left)
            pool.drop([title])  # it waits on its list at its month-level horizon, not in the daily pool
        return f"{title}: parked for later (this month's plan, not the days)."

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
        if action == "archive":
            said = getattr(self.a.pt, "user_message", None)
            if said is not None and not EXPLICIT_REMOVAL.search(str(said)):
                # the user didn't ask for a removal: the non-destructive reading (decided 11-11)
                return self._drop(rec, by="user") + " (It's still on its list: say \"archive it\" to take it off.)"
            out = self._drop(rec, by="user")
            filed = self.a._archive_in_joplin([{"title": title}])
            return f"Archived {title}: ticked and filed in the Dropped note." if filed else out
        if action == "drop":
            return self._drop(rec, by="user")
        if action == "done":  # the normal done path: ticked in Joplin, filed in the month's Done note
            task = next((t for t in self.a.s.tasks if rec.get("task_id") and t.id == rec["task_id"]), None)
            if task is not None and self._on_plan(rec) is None:
                from thirties_core.done_reference import check as pointed_to  # only what the user's words name
                held, _ = pointed_to(self.a.pt, title, [task.id])
                if held:
                    raise ToolError(held)
                out =f"Marked {title} done " + self.a._complete_backlog_task(task, for_good=True) + "."
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
            # the horizon the row showed: what Keep was going to do is what it does (11-06: the row shows
            # exactly what will happen)
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
        from thirties_core.list_reading import live
        tasks = {t.title.strip().lower(): t for t in self.a.s.tasks if t.list_role != "today"}
        for rec in pool.all().values():
            if rec.get("horizon") in (DROPPED, "gone"):
                continue
            low = rec["title"].strip().lower()
            if low in open_items:
                task = tasks.get(low)
                if task is not None and task.id != rec.get("task_id"):
                    pool.set(rec["title"], task_id=task.id)  # moved in Joplin: the same item, its new place
                if task is not None and (task.someday or not live(task)) and rec.get("horizon") != SOMEDAY:
                    pool.set(rec["title"], horizon=SOMEDAY, when="someday", start="", end="")
            elif low in ticked:
                pool.drop([rec["title"]])  # ticked by hand: done
            else:
                pool.set(rec["title"], horizon="gone")  # taken out of every list by hand

    # --- the one-time offer for drops filed under the old meaning (before 11-11) ---

    OFFER_KEY = "old_drops_offer"

    def old_filed_drops(self) -> List[Dict[str, Any]]:
        """Items filed in a Dropped note (taken off their lists), while the one-time offer is open and each one
        hasn't been answered yet."""
        if self.db is None or self.db.get_sync_meta(self.OFFER_KEY):
            return []
        return [r for r in OpenPool(self.db).all().values()
                if r.get("horizon") == DROPPED and r.get("filed_key") and not r.get("offer_left")]

    def restore_old_drops(self, put_back: Any = True, item: str = "") -> str:
        """The one-time offer: put items filed in Dropped notes back on their own lists, open, where they were
        (they still don't come back by themselves), or leave them off. One ``item`` at a time, or all of them; once
        none is left to answer, or all are left off, it isn't offered again."""
        from thirties_core.assistant_tools import ToolError
        said = str(put_back).strip().lower()
        if said in ("true", "1", "yes", "put back", "put it back", "put them back", "back"):
            put_back = True
        elif said in ("false", "0", "no") or said.startswith("leave"):
            put_back = False
        else:
            raise ToolError("put_back must be true (put back) or false (leave off).")
        if self.db is None:
            raise ToolError("There's nothing taken off a list to put back here.")
        items = self.old_filed_drops()
        if item:
            low = str(item).strip().lower()
            items = [r for r in items if r["title"].strip().lower() == low] or \
                [r for r in items if low and low in r["title"].strip().lower()]
            if not items:
                raise ToolError(f"'{item}' isn't one of the items taken off their lists earlier.")
            if len(items) > 1:  # never a guess: the user says which
                raise ToolError(f"'{item}' matches several: " + "; ".join(r["title"] for r in items) + ". Which one?")
        if put_back and self.a._writer() is None:
            raise ToolError("Joplin isn't connected, so nothing could be put back on its list. Try again once it is.")
        back, failed = [], []
        for rec in items:
            if put_back:
                err = self.a._undrop_in_joplin(rec)
                (failed if err else back).append((rec["title"], err))
            else:
                OpenPool(self.db).set(rec["title"], offer_left=True)
        if not self.old_filed_drops():  # every item answered: never offered again (a failed one stays offered)
            self.db.set_sync_meta(self.OFFER_KEY, "answered")
        if not put_back:
            return f"Left {items[0]['title']} off its list." if item else "Left them all off their lists."
        out = []
        if back:
            out.append(f"Put {back[0][0]} back on its list." if len(back) == 1 and item else
                       "Put back on their lists: " + ", ".join(t for t, _ in back) + ".")
        if failed:
            out.append("Couldn't put back: " + "; ".join(f"{t} {e}" for t, e in failed) + ". It's still offered.")
        return " ".join(out)

    def _drop(self, rec: Dict[str, Any], by: str) -> str:
        self.take_off_today(rec)
        OpenPool(self.db).set(rec["title"], horizon=DROPPED, pending_drop=False, dropped_on=self._today().isoformat(),
                              dropped_by=by)
        self.a._drop_leftovers([{"title": rec["title"]}])  # off the Today note; its own list keeps it, untouched
        return f"Dropped {rec['title']}: it won't come back by itself, and it stays on its own list."
