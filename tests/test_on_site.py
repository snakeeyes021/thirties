"""On-site days: nothing else fits during work, and what Thirties placed inside it moves out by itself, with one
line each and no question (on_site.py, placement.out_of_work). Fixtures and wordings are invented."""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import DAY, at
from tests.test_today import TodayTestCase
from thirties_core import on_site
from thirties_core.astronomy import set_debug_time
from thirties_core.on_site import OnSiteDays, said
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE

ROUTINE = {"id": "n_routine", "title": "Kiln check", "body": "- [ ] Vent the kiln\n- [ ] Log the firing\n"}


class WordsTest(unittest.TestCase):
    def test_on_site_and_back(self):
        for m in ("shift runs 10 till 6, at the office", "an office day for me", "I'll be at the office",
                  "heading into the office shortly", "an on-site shift"):
            self.assertIs(said(m), True, m)
        for m in ("change of plan, working remotely", "a remote shift", "wfh", "not on site after all"):
            self.assertIs(said(m), False, m)
        for m in ("met a neighbour face to face", "shift runs 10 till 6", None, ""):
            self.assertIsNone(said(m), m)


class InPersonBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("07:30"))
        self.a = self.t.assistant
        p = patch.object(type(self.a), "_routines", return_value=[ROUTINE])
        p.start()
        self.addCleanup(p.stop)

    def say(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def put(self, title, start, minutes, source="user", plan=None):
        """Placed by Thirties (no time the user stated)."""
        from thirties_core.plan_tools import new_span_id
        plan = plan or self.t.day_plan
        s = EventSpan(new_span_id(), title, start, start + timedelta(minutes=minutes), SpanCategory.ACTIVITY,
                      source=source)
        plan.spans.append(s)
        plan.spans.sort(key=lambda x: x.start_dt)
        self.t._persist(s)
        return s

    def work(self, plan=None):
        return next(s for s in (plan or self.t.day_plan).spans if s.category == SpanCategory.WORK)

    def inside_work(self):
        w = self.work()
        return [s.title for s in self.t.day_plan.spans if s.status == SpanStatus.PLANNED and not s.is_envelope
                and s.start_dt < w.end_dt and w.start_dt < s.end_dt]


class WorkHoursChangeTest(InPersonBase):
    def test_new_hours_on_site_move_what_thirties_placed_out_without_asking(self):
        self.t.by_hand = True
        self.t.execute("update_event", {"event": "Work", "start": "1:15 PM", "end": "6:00 PM"})
        self.t.by_hand = False
        chisels = self.put("Sharpen the chisels", at("11:30"), 60)
        vent = self.put("Vent the kiln", at("12:30"), 30, source="routine")
        firing = self.put("Log the firing", at("13:00"), 30, source="routine")
        lunch = self.put("Lunch", at("14:00"), 30)
        self.say("ring the plumber at 11:45", "add_event", {"title": "Ring the plumber", "start": "11:45 AM",
                                                          "duration": "15 min"})
        out = self.say("they put me on the late shift, at the office", "update_event",
                       {"event": "Work", "start": "11:00 AM", "end": "6:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertTrue(OnSiteDays(self.sched.state_db).get(DAY))
        self.assertEqual((chisels.start_dt, chisels.end_dt), (at("10:00"), at("11:00")))  # nearest free time, before
        self.assertEqual((vent.start_dt, firing.start_dt), (at("09:00"), at("09:30")))  # together, in order
        self.assertEqual(lunch.start_dt, at("14:00"))  # eaten at work
        self.assertIn("Sharpen the chisels: now 10:00 AM, before work.", out)
        self.assertIn("Vent the kiln, Log the firing: now 9:00 AM, before work.", out)
        self.assertIn("Ring the plumber stays at 11:45 AM, during work: you gave it that time.", out)
        self.assertNotIn("?", out.split("moved out:")[1])  # said, never asked
        self.assertEqual(sorted(self.inside_work()), ["Lunch", "Ring the plumber"])
        # stored: a later look at the day finds them where they went
        stored = {s.id: s for s in self.sched.state_db.get_spans_between(at("07:00"), at("07:00", 1))}
        self.assertEqual(stored[chisels.id].start_dt, at("10:00"))
        self.assertEqual(stored[firing.id].start_dt, at("09:30"))

    def test_new_hours_not_on_site_leave_items_alongside_work(self):
        gate = self.put("Sharpen the chisels", at("12:00"), 60)
        out = self.say("they put me on the late shift", "update_event", {"event": "Work", "start": "11:00 AM",
                                                                     "end": "6:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(gate.start_dt, at("12:00"))
        self.assertNotIn("moved out", out)

    def test_hours_changed_later_on_an_on_site_day_move_what_they_now_cover(self):
        OnSiteDays(self.sched.state_db).set(DAY, True)
        self.t.by_hand = True
        self.t.execute("update_event", {"event": "Work", "start": "1:15 PM", "end": "6:00 PM"})
        self.t.by_hand = False
        skis = self.put("Wax the skis", at("11:30"), 30)
        out = self.say("they moved my shift start up", "update_event", {"event": "Work", "start": "11:00 AM"})
        self.assertIn("Wax the skis: now 10:30 AM, before work.", out)
        self.assertEqual(skis.start_dt, at("10:30"))


class SaidAtTurnStartTest(InPersonBase):
    def test_the_users_words_alone_keep_the_fact_and_move_items(self):
        mail = self.put("Wax the skis", at("10:00"), 30)  # inside work (8:30 – 4:00)
        note = on_site.at_turn_start(self.t, "fyi I'll be at the office all day")
        self.assertTrue(OnSiteDays(self.sched.state_db).get(DAY))
        self.assertEqual(mail.start_dt, at("08:00"))
        self.assertIn("Wax the skis: now 8:00 AM, before work.", note)
        self.assertIn("don't ask", note)
        self.assertEqual(on_site.at_turn_start(self.t, "the glaze came out glossy"), "")

    def test_remote_takes_it_back_and_items_may_share_work_again(self):
        on_site.at_turn_start(self.t, "an office day for me")
        out = self.say("add rake the gravel at 10", "add_event", {"title": "Rake the gravel", "start": "10:00 AM",
                                                                  "duration": "30 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("on-site day", out)
        on_site.at_turn_start(self.t, "change of plan, working remotely")
        self.assertIs(OnSiteDays(self.sched.state_db).get(DAY), False)
        out = self.say("add rake the gravel at 10", "add_event", {"title": "Rake the gravel", "start": "10:00 AM",
                                                                  "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_tool_needs_the_users_words_about_work(self):
        out = self.say("the glaze came out glossy", "work_on_site", {"on_site": True})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIsNone(OnSiteDays(self.sched.state_db).get(DAY))
        self.put("Wax the skis", at("10:00"), 30)
        out = self.say("my job needs me in the building", "work_on_site", {"on_site": True})
        self.assertIn("Wax the skis: now 8:00 AM, before work.", out)


class NoOverlapTest(InPersonBase):
    def setUp(self):
        super().setUp()
        OnSiteDays(self.sched.state_db).set(DAY, True)

    def test_nothing_new_goes_inside_work(self):
        out = self.say("put the gravel at 11", "add_event", {"title": "Rake the gravel", "start": "11:00 AM",
                                                            "duration": "30 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = self.say("I'll rake the gravel on my break at 11", "add_event",
                       {"title": "Rake the gravel", "start": "11:00 AM", "duration": "30 min", "alongside": True})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_meals_and_the_apps_own_controls_still_may(self):
        out = self.say("a quick lunch at 1", "add_event", {"title": "Lunch", "start": "1:00 PM", "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.t.by_hand = True
        out = self.t.execute("add_event", {"title": "Rake the gravel", "start": "11:00 AM", "duration": "30 min"})
        self.t.by_hand = False
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_items_added_for_today_land_outside_work(self):
        self.say("sharpen the chisels today", "add_to_today", {"items": ["Sharpen the chisels"]})
        self.assertEqual(self.inside_work(), [])


class NoRoomTodayTest(InPersonBase):
    def setUp(self):
        super().setUp()
        # the evening is short (bedtime at 6:00 PM) and after work is taken by something fixed
        p = patch("thirties_core.if_time._waking_end", side_effect=lambda plan: plan.day_start + timedelta(hours=11))
        p.start()
        self.addCleanup(p.stop)
        self.t.execute("add_event", {"title": "Pottery class", "start": "4:00 PM", "duration": "2 hours",
                                     "fixed": True})

    def test_a_one_off_goes_to_tomorrow(self):
        trim = self.put("Patch the canoe", at("10:00"), 120)
        out = self.say("an office day for me", "work_on_site", {"on_site": True})
        self.assertIn("Patch the canoe: no free time outside work today, so tomorrow at", out)
        self.assertNotIn(trim.id, [s.id for s in self.t.day_plan.spans if s.status == SpanStatus.PLANNED])
        tomorrow = self.t.plan_for(DAY + timedelta(days=1))
        moved = [s for s in tomorrow.spans if s.title == "Patch the canoe"]
        self.assertEqual(len(moved), 1)
        w = self.work(tomorrow)
        self.assertFalse(moved[0].start_dt < w.end_dt and w.start_dt < moved[0].end_dt)

    def test_a_daily_thing_already_on_tomorrow_doesnt_happen_today(self):
        set_debug_time(at("08:30"))
        fish = self.put("Turn the compost", at("10:00"), 30)
        self.put("Turn the compost", at("10:00", 1), 30, plan=self.t.plan_for(DAY + timedelta(days=1)))
        out = self.say("an office day for me", "work_on_site", {"on_site": True})
        self.assertIn("Turn the compost: no free time outside work today, so not today; it's on tomorrow's plan", out)
        self.assertEqual(fish.status, SpanStatus.SKIPPED)


class InTheConversationTest(unittest.TestCase):
    """The user's message alone does it: before the model reads it, Python keeps the fact and moves what's inside
    work, and the model is handed the lines (a mock model: no real calls)."""

    def setUp(self):
        from tests.fixtures import make_scheduler
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        set_debug_time(at("07:30"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine, active_notes=[])

    def test_said_in_a_message_it_moves_before_the_model_answers(self):
        from tests.test_conversation import reply
        from thirties_core.plan_tools import new_span_id
        plan = self.m.tools.day_plan
        mail = EventSpan(new_span_id(), "Wax the skis", at("10:00"), at("10:30"), SpanCategory.ACTIVITY)
        plan.spans.append(mail)
        self.m.tools._persist(mail)
        self.engine.responses = [reply("Got it.")]
        self.m.send_user_message("heading into the office shortly")
        self.assertTrue(OnSiteDays(self.sched.state_db).get(DAY))
        moved = next(s for s in self.m.tools.day_plan.spans if s.id == mail.id)
        self.assertEqual(moved.start_dt, at("08:00"))
        told = [m["content"] for m in self.engine.call_history[0] if m.get("role") == "user"]
        self.assertTrue(any("Wax the skis: now 8:00 AM, before work." in t for t in told), told)
        self.assertNotIn("Wax the skis: now", " ".join(h["content"] for h in self.m.history()))  # hidden


if __name__ == "__main__":
    unittest.main()
