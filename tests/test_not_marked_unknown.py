"""An item stored as unknown is not marked yet, in the chat as on the Agenda.

- **One rule** (not_marked.py): an item belongs on the list when it's still planned once its block is over, or
  stored as unknown (its time passed with no outcome). The Agenda's Not marked yet section and every list the chat
  shows (a reply's what's next, the schedule, a notice, the prompt) come from it, in the same time order.
- **An unknown copy of an item done today is settled** and listed nowhere.
- **Marking it done or skipped, with or without a time,** works for an unknown item as for a planned one, said by name, pointed
  back to, or as a bare done after a list.
- **Listed, never asked about**: the evening catch-up and the prompt still leave unknown items out of questions.

Through the real tool path on the mock Joplin API (no real calls). Fixtures are invented: a clockmaker's bench.
"""

import unittest

from tests.fixtures import DAY, at, line_with, make_scheduler
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.messages import MISSED_HEADING, missed_items, now_section, render_schedule
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.not_marked import unmarked_spans
from thirties_core.plan_tools import NOT_DONE

ESCAPEMENT, BEZEL, PENDULUM, HAIRSPRING = ("Oil the escapement", "Reseat the bezel", "Hang the pendulum",
                                           "Set the hairspring")
ITEMS = ((ESCAPEMENT, "9:10 AM", "t_escapement"), (BEZEL, "9:40 AM", "t_bezel"),
         (PENDULUM, "10:10 AM", "t_pendulum"), (HAIRSPRING, "1:40 PM", "t_hairspring"))
NOW = "10:50"  # the first three are past their blocks; the hairspring is still ahead


class BenchDay(TodayTestCase):
    """Four bench jobs from one list, each on the plan; the bezel went by unreported and is stored as unknown."""

    def setUp(self):
        super().setUp()
        TITLES["n_bench"] = "Pendulum work"
        self.addCleanup(TITLES.pop, "n_bench", None)
        self.api.notes["n_bench"] = {"id": "n_bench", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_bench", "list", "Pendulum work")
        self.services.tasks[:] = [item(tid, title, "n_bench") for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        set_debug_time(at(NOW))
        self.unknown(BEZEL)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def unknown(self, title):
        s = self.span(title)
        s.status = SpanStatus.UNKNOWN
        self.t._persist(s)

    def say(self, message, title, shown, status="done", **times):
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", shown), None
        token = current_turn.set(f"[app] {message}")
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": status, **times}))
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def listed(self):
        return [s.title for s in missed_items(self.t.day_plan, at(NOW))]

    def fresh(self):
        self.doCleanups()
        self.setUp()


class TestListedLikeTheAgenda(BenchDay):

    def test_the_chat_lists_the_unknown_one_in_time_order(self):
        self.assertEqual(self.listed(), [ESCAPEMENT, BEZEL, PENDULUM])

    def test_the_chat_lists_what_the_shared_rule_lists(self):
        plan = self.t.day_plan
        shared = [s.title for s in unmarked_spans(plan.active_spans(), at("10:30"))]
        self.assertEqual(self.listed(), shared)

    def test_every_list_the_chat_shows_has_it(self):
        from thirties_core.whats_next import summary
        schedule = render_schedule(self.t.day_plan, at(NOW))
        self.assertIn(MISSED_HEADING, schedule)
        after = schedule.split(MISSED_HEADING)[1]
        self.assertLess(after.index(ESCAPEMENT), after.index(BEZEL))
        self.assertLess(after.index(BEZEL), after.index(PENDULUM))
        part = summary(self.t, at(NOW), "all")
        self.assertIn(MISSED_HEADING, part)
        self.assertIn(f"9:30 AM – {BEZEL}", part.split(MISSED_HEADING)[1])
        text = self.t.schedule_text()
        self.assertIn(BEZEL, text.split("Not marked yet")[1])
        self.assertNotIn(BEZEL, line_with(text, "Marked already") if "Marked already" in text else "")

    def test_the_prompt_lists_it_and_says_not_to_ask(self):
        text = now_section(self.t)
        line = next(x for x in text.splitlines() if x.startswith("Not marked yet"))
        self.assertIn(BEZEL, line.split(".")[0])
        self.assertIn(f"Not reported and gone by: {BEZEL}: never ask", line)
        self.assertNotIn(BEZEL, line.split("Not shown to the user yet:")[1].split(".")[0]
                         if "Not shown to the user yet:" in line else "")

    def test_an_unknown_copy_of_a_job_done_later_is_settled(self):
        set_debug_time(at("11:20"))
        out = self.t.execute("add_event", {"title": BEZEL, "start": "10:40 AM", "duration": "20 min",
                                           "task_id": "t_bezel", "again": True})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        later = max((s for s in self.t.day_plan.spans if s.title == BEZEL), key=lambda s: s.start_dt)
        later.status = SpanStatus.DONE
        self.t._persist(later)
        titles = [s.title for s in missed_items(self.t.day_plan, at("11:20"))]
        self.assertNotIn(BEZEL, titles)
        self.assertEqual(titles, [ESCAPEMENT, PENDULUM])


class TestMarkedLikeAPlannedOne(BenchDay):

    def test_done_by_name(self):
        out = self.say("bezel reseated", BEZEL, "")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(BEZEL).status, SpanStatus.DONE, out)
        self.assertIn("- [x] Reseat the bezel", self.body("n_bench"))
        self.assertNotIn(BEZEL, self.listed())

    def test_skipped_by_name(self):
        out = self.say("skip the bezel", BEZEL, "", status="skipped")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(BEZEL).status, SpanStatus.SKIPPED, out)
        self.assertNotIn(BEZEL, self.listed())

    def test_done_at_a_time_pointed_back_to(self):
        shown = f"{MISSED_HEADING}\n{BEZEL}"
        out = self.say("I did it at 9:52", BEZEL, shown, finished="9:52 AM")
        self.assertFalse(out.startswith(NOT_DONE), out)
        s = self.span(BEZEL)
        self.assertEqual(s.status, SpanStatus.DONE, out)
        self.assertEqual(s.actual_end, at("9:52"), out)

    def test_a_bare_done_after_the_list_can_pick_it(self):
        shown = f"{MISSED_HEADING}\n{ESCAPEMENT}\n{BEZEL}\n{PENDULUM}"
        out = self.say("done!", BEZEL, shown)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(BEZEL).status, SpanStatus.DONE, out)


class TestTheReplyChecksAgree(BenchDay):

    def test_a_kept_job_gone_unknown_is_still_on_todays_plan(self):
        from thirties_core import leftover_facts
        row = leftover_facts.span_of(self.t.day_plan, {"title": BEZEL, "task_id": "t_bezel"})
        self.assertIsNotNone(row)
        self.assertEqual(row.status, SpanStatus.UNKNOWN)
        keep = [{"title": BEZEL, "name": BEZEL, "span": row, "where": "on its list", "when": ""}]
        f = {"keep": keep, "on_plan": keep, "drop": [], "overflow": []}
        self.assertEqual(leftover_facts._clause_wrong(f"Kept one from before: {BEZEL}, on today's plan.", f), [])

    def test_a_done_after_the_list_names_it_or_the_reply_is_told_to(self):
        from thirties_core.reply_checks import picked_unnamed
        shown = f"{MISSED_HEADING}\n{ESCAPEMENT}\n{BEZEL}\n{PENDULUM}"
        self.t.picked_dones = set()
        self.say("done!", BEZEL, shown)
        brain = type("Brain", (), {"tools": self.t})()
        self.assertEqual(picked_unnamed(brain, "Marked it."), [BEZEL])
        self.assertEqual(picked_unnamed(brain, f"Marked {BEZEL} done."), [])


class TestNeverAskedAbout(BenchDay):

    def test_the_evening_catch_up_leaves_it_out(self):
        from thirties_core import catch_up
        self.assertEqual([s.title for s in catch_up.behind(self.t, missed_items(self.t.day_plan, at(NOW)))
                          if s.title == BEZEL], [])


from tests.test_gui import GTK, _rows  # noqa: E402  (a private headless session, or skipped)


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class TestTheAgendaAndTheChatAgree(unittest.TestCase):

    def setUp(self):
        self.sched = make_scheduler()
        for sid, title, start, end in (("esc", ESCAPEMENT, "9:10", "9:30"), ("bez", BEZEL, "9:40", "10:00"),
                                       ("pen", PENDULUM, "10:10", "10:25"), ("hair", HAIRSPRING, "13:40", "14:00")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        self.plan, _ = self.sched.build_day_plan(DAY)
        set_debug_time(at(NOW))
        self.addCleanup(set_debug_time, None)

    def span(self, sid):
        return next(s for s in self.plan.spans if s.id == sid)

    def behind(self, now):
        from thirties.views.agenda import AgendaPage
        page = AgendaPage(run_tool=lambda name, args: "ok")
        page.refresh(self.plan, now)
        return [r.get_title() for r in _rows(page.groups["behind"])] if "behind" in page.groups else []

    def test_the_same_items_in_the_same_order(self):
        self.span("bez").status = SpanStatus.UNKNOWN
        chat = [s.title for s in missed_items(self.plan, at(NOW))]
        self.assertEqual(chat, [ESCAPEMENT, BEZEL, PENDULUM])
        self.assertEqual(self.behind(at(NOW)), chat)

    def test_a_settled_copy_is_on_neither(self):
        self.span("bez").status = SpanStatus.UNKNOWN
        self.plan.spans.append(EventSpan("bez2", BEZEL, at("10:40"), at("10:45"), SpanCategory.ACTIVITY,
                                         status=SpanStatus.DONE))
        chat = [s.title for s in missed_items(self.plan, at(NOW))]
        self.assertEqual(chat, [ESCAPEMENT, PENDULUM])
        self.assertEqual(self.behind(at(NOW)), chat)


class TestTheRule(unittest.TestCase):

    def setUp(self):
        self.sched = make_scheduler()

    def spans(self):
        def job(sid, title, start, end, status=SpanStatus.PLANNED, task=None):
            return EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=status, task_id=task)
        return [job("a", ESCAPEMENT, "9:10", "9:30"), job("b", BEZEL, "9:40", "10:00", SpanStatus.UNKNOWN),
                job("c", PENDULUM, "10:10", "10:35"), job("d", HAIRSPRING, "13:40", "14:00", SpanStatus.UNKNOWN),
                job("e", "Bezel ring", "9:20", "9:40", SpanStatus.DONE)]

    def test_planned_past_its_block_or_unknown_whenever(self):
        self.assertEqual([s.id for s in unmarked_spans(self.spans(), at("10:30"))], ["a", "b", "d"])

    def test_another_day_lists_only_the_unknown_ones(self):
        self.assertEqual([s.id for s in unmarked_spans(self.spans(), None)], ["b", "d"])

    def test_an_unknown_copy_of_a_done_item_is_settled(self):
        spans = self.spans()
        spans.append(EventSpan("f", BEZEL, at("10:40"), at("10:55"), SpanCategory.ACTIVITY, status=SpanStatus.DONE))
        self.assertEqual([s.id for s in unmarked_spans(spans, at("11:00"))], ["a", "c", "d"])

    def test_the_same_item_by_its_list_line(self):
        spans = self.spans()
        spans[3].task_id = "t_hairspring"
        spans.append(EventSpan("g", "Hairspring, second try", at("14:10"), at("14:25"), SpanCategory.ACTIVITY,
                               status=SpanStatus.DONE, task_id="t_hairspring"))
        self.assertEqual([s.id for s in unmarked_spans(spans, at("15:10"))], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
