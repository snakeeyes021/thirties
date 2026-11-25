"""One standard view of where things stand, the same every time, placed under the reply by Python: "Now:", "The
rest of today:", "Not marked yet:" (titles only, never a time). After any change, whatever words the user's message
used; after show_schedule, once, with nothing the model laid out itself beside it. A tick in the app on an item whose
time is over keeps the item in its planned block. Invented bike co-op items; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM,
Sleep from 11 PM)."""

import re
import unittest

from tests.fixtures import DAY, at, make_scheduler, morning_sent
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory

_TIME = re.compile(r"\d{1,2}:\d{2}\s*[AP]M")


class StandingCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("17:40"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        morning_sent(self.sched.state_db)
        for sid, title, start, end in (("rims", "True the wheel rims", "16:10", "16:40"),
                                       ("pads", "Restock brake pads", "16:40", "17:10"),
                                       ("spokes", "Sort the spoke bin", "17:40", "18:10"),
                                       ("rota", "Print the volunteer rota", "19:10", "19:40"),
                                       ("wall", "Lock up the tool wall", "21:40", "22:10")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        self.manager = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine,
                                           active_notes=[])

    def turn(self, *replies: str, said: str = "ok") -> str:
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.manager.send_user_message(said)

    @staticmethod
    def section(out: str, heading: str) -> list:
        """The lines of one section of the view (its heading, then everything up to the next blank line)."""
        lines = out.splitlines()
        at_ = lines.index(heading)
        body = []
        for line in lines[at_ + 1:]:
            if not line.strip():
                break
            body.append(line)
        return body

    def assertOneView(self, out: str) -> None:
        self.assertEqual(sum(line.startswith("Now:") for line in out.splitlines()), 1, out)
        self.assertEqual(out.count("The rest of today:"), 1, out)
        self.assertEqual(out.count("Not marked yet:"), 1, out)
        self.assertNotIn("You're in the", out)
        unmarked = self.section(out, "Not marked yet:")
        self.assertEqual(unmarked, ["True the wheel rims", "Restock brake pads"], out)  # titles only, never a time
        self.assertFalse(any(_TIME.search(line) for line in unmarked), out)


class TestAfterAChange(StandingCase):

    def test_the_word_schedule_in_the_message_doesnt_keep_the_view_away(self):
        out = self.turn('CALL add_event {"title": "Pump check", "start": "6:40 PM", "duration": "30 minutes"}',
                        "Pump check is on at 6:40.", said="schedule a pump check for 6:40 please")
        self.assertTrue(out.startswith("Pump check is on at 6:40."), out)
        self.assertOneView(out)
        self.assertIn("Now: Sort the spoke bin", out)
        self.assertTrue(any("Pump check" in line for line in self.section(out, "The rest of today:")), out)

    def test_a_list_the_model_wrote_itself_goes(self):
        out = self.turn('CALL add_event {"title": "Pump check", "start": "6:40 PM", "duration": "30 minutes"}',
                        "Done.\n\nNow: Sort the spoke bin\n\nNot marked yet:\n4:00 PM – True the wheel rims\n"
                        "4:30 PM – Restock brake pads", said="pump check at 6:40")
        self.assertOneView(out)


class TestShowSchedule(StandingCase):

    def test_the_result_tells_the_model_python_places_it(self):
        from thirties_core.whats_next import PLACED
        result = self.manager.tools.execute("show_schedule", {})
        self.assertTrue(result.startswith(PLACED), result)
        self.assertNotIn("You're in the", result)

    def test_placed_under_the_reply_once_even_when_the_model_copied_it(self):
        view = self.manager.tools.execute("show_schedule", {}).split("\n\n", 1)[1]
        self.manager.tools.shown_schedule = None
        out = self.turn("CALL show_schedule {}", "Here's where things stand:\n\n" + view, said="what's left?")
        self.assertOneView(out)
        self.assertTrue(out.rstrip().endswith("Restock brake pads"), out)  # the view is the last thing

    def test_the_model_writing_a_layout_of_its_own(self):
        out = self.turn("CALL show_schedule {}",
                        "You're in the 5:30 PM block: Sort the spoke bin.\nNow: Sort the spoke bin\n"
                        "7:00 PM – Print the volunteer rota\n9:30 PM – Lock up the tool wall\n\nNow: Sort the spoke bin",
                        said="what's next?")
        self.assertOneView(out)

    def test_earlier_today_only_with_whole_day(self):
        out = self.turn("CALL show_schedule {}", "Here you go.", said="what's left?")
        self.assertNotIn("Earlier today:", out)
        out = self.turn('CALL show_schedule {"whole_day": true}', "Here's the day.", said="and before now?")
        earlier = self.section(out, "Earlier today:")
        self.assertTrue(any("True the wheel rims" in line for line in earlier), out)
        self.assertFalse(any("Print the volunteer rota" in line for line in earlier), out)
        self.assertOneView(out)


class TestUntimed(StandingCase):

    def test_if_theres_time_after_not_marked_yet_and_no_pencilled_section(self):
        from unittest.mock import MagicMock, patch
        self.manager.tools.assistant = self.manager.tools.assistant or MagicMock()
        with patch("thirties_core.day_lists.pencilled_in", return_value=["Oil the chain tool"]), \
                patch("thirties_core.if_time.open_items", return_value=["Sweep the bench"]):
            out = self.turn("CALL show_schedule {}",
                            "Here you go.\n\nOn today's list, no time yet:\nOil the chain tool", said="what's left?")
        self.assertOneView(out)
        self.assertNotIn("no time yet", out)
        self.assertNotIn("Oil the chain tool", out)
        self.assertEqual(self.section(out, "If there's time:"), ["Sweep the bench"], out)
        self.assertLess(out.index("Not marked yet:"), out.index("If there's time:"))
        self.assertTrue(out.rstrip().endswith("If there's time:\nSweep the bench"), out)  # last

    def test_no_if_theres_time_part_when_its_empty(self):
        out = self.turn("CALL show_schedule {}", "Here you go.", said="what's left?")
        self.assertNotIn("If there's time", out)


class TestNowIsAlwaysSaid(StandingCase):

    def test_between_items_now_says_free_until_the_next_one(self):
        set_debug_time(at("18:40"))
        out = self.turn("CALL show_schedule {}", "Here you go.", said="what's left?")
        self.assertIn("Now: free until 7:00 PM", out)
        self.assertFalse(any(line.startswith("Free until") for line in out.splitlines()), out)

    def test_before_the_days_first_block(self):
        set_debug_time(at("07:40"))
        out = self.turn("CALL show_schedule {}", "Morning.", said="what's on?")
        self.assertIn("Now: free until 8:30 AM", out)  # Work starts the day


class TestHandTick(StandingCase):

    def test_a_past_item_ticked_in_the_app_keeps_its_planned_block(self):
        self.manager.execute_tool("log_outcome", {"event": "pads", "status": "done"})
        span = next(s for s in self.manager.day_plan.spans if s.id == "pads")
        self.assertEqual((span.start_dt, span.end_dt), (at("16:40"), at("17:10")))
        out = self.turn("CALL show_schedule {}", "Here you go.", said="what's left?")
        self.assertNotIn("Restock brake pads", "\n".join(self.section(out, "The rest of today:")), out)


if __name__ == "__main__":
    unittest.main()
