"""A line giving an item's time is said once a day: never again word for word under later replies unless the item
moved or the user asks about it, and never after the user asks for it to stop. A deadline that's part of an item's
title isn't read as a wrong time for it (which is how such a line got added under every reply). Invented: a late
shift at a pottery studio and an evening canoe outing."""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core import repeat_lines
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory

KEY_BACK = "Return the kiln key by 4:30 PM"


class LinesCase(ConversationTestCase):

    def setUp(self):
        super().setUp()
        self.manager.tools.day_plan.spans.append(
            EventSpan("key", KEY_BACK, at("15:30"), at("16:00"), SpanCategory.TASK))
        self.manager.tools.day_plan.spans.append(
            EventSpan("canoe", "Canoe on the reservoir", at("19:00"), at("20:30"), SpanCategory.ACTIVITY))

    def turn(self, said, model_says, minute):
        set_debug_time(at(minute))
        self.engine.set_next_response(reply(model_says))
        return self.manager.send_user_message(said)


class TestATitlesOwnTime(LinesCase):

    def wrong(self, said):
        from thirties_core.brains.frontier import FrontierBrain
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        return brain._wrong_times(said)

    def test_the_deadline_in_a_title_is_no_claim_about_its_time(self):
        self.assertEqual(self.wrong("Sure, the kiln key goes back by 4:30 PM, so there's room."), [])
        self.assertEqual(self.wrong("3:30 PM – Return the kiln key by 4:30 PM"), [])

    def test_a_wrong_time_for_it_is_still_caught(self):
        self.assertEqual(self.wrong("The kiln key return is at 2:00 PM."), [f"{KEY_BACK} is at 3:30 PM"])


class TestSaidOncePerDay(LinesCase):

    def test_the_same_line_isnt_added_to_later_replies(self):
        first = self.turn("what's this afternoon like?", f"Studio from 11. {KEY_BACK} is at 3:30 PM.", "09:00")
        self.assertIn(f"{KEY_BACK} is at 3:30 PM.", first)
        second = self.turn("ok, and the canoe?", f"Canoe on the reservoir is at 7:00 PM. {KEY_BACK} is at 3:30 PM.",
                           "09:05")
        self.assertIn("Canoe on the reservoir is at 7:00 PM.", second)
        self.assertNotIn("kiln", second.lower())
        third = self.turn("thanks, heading to the wheel", f"Have a good session! {KEY_BACK} is at 3:30 PM.", "09:10")
        self.assertEqual(third, "Have a good session!")

    def test_asked_about_it_the_answer_stays(self):
        self.turn("what's this afternoon like?", f"{KEY_BACK} is at 3:30 PM.", "09:00")
        out = self.turn("when does the kiln key go back again?", f"{KEY_BACK} is at 3:30 PM.", "09:05")
        self.assertEqual(out, f"{KEY_BACK} is at 3:30 PM.")

    def test_a_new_time_is_news(self):
        self.turn("what's this afternoon like?", f"Sure. {KEY_BACK} is at 3:30 PM.", "09:00")
        item = next(s for s in self.manager.tools.day_plan.spans if s.id == "key")
        item.start_dt, item.end_dt = at("15:00"), at("15:30")
        out = self.turn("anything changed?", f"Yes. {KEY_BACK} is at 3:00 PM.", "09:05")
        self.assertIn("is at 3:00 PM", out)


class TestAskedToStop(LinesCase):

    def test_stopped_for_the_rest_of_the_day_until_it_moves(self):
        self.turn("plan the evening", f"Canoe at 7. {KEY_BACK} is at 3:30 PM.", "09:00")
        self.turn("good", f"Great. {KEY_BACK} is still at 3:30 PM.", "09:05")
        out = self.turn("please quit mentioning the kiln key, it's in my head", "Understood.", "09:10")
        self.assertEqual(out, "Understood.")
        out = self.turn("glazing now", f"Enjoy! {KEY_BACK} is still at 3:30 PM.", "09:20")
        self.assertEqual(out, "Enjoy!")
        item = next(s for s in self.manager.tools.day_plan.spans if s.id == "key")
        item.start_dt, item.end_dt = at("14:30"), at("15:00")
        out = self.turn("what about the rest", f"OK. {KEY_BACK} is at 2:30 PM.", "09:30")
        self.assertIn("2:30 PM", out)  # it moved: that's news

    def test_without_naming_it_the_last_replys_lines_stop(self):
        self.turn("plan the evening", f"Here you go. {KEY_BACK} is at 3:30 PM.", "09:00")
        self.turn("you've told me that already", "Got it.", "09:05")
        out = self.turn("ok", f"Sounds good. {KEY_BACK} is at 3:30 PM.", "09:10")
        self.assertEqual(out, "Sounds good.")

    def test_reading_the_ask(self):
        plan = self.manager.day_plan
        db = self.manager.scheduler.state_db
        msgs = [{"role": "assistant", "content": f"Fine. {KEY_BACK} is at 3:30 PM."}]
        self.assertEqual(repeat_lines.stop_asked("Yes, I heard you", msgs, plan, db, plan.target_date), [KEY_BACK.lower()])
        self.assertEqual(repeat_lines.stop_asked("I know the kiln is hot", [], plan, db, plan.target_date), [])
        self.assertEqual(repeat_lines.stop_asked("Leave out the canoe sentence in your answers", msgs, plan, db, plan.target_date),
                         ["canoe on the reservoir"])


if __name__ == "__main__":
    unittest.main()
