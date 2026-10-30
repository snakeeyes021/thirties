"""Planning across horizons (ROADMAP C2): periods, each item's horizon, week/month/year plans, candidates that
bubble down, the tools, the planning sessions, the guesses, and sync."""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.horizons import (HorizonCatalog, HorizonPlanner, HorizonPlans, candidates_for_day,
                                    candidates_for_month, candidates_for_week, due_horizon, parse_period, period_label,
                                    period_of, sort_horizons)
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

# DAY is Thursday, Oct 15 2026; with weeks starting Monday this week is Oct 12 - 18.
MON = date(2026, 10, 12)


def item(tid, title, note="n_home", note_title="Home", role="list", **kw):
    return TaskItem(id=tid, source_notebook="Tasks", title=title, is_checkbox_item=True, parent_note_title=note_title,
                    parent_note_id=note, list_role=role, **kw)


class TestPeriods(unittest.TestCase):

    def p(self, text, ws=0):
        got = parse_period(text, DAY, ws)
        return got.level, got.start, got.end

    def test_weeks_days_months_quarters_years(self):
        self.assertEqual(self.p("this week"), ("week", MON, MON + timedelta(days=6)))
        self.assertEqual(self.p("next week")[1], date(2026, 10, 19))
        self.assertEqual(self.p("last week")[1], date(2026, 10, 5))
        self.assertEqual(self.p("the week of Nov 11")[1:], (date(2026, 11, 9), date(2026, 11, 15)))
        self.assertEqual(self.p("Thursday"), ("day", DAY, DAY))
        self.assertEqual(self.p("next Thursday")[1], DAY + timedelta(days=7))
        self.assertEqual(self.p("Oct 30")[1], date(2026, 10, 30))
        self.assertEqual(self.p("this weekend")[1], date(2026, 10, 17))
        self.assertEqual(self.p("this month"), ("month", date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(self.p("in November"), ("month", date(2026, 11, 1), date(2026, 11, 30)))
        self.assertEqual(self.p("march")[1], date(2027, 3, 1))  # a bare month is the next one that isn't over
        self.assertEqual(self.p("February 2026")[1], date(2026, 2, 1))
        self.assertEqual(self.p("Q1"), ("quarter", date(2027, 1, 1), date(2027, 3, 31)))
        self.assertEqual(self.p("Q4")[1:], (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertEqual(self.p("next year"), ("year", date(2027, 1, 1), date(2027, 12, 31)))
        self.assertEqual(self.p("2028")[1], date(2028, 1, 1))
        self.assertEqual(self.p("sometime this year")[0], "year")

    def test_the_week_starts_on_the_users_day(self):
        self.assertEqual(self.p("this week", ws=6)[1], date(2026, 10, 11))  # Sunday

    def test_unreadable_says_how(self):
        with self.assertRaises(ValueError) as e:
            parse_period("whenever the moon is blue", DAY)
        self.assertIn("'this week'", str(e.exception))

    def test_labels(self):
        label = lambda text: period_label(parse_period(text, DAY), DAY)
        self.assertEqual([label(t) for t in ("today", "tomorrow", "this week", "next week", "November", "this month",
                                               "Q1", "next year")],
                         ["today", "tomorrow", "this week", "next week", "November", "this month (October)", "Q1 2027",
                          "2027"])

    def test_a_due_date_sets_the_horizon_and_tightens_as_it_nears(self):
        self.assertEqual(due_horizon(DAY + timedelta(days=1), DAY), "day")
        self.assertEqual(due_horizon(date(2026, 10, 18), DAY), "week")  # Sunday, this week
        self.assertEqual(due_horizon(date(2026, 10, 28), DAY), "month")
        self.assertEqual(due_horizon(date(2026, 12, 20), DAY), "year")
        self.assertEqual(due_horizon(date(2026, 10, 28), date(2026, 10, 27)), "day")


class TestItemHorizons(unittest.TestCase):

    def setUp(self):
        self.db = make_scheduler().state_db
        self.c = HorizonCatalog(self.db)
        self.lists = ListCatalog(self.db)

    def h(self, t):
        return self.c.effective(t, DAY)

    def test_obvious_cases_get_defaults(self):
        self.assertEqual(self.h(item("a", "Fix the door")), "year")  # nothing known: no particular hurry
        self.assertEqual(self.h(item("b", "Wishlist pedal", role="reference")), "someday")
        self.assertEqual(self.h(item("c", "Shower", role="routine")), "day")
        self.assertEqual(self.h(item("d", "Call the bank", due="2026-10-16")), "day")
        self.assertEqual(self.h(item("e", "Learn the banjo", someday=True)), "someday")

    def test_urgency_maps_onto_horizons(self):
        for urgency, horizon in (("now", "day"), ("soon", "week"), ("whenever", "year"), ("someday", "someday")):
            self.lists.set_urgency("u", urgency, source="guess")
            self.assertEqual(self.h(item("u", "Thing")), horizon, urgency)

    def test_the_notes_horizon_is_the_default_for_its_items(self):
        self.c.set_note("n_wedding", "month", "Wedding", source="guess")
        thanks = item("w1", "Thank-you cards", "n_wedding", "Wedding")
        self.assertEqual(self.h(thanks), "month")
        self.lists.set_urgency("w1", "whenever", source="guess")  # "whenever" says nothing: the note fills in
        self.assertEqual(self.h(thanks), "month")
        self.lists.set_urgency("w1", "soon", source="guess")  # a pressing guess is finer than the note
        self.assertEqual(self.h(thanks), "week")
        self.c.set_item("w1", "year", source="guess")  # the model's guess for the item itself
        self.assertEqual(self.h(thanks), "year")

    def test_the_user_wins_and_a_guess_never_replaces_it(self):
        t = item("a", "Fix the door", due="2026-10-16")
        self.c.set_item("a", "month", source="user")
        self.assertFalse(self.c.set_item("a", "day", source="guess"))
        self.assertEqual(self.h(t), "month")  # even over a due date: the user said so
        gutter = item("g", "Gutters", someday=True)
        self.lists.set_urgency("g", "soon", source="user")
        self.assertEqual(self.h(gutter), "week")  # the user's urgency beats a Someday heading

    def test_clearing_a_notes_guesses_keeps_the_users_choices(self):
        self.c.set_item("a", "week", note_id="n_home", source="guess")
        self.c.set_item("b", "day", note_id="n_home", source="user")
        self.assertEqual(self.c.clear_item_guesses("n_home"), 1)
        self.assertEqual(set(self.c.items()), {"b"})

    def test_what_needs_a_models_guess(self):
        ListCatalog(self.db).set_role("n_home", "list", "Home")
        ListCatalog(self.db).set_role("n_ref", "reference", "Ideas")
        self.lists.set_urgency("now", "now", source="guess")
        self.lists.set_urgency("mine", "whenever", source="user")
        tasks = [item("plain", "Fix the door"), item("now", "Leak under sink"), item("mine", "Paint"),
                 item("dated", "Renew passport", due="2027-01-01"), item("maybe", "Banjo", someday=True),
                 item("ref", "Idea", "n_ref", "Ideas", role="reference")]
        need = self.c.items_without_horizon(tasks, ListCatalog(self.db).roles())
        self.assertEqual({k: [t.id for t in v] for k, v in need.items()}, {"n_home": ["plain"]})
        notes = [{"id": "n_home", "title": "Home"}, {"id": "n_ref", "title": "Ideas"}]
        self.assertEqual([n["id"] for n in self.c.notes_without_horizon(notes, ListCatalog(self.db).roles())],
                         ["n_home"])  # reference notes are someday by default: no guess

    def test_sorting_guesses_notes_then_items(self):
        ListCatalog(self.db).set_role("n_home", "list", "Home")
        sorter = MagicMock()
        sorter.note_horizon.return_value = "year"
        sorter.horizons.return_value = {"a": "month"}
        notes = [{"id": "n_home", "title": "Home", "body": "- [ ] Fix the door\n- [ ] Paint"}]
        counts = sort_horizons(self.c, notes, [item("a", "Fix the door"), item("b", "Paint")], sorter, DAY)
        self.assertEqual(counts, {"notes": 1, "items": 2})
        self.assertEqual(self.c.notes()["n_home"]["horizon"], "year")
        self.assertEqual({k: v["horizon"] for k, v in self.c.items().items()}, {"a": "month", "b": "year"})
        self.assertEqual(sorter.horizons.call_args[0][4], "year")  # the model sees the note's horizon
        self.assertEqual(sort_horizons(self.c, notes, [], object(), DAY), {"notes": 0, "items": 0})  # no guessing

    def test_the_backlog_carries_each_items_horizon(self):
        from thirties_core.backlog import load_backlog
        sched = make_scheduler()
        joplin = MagicMock()
        joplin.fetch_tasks.return_value = [item("a", "Fix the door", role="")]
        joplin.fetch_active_notes.return_value = [{"id": "n_home", "title": "Home"}]
        HorizonCatalog(sched.state_db).set_item("a", "month")
        tasks, _ = load_backlog(sched.config, joplin, sched)
        self.assertEqual(tasks[0].horizon, "month")

    def test_syncs_last_write_wins_with_removals(self):
        self.c.set_note("n1", "month", "Home")
        plans = HorizonPlans(self.db)
        week = period_of("week", DAY)
        plans.put("a", "Fix the door", "n_home", "Home", week)
        plans.take_out(f"{week.key}|a")
        other_db = make_scheduler().state_db
        other_c, other_p = HorizonCatalog(other_db), HorizonPlans(other_db)
        for r in self.c.all_records():
            self.assertTrue(other_c.merge_record(r))
        for r in plans.all_records():
            self.assertTrue(other_p.merge_record(r))
        self.assertEqual(other_c.notes()["n1"]["horizon"], "month")
        self.assertEqual(other_p.rows(), [])  # taken out there too
        self.assertFalse(other_c.merge_record(self.c.all_records()[0]))

    def test_registered_for_sync(self):
        from thirties_core.sync_engine import SyncEngine
        stores = SyncEngine(make_scheduler().config, self.db, MagicMock())._stores()
        self.assertIsInstance(stores["horizons"], HorizonCatalog)
        self.assertIsInstance(stores["horizon_plans"], HorizonPlans)


class PlannerCase(unittest.TestCase):
    """A planner on Thursday Oct 15 at 10:00, with a few list items."""

    when = at("10:00")

    def setUp(self):
        set_debug_time(self.when)
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.tasks = [item("door", "Fix the door"), item("ent", "Call the ENT", "n_med", "Medical"),
                      item("gutter", "Clean the gutters"), item("banjo", "Learn the banjo", someday=True),
                      item("pedal", "Buy a pedal", "n_wish", "Wishlist", role="reference"),
                      item("paint", "Paint the hall"), item("tax", "File the taxes", due="2026-10-16"),
                      item("cards", "Thank-you cards", "n_wed", "Wedding", role="project")]
        self.build()

    def build(self, day=None):
        plan, pending = self.sched.build_day_plan(day or self.when.date())
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db, self.tasks))
        self.hp = HorizonPlanner(self.t)

    def ids(self, cands):
        return [c.task_id for c in cands]

    def by_id(self, cands):
        return {c.task_id: c for c in cands}


class TestPinning(PlannerCase):

    def rows(self):
        return sorted((r["level"], r["period"], r["slot"], r["item_id"]) for r in HorizonPlans(self.sched.state_db).rows())

    def test_a_day_is_a_slot_of_its_week_and_a_quarter_of_its_year(self):
        door = self.tasks[0]
        self.hp.pin(door, parse_period("Saturday", DAY))
        self.assertEqual(self.rows(), [("week", "2026-10-12", "2026-10-17", "door")])
        self.hp.pin(self.tasks[2], parse_period("Q1", DAY))
        self.assertIn(("year", "2027-01-01", "2027-01-01", "gutter"), self.rows())

    def test_one_place_per_thing(self):
        door = self.tasks[0]
        self.hp.pin(door, parse_period("November", DAY))
        self.hp.pin(door, parse_period("the week of Nov 9", DAY))
        self.assertEqual(len(self.rows()), 2)  # the week is inside November: both stand
        self.hp.pin(door, parse_period("the week of Nov 16", DAY))  # moved within November
        self.assertEqual(self.rows(), [("month", "2026-11-01", "", "door"), ("week", "2026-11-16", "", "door")])
        self.hp.pin(door, parse_period("December", DAY))  # out of November altogether
        self.assertEqual(self.rows(), [("month", "2026-12-01", "", "door")])

    def test_taking_out(self):
        door = self.tasks[0]
        self.hp.pin(door, parse_period("November", DAY))
        self.hp.pin(door, parse_period("Nov 12", DAY))
        self.hp.unpin("door", parse_period("the week of Nov 9", DAY))
        self.assertEqual(self.rows(), [("month", "2026-11-01", "", "door")])  # the month keeps it
        self.hp.unpin("door")
        self.assertEqual(self.rows(), [])


class TestBubblingDown(PlannerCase):

    def test_a_day_draws_on_its_week_plan_first(self):
        self.hp.pin(self.tasks[1], parse_period("today", DAY))            # ENT: pinned to today
        self.hp.pin(self.tasks[0], parse_period("this week", DAY))        # door: this week, no day yet
        self.hp.pin(self.tasks[5], parse_period("Saturday", DAY))         # paint: another day of the week
        self.hp.pin(self.tasks[2], parse_period("next week", DAY))        # gutters: placed elsewhere
        ListCatalog(self.sched.state_db).set_urgency("cards", "soon", source="guess")  # for this week
        got = candidates_for_day(self.t)
        self.assertEqual(self.ids(got), ["ent", "tax", "door", "cards"])
        c = self.by_id(got)
        self.assertEqual((c["ent"].source, c["ent"].why), ("pinned", "pinned to today"))
        self.assertEqual(c["tax"].why, "due tomorrow")
        self.assertEqual(c["door"].why, "from the plan for this week")
        self.assertEqual((c["cards"].source, c["cards"].why), ("upper", "for this week"))
        self.assertIn("Call the ENT [Medical] (pinned to today) id=ent", c["ent"].line())

    def test_never_whats_done_scheduled_on_today_or_someday(self):
        self.hp.pin(self.tasks[1], parse_period("today", DAY))
        self.hp.pin(self.tasks[0], parse_period("this week", DAY))
        self.sched.state_db.record_task_completed("ent", DAY)
        self.sched.state_db.upsert_span(EventSpan("s1", "Door", at("14:00"), at("15:00"), SpanCategory.TASK,
                                                  task_id="door"))
        from thirties_core.today import TodayLinks
        TodayLinks(self.sched.state_db).link("File the taxes", self.tasks[6])
        self.assertEqual(self.ids(candidates_for_day(self.t)), [])

    def test_done_long_ago_and_open_again_is_a_new_round(self):
        self.hp.pin(self.tasks[0], parse_period("this week", DAY))
        self.sched.state_db.record_task_completed("door", DAY - timedelta(days=60))
        self.assertIn("door", self.ids(candidates_for_day(self.t)))

    def test_whats_left_from_an_earlier_day_comes_back(self):
        self.hp.pin(self.tasks[0], parse_period("Monday", MON))  # Monday's, not done
        c = self.by_id(candidates_for_day(self.t))
        self.assertEqual((c["door"].source, c["door"].why), ("carried", "left over from an earlier day"))

    def test_a_week_draws_on_its_month_and_what_was_left_last_week(self):
        HorizonPlans(self.sched.state_db).put("gutter", "Clean the gutters", "n_home", "Home",
                                              period_of("week", MON - timedelta(days=7)))  # last week's
        self.hp.pin(self.tasks[0], parse_period("this month", DAY))
        HorizonCatalog(self.sched.state_db).set_item("paint", "month")
        HorizonCatalog(self.sched.state_db).set_item("cards", "week")
        got = self.by_id(candidates_for_week(self.t, limit=None))
        self.assertEqual(got["gutter"].source, "carried")
        self.assertEqual(got["door"].why, "from the plan for this month (October)")
        self.assertEqual(got["cards"].source, "belongs")
        self.assertEqual(got["paint"].source, "upper")
        self.assertNotIn("banjo", got)
        self.assertNotIn("pedal", got)
        order = [c.task_id for c in candidates_for_week(self.t, limit=None)]
        self.assertLess(order.index("tax"), order.index("gutter"))  # due first, then left over, then the month's
        self.assertLess(order.index("gutter"), order.index("door"))

    def test_a_month_draws_on_its_years_quarter(self):
        self.hp.pin(self.tasks[0], parse_period("Q4", DAY))
        self.hp.pin(self.tasks[2], parse_period("Q1", DAY))
        got = self.by_id(candidates_for_month(self.t, limit=None))
        self.assertEqual(got["door"].why, "from the plan for this year")
        self.assertNotIn("gutter", got)  # placed in Q1

    def test_the_week_plan_shows_whats_done_and_how_full_each_day_is(self):
        self.hp.pin(self.tasks[0], parse_period("this week", DAY))
        self.hp.pin(self.tasks[1], parse_period("Saturday", DAY))
        self.sched.state_db.record_task_completed("door", DAY)
        text = self.hp.describe_plan(parse_period("this week", DAY))
        self.assertIn("This week (Oct 12 – 18):", text)
        self.assertIn("- Fix the door (done)", text)
        self.assertIn("- Saturday: Call the ENT", text)
        self.assertIn("Sat Oct 17: no work, ~", text)
        self.assertIn("1 pinned from the week", text)
        self.assertNotIn("Mon Oct 12", text)  # past days aren't room


class TestTools(PlannerCase):

    def run_tool(self, name, **args):
        return self.t.execute(name, args)

    def test_set_horizon_for_an_item_keeps_urgency_in_step(self):
        out = self.run_tool("set_horizon", task="Fix the door", horizon="this month")
        self.assertEqual(out, "Noted: 'Fix the door' is for this month.")
        self.assertEqual(HorizonCatalog(self.sched.state_db).items()["door"]["source"], "user")
        self.assertEqual(ListCatalog(self.sched.state_db).urgencies()["door"]["urgency"], "whenever")
        self.assertTrue(self.run_tool("set_horizon", task="door thing", horizon="week").startswith(NOT_DONE))
        self.assertTrue(self.run_tool("set_horizon", task="door", horizon="eventually").startswith(NOT_DONE))

    def test_set_horizon_for_a_whole_list(self):
        catalog = HorizonCatalog(self.sched.state_db)
        catalog.set_item("door", "week", note_id="n_home", source="guess")
        out = self.run_tool("set_horizon", list="Home", horizon="someday")
        self.assertEqual(out, "Noted: Home is someday (its 1 guessed item follow it).")
        self.assertEqual(self.hp.horizon_of(self.tasks[0]), "someday")
        self.assertTrue(self.run_tool("set_horizon", list="Garage", horizon="year").startswith(NOT_DONE))

    def test_pin_and_unpin(self):
        self.assertEqual(self.run_tool("pin_to_period", task="door", when="Saturday"),
                         "'Fix the door' is planned for Saturday.")
        self.assertTrue(self.run_tool("pin_to_period", task="door", when="last week").startswith(NOT_DONE))
        self.assertTrue(self.run_tool("pin_to_period", task="door", when="the blue moon").startswith(NOT_DONE))
        out = self.run_tool("pin_to_period", task="Fix the dor", when="Saturday")
        self.assertTrue(out.startswith(NOT_DONE) and "add_task" in out, out)
        self.assertIn("Saturday: Fix the door", self.run_tool("show_period", when="this week"))
        self.assertEqual(self.run_tool("unpin_from_period", task="door"), "Took 'Fix the door' out of this week.")
        self.assertTrue(self.run_tool("unpin_from_period", task="door").startswith(NOT_DONE))

    def test_propose_then_apply_with_a_skip(self):
        self.hp.pin(self.tasks[2], parse_period("this month", DAY))
        out = self.run_tool("propose_period", when="this week")
        self.assertIn("Candidates (best first", out)
        self.assertIn("- Clean the gutters [Home] (from the plan for this month (October)) id=gutter", out)
        self.assertIn("Suggested: File the taxes; Clean the gutters", out)
        out = self.run_tool("apply_period_proposal", skip="taxes")
        self.assertEqual(out, "Planned for this week: Clean the gutters.")
        self.assertEqual(self.run_tool("apply_period_proposal", when="this week"), "This week is already planned like that.")
        self.assertTrue(self.run_tool("propose_period", when="today").startswith(NOT_DONE))

    def test_apply_the_models_own_picks(self):
        self.run_tool("propose_period", when="this week")
        out = self.run_tool("apply_period_proposal", items=["Paint the hall", {"task": "door", "when": "Saturday"}])
        self.assertEqual(out, "Planned for this week: Paint the hall, Fix the door (Saturday).")
        out = self.run_tool("apply_period_proposal", when="this week", items=[{"task": "door", "when": "November"}])
        self.assertTrue(out.startswith(NOT_DONE) and "isn't within" in out, out)

    def test_read_only_tools_are_marked(self):
        from thirties_core.plan_tools import READ_ONLY_TOOLS, TOOL_SPECS
        self.assertTrue({"show_period", "propose_period"} <= READ_ONLY_TOOLS)
        self.assertTrue({"set_horizon", "pin_to_period", "apply_period_proposal"} <= {s["name"] for s in TOOL_SPECS})

    def test_the_frontier_sees_the_weeks_plan(self):
        from thirties_core.brains.frontier import FrontierBrain
        self.hp.pin(self.tasks[0], parse_period("Saturday", DAY))
        self.hp.pin(self.tasks[2], parse_period("November", DAY))
        text = FrontierBrain(self.t, None, tasks=self.tasks, active_notes=[]).build_backlog_summary()
        self.assertIn("PLANS FOR THE WEEK, MONTH AND YEAR", text)
        self.assertIn("This week:\n- Fix the door (Saturday) id=door", text)
        self.assertNotIn("Clean the gutters", text.split("PLANS FOR")[1].split("\n\n")[0])  # November isn't now


class TestSessions(PlannerCase):

    when = at("10:00", 4)  # Monday Oct 19: a new week

    def test_offered_once_on_the_weeks_first_morning_and_applied_on_yes(self):
        text = self.hp.offer_session()
        self.assertTrue(text.startswith("A new week. For this week I'd plan:\n- File the taxes (overdue)"), text)
        self.assertTrue(text.endswith("Want this week like that?"))
        self.assertIsNone(self.hp.offer_session())  # once
        self.assertEqual(self.hp.pending(), period_of("week", self.when.date()))
        out = self.t.execute("apply_period_proposal", {})
        self.assertTrue(out.startswith("Planned for this week: File the taxes"), out)
        self.assertIsNone(self.hp.pending())

    def test_a_declined_offer_isnt_pending(self):
        self.hp.offer_session()
        self.hp.decline()
        self.assertIsNone(self.hp.pending())

    def test_an_unanswered_offer_goes_stale(self):
        self.hp.offer_session()
        set_debug_time(at("15:00", 4))
        self.assertIsNone(self.hp.pending())

    def test_nothing_to_plan_is_silent(self):
        self.tasks[:] = [self.tasks[3]]  # only a someday item
        self.build()
        self.assertIsNone(self.hp.offer_session())

    def test_the_month_comes_first_and_the_week_the_next_morning(self):
        set_debug_time(at("10:00", 17))  # Sunday Nov 1
        self.build()
        self.assertTrue(self.hp.offer_session().startswith("A new month (November). For this month (November)"))
        set_debug_time(at("10:00", 18))  # Monday Nov 2
        self.build()
        self.assertTrue(self.hp.offer_session().startswith("A new week."))

    def test_the_background_offers_it_after_the_days_own_offer(self):
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = list(self.tasks), []
        ListCatalog(self.sched.state_db).set_urgency("tax", "now", date(2026, 10, 16))  # a due date lives here
        sent = []
        svc = BackgroundService(self.sched.config, lambda t, b, k: sent.append((k, b)), scheduler=self.sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: self.when.date()
        svc.bridge = MagicMock()
        svc.horizon_session(self.when)
        self.assertEqual(sent, [])  # the morning hasn't happened yet
        svc.rollover(self.when)  # nothing from yesterday; the day's own proposal may go out
        sent[:] = [x for x in sent if x[0] != "proposal"]
        assistant = svc.manager().tools.assistant
        if assistant.proposals.pending():
            svc.horizon_session(self.when)
            self.assertEqual(sent, [])  # one offer at a time
            assistant.proposals.decline()
        svc.horizon_session(self.when)
        self.assertEqual([k for k, _ in sent], ["horizon"])
        self.assertTrue(sent[0][1].startswith("📅 A new week."))
        self.assertIn("A new week.", svc.manager().history()[-1]["content"])


class TestSlmAnswersTheOffer(unittest.TestCase):

    def setUp(self):
        from tests.test_slm_brain import TASKS, FakeRuntime
        from thirties_core.brains.slm.brain import SlmBrain
        from thirties_core.conversation import ConversationManager
        set_debug_time(at("10:00", 4))
        self.addCleanup(set_debug_time, None)
        self.rt = FakeRuntime()
        self.sched = make_scheduler()
        tasks = list(TASKS) + [item("door", "Fix the door")]
        HorizonCatalog(self.sched.state_db).set_item("door", "week")
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=4))
        self.m = ConversationManager(plan, tasks, pending, self.sched, active_notes=[], brain=None,
                                     inference_engine=None)
        self.m.brain = SlmBrain(self.m.tools, tasks=tasks, runtime=self.rt)
        self.m._load(None)
        self.m.add_assistant_message("📅 " + HorizonPlanner(self.m.tools).offer_session())

    def say(self, text):
        from tests.test_slm_brain import actions
        self.rt.parse_replies.append(actions({"type": "chat"}))
        self.m.send_user_message(text)
        return self.m.last_raw_reply

    def test_yes_applies_it(self):
        self.assertIn("Planned for this week: Fix the door", self.say("yes please"))

    def test_no_declines_it(self):
        self.assertEqual(self.say("no thanks"), "Okay, no plan for it then. Ask any time.")
        self.assertIsNone(HorizonPlanner(self.m.tools).pending())


class TestSorters(unittest.TestCase):

    def test_gemma_one_choice_per_item_and_day_needs_a_sign(self):
        from thirties_core.brains.list_sorter import GemmaListSorter
        runtime = MagicMock()
        runtime.json_call.side_effect = [{"horizon": "day"}, {"horizon": "day"}, {"horizon": "month"}]
        sorter = GemmaListSorter(runtime)
        items = [item("a", "Paint the hall"), item("b", "Fix the leak under the sink"), item("c", "Gutters")]
        out = sorter.horizons({"title": "Home", "body": ""}, "list", items, DAY, "year")
        self.assertEqual(out, {"a": "week", "b": "day", "c": "month"})
        runtime.json_call.side_effect = None
        runtime.json_call.return_value = {"horizon": "month"}
        self.assertEqual(sorter.note_horizon({"title": "Wedding", "body": ""}, "project"), "month")

    def test_gemini_a_whole_note_per_call(self):
        from thirties_core.brains.list_sorter import FrontierListSorter
        engine = MagicMock()
        engine.chat.return_value = {"content": json.dumps({"items": [{"n": 1, "horizon": "month"},
                                                                     {"n": 2, "horizon": "soonish"}]})}
        out = FrontierListSorter(engine).horizons({"title": "Home", "body": "- [ ] Paint\n- [ ] Gutters"}, "list",
                                                  [item("a", "Paint"), item("b", "Gutters")], DAY)
        self.assertEqual(out, {"a": "month"})


if __name__ == "__main__":
    unittest.main()
