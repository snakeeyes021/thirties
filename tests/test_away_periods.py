"""Time away from home: the fact is kept, the model's pauses hold for exactly that time, and nothing paused is
planned, rung or asked about while it lasts (away_periods.py).

Invented: a pottery studio owner goes to a glaze fair in another town from Friday through Monday (back around
7:40 PM Monday) and someone else opens the studio meanwhile. Their "Kiln room" routine and their "Studio supper"
can't happen away from home; sketching new mugs can happen anywhere.
"""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY, at
from tests.test_bell_every_thirty import Day as BellDay
from tests.test_routines import RoutineCase
from thirties_core import away_periods
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE

KILN = ["Check the kiln", "Wedge clay"]
FRI, SAT, MON, TUE = (DAY + timedelta(days=n) for n in (1, 2, 4, 5))  # DAY is a Thursday


class AwayCase(RoutineCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_kiln"] = {"id": "n_kiln", "body": "".join(f"- [ ] {s}\n" for s in KILN)}
        from tests.test_routines import TITLES
        TITLES["n_kiln"] = "Kiln room"
        self.addCleanup(TITLES.pop, "n_kiln", None)
        self.catalog.set_role("n_kiln", "routine", "Kiln room")
        self.t.execute("set_routine", {"routine": "Kiln room", "parts": [{"start": "9:10 AM", "items": KILN}]})
        self.t.execute("set_anchor", {"title": "Studio supper", "time": "6:40 PM"})
        self.t.execute("set_anchor", {"title": "Tea break", "time": "10:50 AM"})

    def go_away(self, t=None):
        out = (t or self.t).execute("set_away", {"start": "Friday", "end": "Monday", "back_at": "7:40 PM",
                                                 "place": "the glaze fair in Elmbrook",
                                                 "note": "Ines opens the studio"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out

    def morning(self, day):
        set_debug_time(at("07:30", (day - DAY).days))
        t = self.tools_for(day)
        return t, t.assistant.routines.set_up_current()

    def titles(self, t):
        return {s.title for s in t.day_plan.spans if s.status == SpanStatus.PLANNED}


class TestSetAway(AwayCase):

    def test_set_away_keeps_the_users_words_and_lists_what_to_decide_on(self):
        out = self.go_away()
        p = away_periods.periods(self.sched.state_db)[0]
        self.assertEqual((p["start"], p["end"], p["back_at"]), (FRI.isoformat(), MON.isoformat(), "7:40 PM"))
        self.assertEqual((p["place"], p["note"]), ("the glaze fair in Elmbrook", "Ines opens the studio"))
        for name in ("Kiln room", "Studio supper", "pause_while_away"):
            self.assertIn(name, out)
        facts = away_periods.facts(self.t.assistant)
        self.assertIn("will be away at the glaze fair in Elmbrook from tomorrow", facts)
        self.assertIn("Ines opens the studio", facts)
        self.assertIn("Kiln room", facts.split("Not paused:")[1])

    def test_pausing_needs_a_period_and_a_known_name(self):
        out = self.t.execute("pause_while_away", {"names": ["Kiln room"]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.go_away()
        out = self.t.execute("pause_while_away", {"names": ["Polish the moon"]})
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestMorningsAway(AwayCase):

    def setUp(self):
        super().setUp()
        self.go_away()
        out = self.t.execute("pause_while_away", {"names": ["Kiln room", "Studio supper"]})
        self.assertIn("Paused through Mon Oct 19: Kiln room, Studio supper", out)

    def test_inside_the_period_what_the_model_paused_stays_off_and_the_rest_plans(self):
        t, out = self.morning(FRI)
        self.assertNotIn("Kiln room", out)
        self.assertNotIn("Studio supper", out)
        self.assertIn("Tea break", out)  # not paused: it plans as usual
        self.assertFalse({"Check the kiln", "Wedge clay", "Studio supper"} & self.titles(t))
        section = t.assistant.routines.prompt_section()
        self.assertIn("Kiln room", section)
        self.assertIn("paused while the user is away", section)
        self.assertNotIn("Kiln room", {d["title"] for d in t.assistant.routines.day_routines()})
        facts = away_periods.facts(t.assistant)
        self.assertIn("The user is away at the glaze fair in Elmbrook until Monday, Oct 19; back 7:40 PM", facts)
        self.assertIn("Kiln room, Studio supper", facts.split("Not paused:")[0])

    def test_the_trip_is_said_on_the_first_morning_only(self):
        db = self.sched.state_db
        self.assertEqual(away_periods.morning_line(db, DAY), "")  # not away yet
        t, _ = self.morning(FRI)
        line = away_periods.morning_line(db, FRI)
        self.assertEqual(line, "You're away until Monday (the glaze fair in Elmbrook).")
        self.assertIn(line, away_periods.morning_facts(t.assistant, FRI))
        reply = away_periods.say_once(t.assistant, FRI, "☀️ Good morning!\n9:30 AM Lunch prep")
        self.assertTrue(reply.startswith(line), reply)  # left out: Python's line goes on top
        kept = "☀️ Good morning! You're away at the fair until Monday."
        self.assertEqual(away_periods.say_once(t.assistant, FRI, kept), kept)  # said already: unchanged
        t2, _ = self.morning(SAT)
        self.assertEqual(away_periods.morning_line(db, SAT), "")  # never again inside the period
        self.assertEqual(away_periods.say_once(t2.assistant, SAT, "☀️ Good morning!"), "☀️ Good morning!")
        self.assertNotIn("First morning of the time away", away_periods.morning_facts(t2.assistant, SAT))

    def test_the_first_morning_back_plans_the_routines_again(self):
        t, out = self.morning(TUE)
        self.assertIn("Kiln room at 9:00 AM", out)
        self.assertIn("Studio supper", out)
        self.assertEqual(away_periods.morning_line(self.sched.state_db, TUE), "")
        self.assertEqual(away_periods.facts(t.assistant), "")  # nothing to say: plans as usual

    def test_back_early_ends_the_pauses(self):
        t, _ = self.morning(SAT)
        self.assertNotIn("Check the kiln", self.titles(t))
        out = t.execute("end_away", {})
        self.assertIn("plans as usual again", out)
        self.assertEqual(away_periods.periods(self.sched.state_db)[0]["end"], FRI.isoformat())
        self.assertIn("Studio supper", self.titles(t))  # put back on today, where there's still time
        self.assertFalse(away_periods.paused_on(self.sched.state_db, "Kiln room", SAT))
        t2, out2 = self.morning(SAT + timedelta(days=1))
        self.assertIn("Kiln room", out2)

    def test_what_was_planned_already_comes_off_when_paused(self):
        t, _ = self.morning(FRI)
        t.execute("put_routine", {"routine": "Kiln room", "parts": [{"start": "4:10 PM", "items": KILN}]})
        self.assertIn("Check the kiln", self.titles(t))
        t.execute("pause_while_away", {"keep": ["Kiln room"]})
        self.assertFalse(away_periods.paused_on(self.sched.state_db, "Kiln room", FRI))
        out = t.execute("pause_while_away", {"names": ["Kiln room"]})
        self.assertIn("2 planned items taken off the plan", out)
        self.assertNotIn("Check the kiln", self.titles(t))

    def test_paused_items_are_left_out_of_the_days_proposal(self):
        cands = [{"title": "Glaze test tiles"}, {"title": "Sketch new mugs"}]
        db = self.sched.state_db
        self.t.execute("set_away", {"start": "Friday", "end": "Monday"})
        items = away_periods.periods(db)
        items[0]["paused"].append({"kind": "item", "name": "Glaze test tiles", "keys": ["glaze test tiles"]})
        away_periods._save(db, items)
        self.assertEqual(away_periods.not_paused(db, FRI, cands), [{"title": "Sketch new mugs"}])
        self.assertEqual(away_periods.not_paused(db, TUE, cands), cands)

    def test_cancelled_trip_is_gone(self):
        out = self.t.execute("end_away", {"cancel": True})
        self.assertIn("off", out)
        self.assertEqual(away_periods.periods(self.sched.state_db), [])


class TestTheMorningListIsPythons(AwayCase):
    """The morning reply's day list is Python's, placed by Python: a listing the model copied from earlier mornings,
    with a routine step paused for the time away, never reaches the user."""

    def setUp(self):
        super().setUp()
        from unittest.mock import MagicMock
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
        self.m.tools.execute("set_away", {"start": "today", "end": "Sunday", "place": "the glaze fair in Elmbrook"})
        self.m.tools.execute("pause_while_away", {"names": ["Kiln room"]})

    def test_a_copied_listing_with_a_paused_step_is_replaced_by_pythons_once(self):
        from tests.fixtures import mark_up
        self.engine.responses = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "propose_day", "arguments": {"items": [{"item": "t_bank", "start": "4:30 PM"}]}}]},
            {"role": "assistant", "content": "☀️ Good morning! You're away at the glaze fair until Sunday.\n"
                                             "9:00 AM Check the kiln, Wedge clay\n10:30 AM Tea break\n"
                                             "4:30 PM Call the bank\nClay orders first, so the bank waits for the afternoon."}]
        mark_up(self.sched.state_db, at("07:45"))  # the first message of the day
        self.svc.rollover(at("08:00"))
        self.assertEqual([k for k, _ in self.sent], ["rollover"])
        text = self.sent[0][1]
        self.assertTrue(text.startswith("☀️ Good morning! You're away at the glaze fair until Sunday."), text)
        self.assertIn("Clay orders first, so the bank waits for the afternoon.", text)  # the model's own sentences stay
        self.assertNotIn("Check the kiln", text)  # paused: not on today's plan, never listed
        self.assertNotIn("Wedge clay", text)
        from thirties_core.day_lists import planned_ahead_text
        view = planned_ahead_text(self.m.tools.assistant)
        self.assertEqual(text.count(view), 1)  # Python's list, exactly once, as Python lists it
        self.assertNotIn("Check the kiln", view)
        self.assertEqual(text.count("Tea break"), 1)
        self.assertEqual(text.count("Call the bank"), 1)
        self.assertRegex(view, r"\n1[01]:[03]0 AM Tea break")  # Python's row
        self.assertEqual(text.count("You're away"), 1)  # the trip said once


class TestAPausedStep(AwayCase):
    """One step of a routine part paused for the time away: the part goes on and is listed without it everywhere,
    and the first day back it's whole again."""

    def setUp(self):
        super().setUp()
        self.go_away()
        out = self.t.execute("pause_while_away", {"names": ["Wedge clay"]})
        self.assertIn("Paused through Mon Oct 19: Wedge clay", out)

    def test_the_part_goes_on_without_it_and_every_list_leaves_it_out(self):
        from thirties_core.day_lists import planned_ahead_text
        from thirties_core.messages import whole_day_lines
        t, out = self.morning(FRI)
        self.assertIn("Kiln room at 9:00 AM", out)
        self.assertIn("Check the kiln", self.titles(t))
        self.assertNotIn("Wedge clay", self.titles(t))
        listed = planned_ahead_text(t.assistant)  # the morning's list and its facts
        self.assertIn("Check the kiln", listed)
        self.assertNotIn("Wedge clay", listed)
        view = "\n".join(whole_day_lines(t.day_plan))  # the standing view's rows
        self.assertIn("Check the kiln", view)
        self.assertNotIn("Wedge clay", view)
        self.assertIn("Kiln room", {d["title"] for d in t.assistant.routines.day_routines()})  # the Agenda: still on

    def test_a_step_already_on_the_plan_comes_off_as_the_day_is_built(self):
        from tests.fixtures import at as at_day
        self.sched.state_db.upsert_span(EventSpan("seeded", "Wedge clay", at_day("09:20", 1), at_day("09:25", 1),
                                                  SpanCategory.TASK))  # put on before the pause took hold
        t, _ = self.morning(FRI)
        self.assertEqual({s.status for s in t.day_plan.spans if s.title == "Wedge clay"}, {SpanStatus.SKIPPED})

    def test_every_step_paused_the_part_goes(self):
        self.t.execute("pause_while_away", {"names": ["Check the kiln"]})
        t, out = self.morning(FRI)
        self.assertNotIn("Kiln room", out)
        self.assertFalse({"Check the kiln", "Wedge clay"} & self.titles(t))

    def test_the_first_day_back_the_part_is_whole(self):
        t, out = self.morning(TUE)
        self.assertIn("Kiln room at 9:00 AM", out)
        self.assertTrue({"Check the kiln", "Wedge clay"} <= self.titles(t))


class TestNoBellWhileAway(BellDay):

    def test_a_paused_routine_step_never_rings_and_the_rest_does(self):
        db = self.sched.state_db
        self.add(EventSpan("kc", "Check the kiln", at("17:40"), at("17:50"), SpanCategory.ACTIVITY, source="routine"))
        self.add(EventSpan("sk", "Sketch new mugs", at("18:10"), at("18:40"), SpanCategory.ACTIVITY))
        away_periods._save(db, [{"id": "p1", "start": DAY.isoformat(), "end": (DAY + timedelta(days=2)).isoformat(),
                                 "back_at": "", "place": "the fair", "note": "",
                                 "paused": [{"kind": "routine", "name": "Kiln room",
                                             "keys": ["check the kiln", "wedge clay"]}]}])
        self.ask("17:30", "18:35")
        self.assertEqual(self.bells(), ["🔔 6:00 PM: Sketch new mugs"])  # never the paused routine's step
        self.assertFalse(away_periods.paused_on(db, "Check the kiln", date(2026, 10, 18)))  # the period is over


    def test_a_paused_step_is_left_out_of_its_parts_bell(self):
        db = self.sched.state_db
        self.add(EventSpan("kc", "Check the kiln", at("17:40"), at("17:45"), SpanCategory.ACTIVITY, source="routine"))
        self.add(EventSpan("wc", "Wedge clay", at("17:45"), at("17:50"), SpanCategory.ACTIVITY, source="routine"))
        away_periods._save(db, [{"id": "p2", "start": DAY.isoformat(), "end": DAY.isoformat(), "back_at": "",
                                 "place": "the fair", "note": "",
                                 "paused": [{"kind": "step", "name": "Wedge clay", "keys": ["wedge clay"]}]}])
        self.ask("17:30", "17:35")
        self.assertEqual(self.bells(), ["🔔 5:30 PM: Check the kiln"])


if __name__ == "__main__":
    unittest.main()
