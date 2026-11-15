"""Whose is this event (others_events.py): other people's calendar events, worked out in Python.
Invented calendars and people throughout: Jamie (a partner), Robin (a housemate), a Family calendar."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from thirties_core import others_events
from thirties_core.calendar_engine import CalendarEvent, events_to_spans, series_key
from thirties_core.plan_tools import PlanTools


def ev(eid, title, start, end, calendar="Jamie", day_offset=0, mine=False, status="accepted", invited=False,
       series=None):
    begin = at(start, day_offset)
    sid = series or f"{eid}@google.com"
    return CalendarEvent(id=f"{sid}@{int(begin.timestamp())}", summary=title, start_dt=begin,
                         end_dt=at(end, day_offset), calendar_id=f"uid-{calendar.lower()}", calendar_name=calendar,
                         is_primary=mine, is_ambiguous=not mine and not invited, response_status=status,
                         invited=invited, series_id=sid)


class Designed(unittest.TestCase):
    def day(self, events, modes=None):
        self.sched = make_scheduler(events)
        if modes:
            self.sched.config.calendar.modes = modes
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched)
        return self.t

    def span(self, title):
        return next(s for s in self.t.day_plan.spans_in_view() if s.title == title)


class WhoseTest(Designed):
    def test_a_calendar_is_named_the_way_the_user_would_say_it(self):
        self.assertEqual(others_events.whose("Jamie"), "Jamie's calendar")
        self.assertEqual(others_events.whose("Family"), "the Family calendar")
        sched = make_scheduler([])
        others_events.set_owner(sched.state_db, "Jamie", "my partner")
        self.assertEqual(others_events.whose("jamie", sched.state_db.get_event_decisions()), "your partner's calendar")
        self.assertEqual(others_events.to_second_person("that's my partner's calendar"), "your partner")
        self.assertEqual(others_events.short_whose("Jamie's calendar"), "Jamie's")

    def test_saying_whose_a_calendar_is_in_chat(self):
        t = self.day([ev("pottery", "Pottery class", "18:00", "21:00")])
        out = t.execute("set_calendar_mode", {"calendar": "Jamie", "whose": "my partner"})
        self.assertNotIn("NOT_DONE", out)
        self.assertEqual(self.span("Pottery class").whose, "your partner's calendar")  # every mention from now on
        self.assertIn("Pottery class, 6:00 PM – 9:00 PM, your partner's calendar", t.schedule_text())


class OwnershipTest(Designed):
    def test_someone_elses_event_stays_off_the_plan_and_takes_no_free_time(self):
        t = self.day([ev("pottery", "Pottery class", "18:00", "21:00")])
        s = self.span("Pottery class")
        self.assertTrue(s.shown_only)
        self.assertFalse(s.is_firm)
        self.assertEqual(s.whose, "Jamie's calendar")
        self.assertIn("4:00 PM – 11:00 PM", t.schedule_text().split("Free time:")[1])  # never counted against it
        self.assertNotIn("Pottery class", t.schedule_text().split("Not yours today:")[0])

    def test_one_event_on_both_calendars_is_one_item_shared(self):
        dinner_mine = ev("dinner", "Dinner with the Okafors", "19:00", "21:00", calendar="Me", mine=True)
        dinner_hers = ev("dinner", "Dinner with the Okafors", "19:00", "21:00")
        t = self.day([dinner_mine, dinner_hers])
        spans = [s for s in t.day_plan.spans_in_view() if s.title.startswith("Dinner")]
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].is_firm and spans[0].shared)
        self.assertIn("shared, also on Jamie's calendar", t.schedule_text())

    def test_the_same_title_and_times_on_separate_calendars_is_one_item_too(self):
        a = ev("x1", "Family dinner", "18:00", "19:00", calendar="Jamie")
        b = ev("x2", "family dinner", "18:00", "19:00", calendar="Robin")
        spans, _ = events_to_spans([a, b])
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].shared and spans[0].is_firm)  # a family dinner: everyone's, the user's too
        self.assertEqual(spans[0].whose, "Jamie's calendar and Robin's calendar")

    def test_a_household_calendar_is_everyones(self):
        t = self.day([ev("movie", "Quiz night", "20:00", "22:00", calendar="Family")])
        s = self.span("Quiz night")
        self.assertTrue(s.is_firm and s.shared and not s.shown_only)
        self.assertEqual(others_events.marker(s), "shared, also on the Family calendar")

    def test_a_household_calendar_the_user_set_to_show_only_stays_off(self):
        self.day([ev("movie", "Quiz night", "20:00", "22:00", calendar="Family")], modes={"family": "show"})
        self.assertTrue(self.span("Quiz night").shown_only)

    def test_a_calendar_the_user_calls_shared(self):
        self.day([ev("book", "Book club", "19:00", "21:00", calendar="Robin")], modes={"robin": "shared"})
        s = self.span("Book club")
        self.assertTrue(s.is_firm and s.shared)

    def test_an_invite_they_accepted_on_someone_elses_calendar_is_theirs(self):
        self.day([ev("party", "Ben's birthday party", "15:00", "17:00", invited=True)])
        s = self.span("Ben's birthday party")
        self.assertTrue(s.is_firm and s.shared and not s.tentative)

    def test_an_open_invite_on_someone_elses_calendar_is_asked_naming_whose(self):
        from thirties_core.not_yours import invite_lines
        t = self.day([ev("gala", "School gala", "18:00", "20:00", status="needsAction", invited=True)])
        s = self.span("School gala")
        self.assertTrue(s.tentative and not s.is_firm)
        self.assertIn("on Jamie's calendar", invite_lines(t.day_plan, t.ambiguous_events)[0])

    def test_a_title_for_everyone_is_shared(self):
        self.day([ev("bbq", "BBQ, all of us", "17:00", "19:00", calendar="Robin")])
        self.assertTrue(self.span("BBQ, all of us").shared)


class LearningTest(Designed):
    def week(self):
        return [ev("pottery", "Pottery class", "18:00", "21:00"), ev("pottery", "Pottery class", "18:00", "21:00", day_offset=7)]

    def test_not_mine_on_one_covers_the_series(self):
        t = self.day(self.week())
        t.execute("resolve_calendar_event", {"event": self.span("Pottery class").id, "attend": False})
        self.assertNotIn("Pottery class", [s.title for s in t.day_plan.spans_in_view()])
        plan, _ = self.sched.build_day_plan(DAY + timedelta(days=7))
        self.assertNotIn("Pottery class", [s.title for s in plan.spans_in_view()])  # every time, from now on

    def test_thats_mine_on_one_covers_the_series_too(self):
        t = self.day(self.week())
        out = t.execute("resolve_calendar_event", {"event": self.span("Pottery class").id, "attend": True})
        self.assertIn("every time", out)
        s = self.span("Pottery class")
        self.assertTrue(s.is_firm and not s.shown_only and s.shared)  # on the plan now, also Jamie's
        plan, _ = self.sched.build_day_plan(DAY + timedelta(days=7))
        nxt = next(x for x in plan.spans_in_view() if x.title == "Pottery class")
        self.assertTrue(nxt.is_firm and nxt.shared)

    def test_just_this_once(self):
        t = self.day(self.week())
        t.execute("resolve_calendar_event", {"event": self.span("Pottery class").id, "attend": True, "series": False})
        plan, _ = self.sched.build_day_plan(DAY + timedelta(days=7))
        self.assertTrue(next(x for x in plan.spans_in_view() if x.title == "Pottery class").shown_only)


class AskOnceTest(Designed):
    """The first time a series from someone else's calendar is on the day: asked once, naming whose; remembered."""

    def test_asks_once_per_new_series_naming_whose(self):
        t = self.day([ev("pickup", "Pick up Maya", "17:30", "18:00")])
        self.assertTrue(self.span("Pick up Maya").ask)
        out = others_events.after_reply(t, "Here's your day.")
        self.assertIn("Pick up Maya, 5:30 PM – 6:00 PM is on Jamie's calendar. Is that yours too?", out)
        self.assertEqual(others_events.after_reply(t, "Anything else?"), "Anything else?")  # once
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertFalse(next(s for s in plan.spans_in_view() if s.title == "Pick up Maya").ask)  # remembered

    def test_the_models_own_question_counts(self):
        t = self.day([ev("pickup", "Pick up Maya", "17:30", "18:00")])
        said = "Is the Maya pickup at 5:30 yours? It's on Jamie's calendar."
        self.assertEqual(others_events.after_reply(t, said), said)
        self.assertFalse(self.span("Pick up Maya").ask)

    def test_a_decided_series_is_never_asked(self):
        first = ev("choir", "Choir practice", "19:00", "22:00")
        sched = make_scheduler([first])
        sched.state_db.set_event_decision(series_key(first), "not_mine")
        plan, _ = sched.build_day_plan(DAY)
        self.assertEqual([s for s in plan.spans_in_view() if s.title == "Choir practice"], [])

    def test_shared_and_invites_are_never_the_once_question(self):
        self.day([ev("movie", "Quiz night", "20:00", "22:00", calendar="Family"),
                  ev("gala", "School gala", "18:00", "20:00", status="needsAction", invited=True)])
        self.assertFalse(self.span("Quiz night").ask)
        self.assertFalse(self.span("School gala").ask)

    def test_a_calendar_that_proves_not_the_users_stops_being_asked(self):
        events = [ev(f"s{i}", f"Thing {i}", f"{10 + i}:00", f"{10 + i}:30") for i in range(4)]
        t = self.day(events)
        others_events.after_reply(t, "ok")
        for i in range(3):
            t.execute("resolve_calendar_event", {"event": self.span(f"Thing {i}").id, "attend": False})
        self.assertTrue(others_events.calendar_quiet("jamie", self.sched.state_db.get_event_decisions()))
        out = others_events.after_reply(t, "ok")
        self.assertIn("I'll stop asking about Jamie's calendar", out)
        self.assertEqual(others_events.after_reply(t, "ok"), "ok")  # said once
        new = ev("s9", "Thing 9", "20:00", "20:30")
        self.sched.calendar_engine.events.append(new)
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertFalse(next(s for s in plan.spans_in_view() if s.title == "Thing 9").ask)

    def test_one_mine_keeps_a_calendar_asking(self):
        events = [ev(f"s{i}", f"Thing {i}", f"{10 + i}:00", f"{10 + i}:30") for i in range(4)]
        t = self.day(events)
        others_events.after_reply(t, "ok")
        t.execute("resolve_calendar_event", {"event": self.span("Thing 0").id, "attend": True})
        for i in (1, 2, 3):
            t.execute("resolve_calendar_event", {"event": self.span(f"Thing {i}").id, "attend": False})
        self.assertFalse(others_events.calendar_quiet("jamie", self.sched.state_db.get_event_decisions()))


class NoJoplinTest(unittest.TestCase):
    def test_whose_and_learning_never_write_to_joplin(self):
        """The data guard: everything here lives in the event decisions; no Joplin write is ever made."""
        import inspect
        src = inspect.getsource(others_events)
        for word in ("joplin", "outbox", "writeback"):
            self.assertNotIn(word, src.lower())


if __name__ == "__main__":
    unittest.main()
