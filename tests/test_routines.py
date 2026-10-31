"""Routines and daily anchors set themselves up each morning; weekly patterns; the assistant proposes the day.

R1 (12+ days, 10-18 to 10-30): "Just put them on the stinking schedule! This is your job!" R2: "That is not how we've
ever done it." A6: "Surely I have pressing things, no??"
"""

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, event, make_scheduler
from thirties_core.assistant_tools import Services, _checkbox_id
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.routines import Routine, RoutineBook, describe_repeat, is_due, next_due, parse_repeat
from thirties_core.week import day_hours, describe_week, parse_days

TITLES = {"n_today": "Today To-Do", "n_errands": "Errands/Admin", "n_routine": "Morning Routine",
          "n_clean": "Deep Clean", "n_pack": "Packing List", "n_dev": "Thirties"}
STEPS = ["Feed kitties", "Caffeinate", "Water bottle", "Water kitties", "Breakfast (w/ vitamins)", "Throne", "Shower",
         "PT"]
FIRST = ["Feed kitties", "Caffeinate", "Water bottle", "Water kitties", "Breakfast (w/ vitamins)"]
SECOND = ["Throne", "Shower", "PT", "Brush teeth"]  # Brush teeth isn't in the note (10-30): planned with it anyway
TOMORROW = DAY + timedelta(days=1)


def item(tid, title, note="n_errands", **kw):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=TITLES[note], parent_note_id=note, **kw)


class RoutineCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("08:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.routine_body = "".join(f"- [ ] {s}\n" for s in STEPS)
        self.api = FakeJoplinAPI({
            "n_today": {"id": "n_today", "body": ""},
            "n_errands": {"id": "n_errands", "body": "- [ ] Call the bank\n- [ ] Buy stamps\n"},
            "n_routine": {"id": "n_routine", "body": self.routine_body},
            "n_clean": {"id": "n_clean", "body": "- [ ] Vacuum\n- [ ] Mop\n"},
        })
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None  # the lists aren't re-read from a real Joplin
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES[nid], "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        self.catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_errands", "list"), ("n_routine", "routine"),
                          ("n_clean", "routine")):
            self.catalog.set_role(nid, role, TITLES[nid])
        self.services = Services.for_db(self.sched.state_db, self.catalog.apply(
            [item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps")]), joplin)
        self.t = self.tools_for(DAY)

    def tools_for(self, day):
        plan, pending = self.sched.build_day_plan(day)
        return PlanTools(plan, pending, self.sched, services=self.services)

    def body(self, note):
        return self.api.notes[note]["body"]

    def spans(self, tools, title):
        return [s for s in tools.day_plan.spans if s.title == title]

    def place_the_users_way(self, tools=None):
        out = (tools or self.t).execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "9:00 AM", "items": FIRST}, {"start": "3:00 PM", "items": SECOND}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out


class TestRepeatSchedules(unittest.TestCase):
    """10-30 (added by the user): every day, every other day, every 12 days, some weekdays, or on request; counted
    from the last time it was done."""

    def test_reading_how_often(self):
        gen = make_scheduler().config.general
        cases = {"daily": {"every": "day"}, "every other day": {"every": "days", "n": 2},
                 "every 12 days": {"every": "days", "n": 12}, "every two weeks": {"every": "days", "n": 14},
                 "weekly": {"every": "days", "n": 7}, "work days": {"every": "work_days"},
                 "days off": {"every": "days_off"}, "on request": {"every": "asked"},
                 "only when I ask": {"every": "asked"},
                 "Mondays and Thursdays": {"every": "weekdays", "days": ["mon", "thu"]},
                 "weekends": {"every": "weekdays", "days": ["sat", "sun"]}}
        for text, want in cases.items():
            self.assertEqual(parse_repeat(text, gen), want, text)
        self.assertEqual(describe_repeat({"every": "days", "n": 12}), "every 12 days")
        self.assertEqual(describe_repeat({"every": "weekdays", "days": ["mon", "thu"]}), "on Mon, Thu")
        with self.assertRaises(ValueError):
            parse_repeat("now and then-ish", gen)

    def test_counted_from_the_last_time_it_was_done(self):
        gen = make_scheduler().config.general
        r = Routine("n", "Deep Clean", {"every": "days", "n": 12})
        self.assertTrue(is_due(r, DAY, gen, None))  # never done: due
        self.assertFalse(is_due(r, DAY, gen, DAY - timedelta(days=5)))
        self.assertTrue(is_due(r, DAY, gen, DAY - timedelta(days=12)))
        self.assertTrue(is_due(r, DAY, gen, DAY - timedelta(days=20)))  # overdue stays due until it's done
        self.assertEqual(next_due(r, DAY, gen, DAY - timedelta(days=2)), DAY + timedelta(days=10))
        other = Routine("n", "Laundry", {"every": "days", "n": 2})
        self.assertFalse(is_due(other, DAY, gen, DAY - timedelta(days=1)))
        self.assertTrue(is_due(other, DAY + timedelta(days=1), gen, DAY - timedelta(days=1)))

    def test_weekdays_work_days_and_on_request(self):
        gen = make_scheduler().config.general  # Mon-Fri; DAY is a Thursday
        self.assertTrue(is_due(Routine("n", "x", {"every": "work_days"}), DAY, gen, None))
        self.assertFalse(is_due(Routine("n", "x", {"every": "days_off"}), DAY, gen, None))
        self.assertTrue(is_due(Routine("n", "x", {"every": "days_off"}), DAY + timedelta(days=2), gen, None))
        self.assertFalse(is_due(Routine("n", "x", {"every": "weekdays", "days": ["mon"]}), DAY, gen, None))
        self.assertFalse(is_due(Routine("n", "Packing List"), DAY, gen, None))  # occasional by its name: on request
        self.assertTrue(is_due(Routine("n", "Morning Routine"), DAY, gen, None))  # a routine is daily by default


class TestTheMorningSetUp(RoutineCase):

    def test_the_users_placement_is_learned_and_goes_on_by_itself_the_next_morning(self):
        self.place_the_users_way()
        stored = RoutineBook(self.sched.state_db).get("n_routine")
        self.assertEqual([(p["at"], p["items"]) for p in stored.parts], [("09:00", FIRST), ("15:00", SECOND)])
        self.assertEqual(stored.placement_source, "learned")
        self.assertEqual(self.body("n_routine"), self.routine_body)  # the template is never written to

        set_debug_time(at("07:30", 1))  # the next morning, nothing said yet
        t2 = self.tools_for(TOMORROW)
        out = t2.assistant.routines.set_up_current()
        self.assertIn("Morning Routine at 9:00 AM and 3:00 PM", out)
        self.assertEqual(self.spans(t2, "Feed kitties")[0].start_dt, at("09:00", 1))
        self.assertEqual(self.spans(t2, "Throne")[0].start_dt, at("15:00", 1))
        self.assertEqual(len(self.spans(t2, "Brush teeth")), 1)  # the extra step comes along
        self.assertTrue(all(s.end_dt <= at("15:30", 1) for s in self.spans(t2, "Brush teeth")))
        work = self.spans(t2, "Work")[0]
        self.assertEqual((work.start_dt, work.end_dt), (at("08:30", 1), at("16:00", 1)))  # never moved
        self.assertEqual(self.body("n_today"), "- [ ] Morning Routine\n" + "".join(
            f"    - [ ] {s}\n" for s in FIRST + SECOND))  # yesterday's group went, today's is one group
        self.assertEqual(self.body("n_routine"), self.routine_body)
        self.assertEqual(t2.assistant.routines.set_up_current(), "")  # once a day
        self.assertEqual(len(self.spans(t2, "Feed kitties")), 1)

    def test_an_extra_step_the_user_always_places_isnt_asked_about_again(self):
        self.assertIn("Ask the user whether to add it", self.place_the_users_way())
        set_debug_time(at("07:30", 1))
        t2 = self.tools_for(TOMORROW)
        t2.assistant.routines.set_up_current()
        t2.execute("remove_event", {"event": self.spans(t2, "Brush teeth")[0].id})
        out = t2.execute("put_routine", {"routine": "Morning Routine", "parts": [
            {"start": "3:00 PM", "items": ["Brush teeth"]}]})
        self.assertNotIn("Ask the user", out)

    def test_before_any_placement_its_read_from_recent_plans(self):
        for k in range(1, 4):  # the last three days, placed by hand: kitties at 10, shower things at 3
            for title, start in (("Feed kitties", "10:00"), ("Caffeinate", "10:00"), ("Shower", "15:00"),
                                 ("PT", "15:00")):
                self.sched.state_db.upsert_span(EventSpan(f"{title}{k}", title, at(start, -k),
                                                          at(start, -k) + timedelta(minutes=10), SpanCategory.TASK))
        self.sched.state_db.upsert_span(EventSpan("odd", "Shower", at("21:00", -1), at("21:10", -1),
                                                  SpanCategory.TASK))  # once in the evening: not the pattern
        out = self.t.assistant.routines.set_up_current()
        self.assertIn("Morning Routine at 10:00 AM and 3:00 PM", out)
        self.assertEqual(self.spans(self.t, "Shower")[0].start_dt.hour, 15)

    def test_a_part_whose_time_has_passed_goes_in_the_first_free_thirty(self):
        set_debug_time(at("13:00"))
        self.t.execute("set_routine", {"routine": "Morning Routine", "parts": [
            {"start": "9:00 AM", "items": FIRST}, {"start": "3:00 PM", "items": SECOND}]})
        out = self.t.assistant.routines.set_up_current()
        self.assertEqual(self.spans(self.t, "Throne")[0].start_dt, at("15:00"))  # still ahead: its usual time
        self.assertEqual(self.spans(self.t, "Feed kitties")[0].start_dt, at("16:00"))  # missed: after work
        self.assertIn("usually goes at 9:00 AM; that's passed, so it's at 4:00 PM", out)

    def test_planning_before_sunrise_sets_up_the_coming_day(self):
        self.t.execute("set_routine", {"routine": "Morning Routine", "parts": [{"start": "9:00 AM", "items": FIRST}]})
        set_debug_time(at("06:30"))  # dark: the diurnal date is still yesterday
        yesterday = self.tools_for(DAY - timedelta(days=1))
        self.assertEqual(yesterday.today(), DAY - timedelta(days=1))
        out = yesterday.assistant.routines.set_up_current()
        self.assertIn("Morning Routine at 9:00 AM", out)
        self.assertEqual(self.spans(self.tools_for(DAY), "Feed kitties")[0].start_dt, at("09:00"))
        self.assertFalse(self.spans(yesterday, "Feed kitties"))  # yesterday's waking hours are over: not set up

    def test_a_routine_thats_not_due_stays_off(self):
        self.t.execute("set_routine", {"routine": "Morning Routine", "parts": [{"start": "9:00 AM", "items": FIRST}]})
        self.t.execute("set_routine", {"routine": "Deep Clean", "repeat": "every 12 days", "last_done": "Oct 10",
                                       "parts": [{"start": "6:00 PM", "items": ["Vacuum", "Mop"]}]})
        out = self.t.assistant.routines.set_up_current()
        self.assertNotIn("Deep Clean", out)
        self.assertFalse(self.spans(self.t, "Vacuum"))
        section = self.t.assistant.routines.prompt_section()
        self.assertIn("Deep Clean: every 12 days; last done Oct 10; next due Thu Oct 22", section)

    def test_the_confirmed_placement_stays_until_the_user_changes_it(self):
        self.place_the_users_way()
        shower = self.spans(self.t, "Shower")[0]
        self.t.execute("update_event", {"event": shower.id, "start": "4:30 PM"})
        out = self.t.execute("set_routine", {"routine": "morning", "confirm": True,
                                             "fallback": "after work on work days"})
        self.assertIn("today's placement as the usual one", out)
        r = RoutineBook(self.sched.state_db).get("n_routine")
        self.assertEqual(r.placement_source, "user")
        self.assertIn({"items": ["Shower"], "at": "16:30", "fallback": "after work on work days"}, r.parts)
        t2 = self.tools_for(TOMORROW)  # a one-off placement another day doesn't change it
        t2.execute("put_routine", {"routine": "Morning Routine", "start": "7:00 PM"})
        self.assertEqual(RoutineBook(self.sched.state_db).get("n_routine").parts, r.parts)
        self.assertIn("(if missed: after work on work days)", self.t.assistant.routines.prompt_section())

    def test_two_routines_sharing_a_word_are_told_apart_by_name(self):
        out = self.t.execute("put_routine", {"routine": "Deep Clean", "start": "6:00 PM"})
        self.assertTrue(out.startswith("Put Deep Clean on the plan"), out)
        self.api.notes["n_pack"] = {"id": "n_pack", "body": "- [ ] Charger\n"}
        self.catalog.set_role("n_pack", "routine", "Packing List")
        TITLES["n_routine"] = "Morning Routine"
        out = self.t.execute("put_routine", {"routine": "Morning Routine", "start": "9:00 AM"})
        self.assertTrue(out.startswith("Put Morning Routine on the plan"), out)

    def test_set_up_happens_before_the_first_write_to_today(self):
        self.t.execute("set_anchor", {"title": "Lunch", "time": "12:00 PM"})
        self.t.execute("add_event", {"title": "Call the bank", "start": "5:00 PM", "duration": "30 min",
                                     "task_id": "t_bank"})  # the first write to Today: the day sets itself up
        self.assertTrue(self.spans(self.t, "Lunch"))
        self.assertIn("- [ ] Lunch", self.body("n_today"))


class TestDailyAnchors(RoutineCase):

    def setUp(self):
        super().setUp()
        self.t.execute("set_anchor", {"title": "Lunch", "time": "12:00 PM"})
        self.t.execute("set_anchor", {"title": "Dinner", "time": "6:00 PM", "duration": "1 hour"})
        self.t.execute("set_anchor", {"title": "Cats' dinner", "time": "5:00 PM", "fixed": True})

    def test_they_go_on_every_morning_and_a_fixed_one_keeps_its_time(self):
        out = self.t.assistant.routines.set_up_current()
        self.assertIn("Lunch at 12:00 PM; Cats' dinner at 5:00 PM; Dinner at 6:00 PM", out)
        dinner = self.spans(self.t, "Dinner")[0]
        self.assertEqual(dinner.duration_minutes, 60)  # dinner takes an hour (R6)
        cats = self.spans(self.t, "Cats' dinner")[0]
        self.assertTrue(self.t._is_fixed(cats))  # "that one's not allowed to move" (10-27)
        self.assertIn("- [ ] Lunch\n", self.body("n_today"))

    def test_a_fixed_anchor_sits_alongside_whatever_is_there(self):
        sched = make_scheduler([event("vet", "17:00", "17:30", "Vet call")])
        sched.state_db = self.sched.state_db
        self.sched.calendar_engine = sched.calendar_engine
        t = self.tools_for(DAY)
        t.assistant.routines.set_up_current()
        self.assertEqual(self.spans(t, "Cats' dinner")[0].start_dt, at("17:00"))

    def test_passed_anchors_arent_put_on(self):
        set_debug_time(at("13:00"))
        out = self.t.assistant.routines.set_up_current()
        self.assertNotIn("Lunch", out)  # it's moot
        self.assertIn("Dinner at 6:00 PM", out)

    def test_removed_and_on_some_days(self):
        self.t.execute("set_anchor", {"title": "Lunch", "remove": True})
        self.t.execute("set_anchor", {"title": "Dinner", "days": "weekends"})
        out = self.t.assistant.routines.set_up_current()
        self.assertEqual(out, "On today by themselves: Cats' dinner at 5:00 PM.")

    def test_added_after_the_set_up_it_goes_on_today_too(self):
        self.t.assistant.routines.set_up_current()
        out = self.t.execute("set_anchor", {"title": "Walk", "time": "7:00 PM"})
        self.assertIn("It's on today's plan at 7:00 PM", out)

    def test_they_dont_carry_over_and_leave_today_the_next_morning(self):
        self.t.assistant.routines.set_up_current()
        self.api.notes["n_today"]["body"] += "- [ ] Call the bank\n"
        self.assertEqual(self.t.assistant.today_carryover()["keep"], ["Call the bank"])
        lunch = self.spans(self.t, "Lunch")[0]
        self.assertFalse(self.t.assistant._carries_over(lunch))
        set_debug_time(at("07:30", 1))
        t2 = self.tools_for(TOMORROW)
        t2.assistant.routines.set_up_current()
        body = self.body("n_today")
        self.assertEqual(body.count("- [ ] Lunch"), 1)  # yesterday's went, today's is on
        self.assertIn("- [ ] Call the bank", body)

    def test_offered_once(self):
        book = RoutineBook(self.sched.state_db)
        for a in book.anchors():
            a.removed = True
            book.save_anchor(a)
        db = self.sched.state_db
        with db._get_connection() as conn:
            conn.execute("DELETE FROM daily_anchors")
        db.set_sync_meta("anchors_offered", "")
        self.assertIn("OFFER ONCE", self.t.assistant.routines.prompt_section())
        self.assertIn("Lunch at 12:00 PM (30m), Dinner at 6:00 PM (1h)",
                      self.t.assistant.routines.prompt_section())  # still today
        set_debug_time(at("09:00", 1))
        self.assertNotIn("OFFER ONCE", self.tools_for(TOMORROW).assistant.routines.prompt_section())


class TestSkipping(RoutineCase):

    def setUp(self):
        super().setUp()
        self.t.execute("set_routine", {"routine": "Deep Clean", "repeat": "every 12 days", "last_done": "Oct 3",
                                       "parts": [{"start": "6:00 PM", "items": ["Vacuum", "Mop"]}]})
        self.t.assistant.routines.set_up_current()

    def test_without_saying_how_it_asks_with_the_three_choices(self):
        self.assertTrue(self.spans(self.t, "Vacuum"))
        out = self.t.execute("skip_routine", {"routine": "deep clean"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("skip Deep Clean today (it's back Fri Oct 16), skip this time (the next one is Tue Oct 27), or "
                      "skip it until a day they pick", out)
        choices = self.t.assistant.routines.skip_choices("Deep Clean")
        self.assertEqual([(c["how"], c["back"]) for c in choices],
                         [("today", date(2026, 10, 16)), ("this_time", date(2026, 10, 27)), ("until", None)])

    def test_skip_this_time_is_a_full_cycle_later(self):
        out = self.t.execute("skip_routine", {"routine": "Deep Clean", "how": "this time"})
        self.assertEqual(out, "Skipped Deep Clean (2 planned steps off today's plan). Next: Tue Oct 27.")
        self.assertEqual({s.status for s in self.spans(self.t, "Vacuum")}, {SpanStatus.SKIPPED})
        self.assertNotIn("Deep Clean", self.body("n_today"))
        self.assertIsNone(self.sched.state_db.get_task_history(_checkbox_id("n_clean", "Vacuum")))  # not "put off"

    def test_skip_today_and_until(self):
        self.assertIn("Next: Fri Oct 16", self.t.execute("skip_routine", {"routine": "Deep Clean", "how": "today"}))
        out = self.t.execute("skip_routine", {"routine": "Deep Clean", "how": "until", "until": "Monday"})
        self.assertIn("Next: Mon Oct 19", out)

    def test_the_day_page_lists_what_can_still_be_skipped(self):
        r = self.t.assistant.routines
        got = {d["title"]: d for d in r.day_routines()}
        self.assertEqual(got["Deep Clean"]["state"], "On the plan at 6:00 PM")
        self.assertEqual([(c["how"], c["back"]) for c in got["Deep Clean"]["choices"]],
                         [("today", date(2026, 10, 16)), ("this_time", date(2026, 10, 27)), ("until", None)])
        self.t.execute("skip_routine", {"routine": "Deep Clean", "how": "this time"})  # what the Skip button calls
        self.assertNotIn("Deep Clean", {d["title"] for d in r.day_routines()})  # skipped: nothing left to skip
        tomorrow = self.tools_for(TOMORROW)
        self.assertNotIn("Deep Clean", {d["title"] for d in tomorrow.assistant.routines.day_routines()})  # not due

    def test_done_counts_from_when_it_was_really_done(self):
        vacuum = self.spans(self.t, "Vacuum")[0]
        self.t.execute("log_outcome", {"event": vacuum.id, "status": "done"})
        self.assertIn("next due Tue Oct 27", self.t.assistant.routines.prompt_section())


class TestWeeklyPatterns(RoutineCase):

    def test_hours_by_weekday(self):
        gen = self.sched.config.general
        gen.week = {"sat": {"sleep_end_thirty": 18}, "fri": {"sleep_start_thirty": 0}}
        self.assertEqual(day_hours(gen, date(2026, 10, 17)).sleep_end, 18)  # a Saturday
        spans = {s.title: s for s in self.sched.routine_spans(date(2026, 10, 16))}  # Friday night
        self.assertEqual(spans["Sleep"].start_dt, at("00:00", 2))
        self.assertEqual(spans["Sleep"].end_dt, at("09:00", 2))  # Saturday's sleep-in
        self.assertEqual(parse_days("Mon-Thu"), ["mon", "tue", "wed", "thu"])
        self.assertEqual(parse_days("Monday and Thursday"), ["mon", "thu"])
        self.assertIn("Mon, Tue, Wed, Thu: work 8:30 AM – 4:00 PM", describe_week(gen))

    def test_the_user_says_their_week_and_work_follows_from_today(self):
        self.t.user_message = "I work 7 to 3 on weekdays, off weekends"
        out = self.t.execute("set_week", {"days": "weekdays", "work_start": "7:00 AM", "work_end": "3:00 PM"})
        self.assertIn("weekdays: work 7:00 AM – 3:00 PM", out)
        self.t.execute("set_week", {"days": "weekends", "no_work": True})
        gen = self.sched.config.general
        self.assertEqual((gen.work_start_thirty, gen.work_end_thirty), (14, 29))  # the usual hours (Preferences)
        work = self.spans(self.t, "Work")[0]
        self.assertEqual((work.start_dt, work.end_dt), (at("07:00"), at("15:00")))  # today's untouched Work
        from thirties_core.settings import Settings
        self.assertEqual(Settings(self.sched.state_db).get("general.work_start_thirty"), 14)  # synced setting

    def test_some_days_differ_and_a_hand_moved_work_stays(self):
        self.t.execute("update_event", {"event": "Work", "end": "5:00 PM"})
        self.t.user_message = "on Thursdays and Fridays I work 10 to 6"
        self.t.execute("set_week", {"days": "Thu, Fri", "work_start": "10:00 AM", "work_end": "6:00 PM"})
        gen = self.sched.config.general
        self.assertEqual(gen.week, {"thu": {"work_start_thirty": 20, "work_end_thirty": 35},
                                    "fri": {"work_start_thirty": 20, "work_end_thirty": 35}})
        self.assertEqual(self.spans(self.t, "Work")[0].end_dt, at("17:00"))  # moved by hand: kept
        friday = {s.title: s for s in self.sched.routine_spans(TOMORROW)}
        self.assertEqual(friday["Work"].start_dt, at("10:00", 1))

    def test_preferences_keep_only_what_differs(self):
        from thirties_core.week import describe_day, with_day_hour
        gen = self.sched.config.general
        gen.week = with_day_hour(gen, "sat", "sleep_end_thirty", 18)  # up at 9 on Saturdays
        self.assertEqual(gen.week, {"sat": {"sleep_end_thirty": 18}})
        self.assertEqual(describe_day(gen, "sat"), "No work · bed 11:00 PM · up 9:00 AM")
        self.assertEqual(describe_day(gen, "mon"), "Usual hours")
        gen.week = with_day_hour(gen, "sat", "sleep_end_thirty", gen.sleep_end_thirty)  # back to the usual
        self.assertEqual(gen.week, {})
        with self.assertRaises(ValueError):
            with_day_hour(gen, "sat", "lunch_thirty", 24)

    def test_only_when_the_user_talks_about_it(self):
        self.t.user_message = "add lunch at noon"
        out = self.t.execute("set_week", {"days": "weekdays", "work_start": "7:00 AM", "work_end": "3:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestPlacedOnWaking(RoutineCase):
    """10-30: "the two morning routine blocks go immediately after waking up"; a late riser does the first chunk on
    waking and the second right after (or after work: their call)."""

    def test_parts_follow_waking(self):
        out = self.t.execute("set_routine", {"routine": "Morning Routine", "confirm": True, "parts": [
            {"start": "on waking", "items": ["Feed kitties", "Caffeinate"]},
            {"start": "right after that", "items": ["Shower", "PT"]}]})
        self.assertIn("on waking: Feed kitties, Caffeinate", out)
        self.assertIn("right after the part before: Shower, PT", out)
        book = self.t.assistant.routines.book()
        note = self.t.assistant._routines()[0]
        self.assertEqual([p["at"] for p in book.get(note["id"], note["title"]).parts], ["wake", "wake+1"])
        from thirties_core.clock import thirty_ceil
        from thirties_core.day_times import wake_time
        plan = self.t.day_plan
        history = self.sched.state_db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
        sleep_end = wake_time(plan, history) or plan.day_start  # this morning's waking (the night before's Sleep)
        set_debug_time(sleep_end - timedelta(minutes=5))
        self.sched.state_db.set_sync_meta(f"routines_set_up:{plan.target_date.isoformat()}", "")
        self.t.assistant.routines.set_up_day(plan)
        start = {s.title: s.start_dt for s in self.t.day_plan.spans}
        self.assertEqual(start["Feed kitties"], thirty_ceil(sleep_end))
        self.assertEqual(start["Shower"], thirty_ceil(sleep_end) + timedelta(minutes=30))


class TestProposingTheDay(RoutineCase):

    def setUp(self):
        super().setUp()
        self.catalog.set_urgency("t_bank", "now")
        self.catalog.set_urgency("t_stamps", "soon", DAY + timedelta(days=2))
        self.services.tasks += [item("t_garage", "Clean the garage"), item("t_maybe", "Learn the banjo"),
                                item("t_done", "Return the library books")]
        self.catalog.set_urgency("t_garage", "soon")
        self.catalog.set_urgency("t_maybe", "someday")
        self.catalog.set_urgency("t_done", "now")
        self.sched.state_db.record_task_completed("t_done", DAY - timedelta(days=1))
        for _ in range(4):
            self.sched.state_db.record_task_deferred("t_garage")
        self.services.tasks = self.catalog.apply(self.services.tasks)
        self.sched.attach_history_to_tasks(self.services.tasks)

    def test_candidates_are_whats_pressing_not_the_backlog(self):
        cands = self.t.assistant.proposals.candidates()
        self.assertEqual([c["title"] for c in cands], ["Buy stamps", "Call the bank", "Clean the garage"])
        self.assertIn("put off 4 times: offer a 30-minute start", cands[2]["why"])
        text = self.t.execute("day_options", {})
        self.assertIn("- Buy stamps (due Saturday; Errands/Admin; about 30m) task_id=t_stamps", text)  # horizons
        self.assertIn("Free time:\n- 4:00 PM – ", text)  # after work, before bed

    def test_one_proposal_applied_on_yes(self):
        section = self.t.assistant.proposals.prompt_section()
        self.assertTrue(section.startswith("DAY OPTIONS"), section)
        out = self.t.execute("propose_day", {"items": [{"item": "t_stamps", "start": "4:30 PM"},
                                                       {"item": "Call the bank", "start": "5:30 PM"}],
                                             "why": "the stamps are due Saturday"})
        self.assertIn("- 4:30 PM – Buy stamps\n- 5:30 PM – Call the bank", out)
        self.assertFalse(self.spans(self.t, "Buy stamps"))  # nothing on the plan until yes
        self.assertTrue(self.t.assistant.proposals.pending())
        self.assertIn("DAY PROPOSAL waiting", self.t.assistant.proposals.prompt_section())
        out = self.t.execute("apply_day_proposal", {})
        self.assertEqual(out, "On the plan:\n- 4:30 PM – Buy stamps\n- 5:30 PM – Call the bank")
        self.assertEqual(self.spans(self.t, "Buy stamps")[0].task_id, "t_stamps")
        self.assertIn("- [ ] Buy stamps", self.body("n_today"))
        self.assertFalse(self.t.assistant.proposals.pending())
        self.assertEqual(self.t.assistant.proposals.prompt_section(), "")  # proposed once
        self.assertIn("already on the plan", self.t.execute("apply_day_proposal", {}))

    def test_only_into_the_free_time(self):
        out = self.t.execute("propose_day", {"items": [{"item": "Call the bank", "start": "10:00 AM"}]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Call the bank at 10:00 AM isn't in the free time", out)
        out = self.t.execute("propose_day", {"items": [{"item": "Call the bank", "start": "4:30 PM"},
                                                       {"item": "Buy stamps", "start": "4:30 PM"}]})
        self.assertIn("overlaps another proposed item", out)

    def test_pythons_own_proposal_when_no_model_picks(self):
        items = self.t.assistant.proposals.default_items()
        self.assertEqual([(i["title"], i["start"][11:16]) for i in items],
                         [("Buy stamps", "16:00"), ("Call the bank", "17:00"), ("Clean the garage", "18:00")])

    def test_routines_and_anchors_arent_candidates(self):
        self.services.tasks += [item(_checkbox_id("n_routine", "Shower"), "Shower", "n_routine")]
        self.services.tasks = self.catalog.apply(self.services.tasks)
        self.t.execute("set_anchor", {"title": "Call the bank", "time": "5:00 PM"})
        self.assertNotIn("Shower", [c["title"] for c in self.t.assistant.proposals.candidates()])
        self.assertNotIn("Call the bank", [c["title"] for c in self.t.assistant.proposals.candidates()])


class TestTheMorningMessage(RoutineCase):

    def setUp(self):
        super().setUp()
        from thirties_core.background import BackgroundService
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        self.catalog.set_urgency("t_bank", "now")
        self.services.tasks = self.catalog.apply(self.services.tasks)
        self.engine = MockInferenceEngine()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, self.services.tasks, pending, self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)), scheduler=self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc._manager, self.svc._day = self.m, DAY
        self.m.tools.execute("set_anchor", {"title": "Lunch", "time": "12:00 PM"})
        self.m.tools.execute("set_routine", {"routine": "Morning Routine", "parts": [
            {"start": "9:00 AM", "items": FIRST}, {"start": "3:00 PM", "items": SECOND}]})

    def test_at_wake_the_day_is_set_up_then_proposed(self):
        self.engine.responses = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "propose_day", "arguments": {"items": [{"item": "t_bank", "start": "4:00 PM"}]}}]},
            {"role": "assistant", "content": "For after work:\n- 4:00 PM – Call the bank\nIt's the pressing one. "
                                             "Want that?"}]
        self.svc.rollover(at("08:00"))
        kinds = [k for k, _ in self.sent]
        self.assertEqual(kinds, ["rollover", "proposal"])
        self.assertTrue(self.sent[0][1].startswith("☀️ Good morning! On today by themselves: Morning Routine at "
                                                   "9:00 AM and 3:00 PM; Lunch at 12:00 PM."), self.sent[0][1])
        self.assertIn("4:00 PM – Call the bank", self.sent[1][1])
        self.assertTrue(self.m.tools.assistant.proposals.pending())
        visible = [h["content"] for h in self.m.history()]
        self.assertFalse(any(v.startswith("[Morning") for v in visible))  # the instruction stays hidden
        self.assertTrue(any("Call the bank" in v for v in visible))
        self.svc.rollover(at("08:30"))
        self.assertEqual(len(self.sent), 2)  # once a day

    def test_without_a_model_python_proposes(self):
        self.m.brain_name = "slm"
        self.svc.rollover(at("08:00"))
        self.assertEqual(self.sent[-1], ("proposal", "For the free time today, how about:\n- 4:00 PM – Call the bank\n"
                                                     "Want these on the plan?"))

    def test_the_first_message_of_the_day_sets_it_up_before_the_model_looks(self):
        self.engine.responses = [{"role": "assistant", "content": "Morning!"}]
        self.m.send_user_message("good morning")
        prompt = self.engine.call_history[0][0]["content"]
        self.assertIn("Put on this morning by Python: Morning Routine at 9:00 AM and 3:00 PM; Lunch at 12:00 PM.",
                      prompt)
        self.assertIn("DAY OPTIONS", prompt)
        self.assertIn("ROUTINES AND DAILY ANCHORS", prompt)


class TestSync(unittest.TestCase):

    def test_routines_and_anchors_sync_last_write_wins(self):
        a, b = make_scheduler().state_db, make_scheduler().state_db
        book_a, book_b = RoutineBook(a), RoutineBook(b)
        book_a.save(Routine("n_routine", "Morning Routine", {"every": "days", "n": 2},
                            [{"items": ["Shower"], "at": "15:00", "fallback": ""}], "user"))
        from thirties_core.routines import Anchor
        book_a.save_anchor(Anchor("Lunch", "12:00"))
        for record in book_a.all_records():
            self.assertTrue(book_b.merge_record(record))
        self.assertEqual(book_b.get("n_routine").repeat, {"every": "days", "n": 2})
        self.assertEqual([x.title for x in book_b.anchors()], ["Lunch"])
        for record in book_a.all_records():
            self.assertFalse(book_b.merge_record(record))  # nothing newer
        from thirties_core.sync_engine import SyncEngine
        self.assertIn("routines", SyncEngine(config=make_scheduler().config, state_db=a,
                                             joplin_engine=MagicMock())._stores())


if __name__ == "__main__":
    unittest.main()
