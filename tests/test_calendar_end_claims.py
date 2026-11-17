"""A reply that gives a calendar event's real start and end ("11:15 AM Pottery class (until 12:40 PM)") is a correct
claim: the reply-against-the-plan check sends back only a wrong time, never a right start with its real end. Invented
event."""

from tests.fixtures import at, event
from tests.test_conversation import ConversationTestCase, reply


class TestACorrectCalendarEndIsNoMismatch(ConversationTestCase):

    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(events=[event("cal-pottery", "11:15", "12:40", "Pottery class")])

    def ask(self, said):
        self.engine.responses = [reply(said), reply("That was wrong. It is at some other time.")]
        return self.manager.send_user_message("what's on today?")

    def test_start_with_its_real_end_in_the_until_form_passes(self):
        said = "Today:\n11:15 AM Pottery class (until 12:40 PM)"
        self.assertEqual(self.ask(said), said)

    def test_a_sentence_with_start_and_real_end_passes(self):
        said = "Pottery class is at 11:15 AM, until 12:40 PM."
        self.assertEqual(self.ask(said), said)

    def test_other_starts_and_lengths_pass_with_their_real_end(self):
        from datetime import timedelta
        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.clock import format_clock
        for start, minutes in (("11:25", 25), ("11:50", 100), ("10:05", 10), ("15:40", 90)):
            begin = at(start)
            end = begin + timedelta(minutes=minutes)
            manager = self.make_manager(events=[event("cal-pottery", start, end.strftime("%H:%M"), "Pottery class")])
            brain = FrontierBrain.__new__(FrontierBrain)
            brain.tools = manager.tools
            for said in (f"{format_clock(begin)} Pottery class (until {format_clock(end)})",
                         f"{format_clock(begin)} – {format_clock(end)} Pottery class"):
                self.assertEqual(brain._wrong_times(said), [], said)

    def test_a_wrong_start_and_end_is_still_sent_back(self):
        out = self.ask("Today:\n11:45 AM Pottery class (until 12:55 PM)")
        self.assertNotIn("12:55 PM", out)
