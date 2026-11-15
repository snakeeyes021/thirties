"""Muting check-ins quiets what Thirties says by itself, never where things stand: a schedule the user asks for
still shows what's not marked yet. Invented items; DAY is Thu Oct 15."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, line_with, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.clock import format_clock
from thirties_core.background import BackgroundService
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory


class TestMutedStillShown(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("18:50"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        for sid, title, start, end in (("oil", "Oil the hinges", "16:00", "16:30"),
                                       ("seeds", "Sort the seed packets", "16:30", "17:00"),
                                       ("dinner", "Dinner", "19:00", "20:00")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(),
                                     active_notes=[])
        self.assertIn("Check-ins paused until", self.m.execute_tool("mute_checkins", {}))  # mute everything

    def test_the_schedule_still_lists_whats_not_marked(self):
        out = self.m.execute_tool("show_schedule", {})
        self.assertIn("Not marked yet:", out)
        self.assertIn("Oil the hinges", out)
        self.assertIn("Sort the seed packets", out)

    def test_the_prompt_knows_them_but_not_to_ask(self):
        from thirties_core.messages import now_section
        text = now_section(self.m.tools)
        line_with(text, "Not marked yet", "Oil the hinges", "Sort the seed packets")
        line_with(text, "muted", "Oil the hinges", "Sort the seed packets")

    def test_no_notice_lists_them(self):
        svc = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=MagicMock(),
                                inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today, svc._manager, svc._day = (lambda: DAY), self.m, DAY
        self.assertEqual(svc._with_missed("🔔 7:00 PM: Dinner", at("18:55")), "🔔 7:00 PM: Dinner")


if __name__ == "__main__":
    unittest.main()


class TestMuteNeedsTheWords(unittest.TestCase):
    """Pausing every check-in takes the user's own words for it; a mute can always be undone."""

    def setUp(self):
        set_debug_time(at("16:09"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.state_db.upsert_span(EventSpan("oil", "Oil the hinges", at("17:00"), at("17:30"),
                                                  SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(),
                                     active_notes=[])
        self.t = self.m.tools

    def said(self, words, tool, args=None):
        self.t.user_message = words
        try:
            return self.t.execute(tool, args or {})
        finally:
            self.t.user_message = None

    def paused(self):
        return self.t.assistant.checkins_paused()

    def test_a_bare_stop_or_a_complaint_never_pauses_everything(self):
        for words in ("Whoa, enough, haha", "stop", "ugh, not that one again"):
            out = self.said(words, "mute_checkins")
            self.assertTrue(out.startswith("NOT_DONE"), (words, out))
            self.assertEqual(self.paused(), "")

    def test_asking_for_it_pauses_them_and_says_how_to_undo(self):
        for words in ("stop the check-ins for today", "quiet until tomorrow please", "pause reminders"):
            out = self.said(words, "mute_checkins")
            self.assertIn("Check-ins paused until", out, words)
            self.assertIn('say "resume check-ins" to turn them back on', out)
            self.assertTrue(self.paused().startswith("Check-ins paused until"))
            self.said("resume check-ins", "unmute_checkins")

    def test_the_pause_is_shown_in_the_users_own_time(self):
        out = self.said("stop the check-ins for today", "mute_checkins")  # until the end of the day, local time
        shown = self.paused()
        self.assertTrue(shown.startswith("Check-ins paused until "), shown)
        self.assertIn(shown[len("Check-ins paused until "):], out)  # the same time the reply gave, never UTC's
        end = self.t.day_plan.day_end
        self.assertIn(format_clock(end), shown)

    def test_one_item_still_mutes_on_a_plain_word(self):
        out = self.said("no need to ask about the hinges", "mute_checkins", {"event": "oil"})
        self.assertIn("I won't ask about Oil the hinges again", out)
        self.assertEqual(self.paused(), "")  # just that one
        self.assertIn("I'll ask about Oil the hinges again", self.said("ask about it again", "unmute_checkins",
                                                                       {"event": "oil"}))

    def test_a_wrong_mute_is_undone(self):
        self.said("stop the check-ins for today", "mute_checkins")
        self.assertEqual(self.said("I didn't mean mute, turn them back on", "unmute_checkins"),
                         "Check-ins are back on.")
        self.assertEqual(self.paused(), "")
        self.assertFalse(self.t.services.mutes.is_muted("oil", at("16:20")))

    def test_an_unmute_travels_to_the_other_computer(self):
        from thirties_core.memory import CheckinMutes
        self.said("stop the check-ins for today", "mute_checkins")
        mine = CheckinMutes(self.sched.state_db)
        other = CheckinMutes(make_scheduler().state_db)
        for r in mine.all_records():
            other.merge_record(r)
        self.assertTrue(other.is_muted("oil", at("16:20")))
        self.said("resume check-ins", "unmute_checkins")
        for r in mine.all_records():
            other.merge_record(r)
        self.assertFalse(other.is_muted("oil", at("16:20")))
