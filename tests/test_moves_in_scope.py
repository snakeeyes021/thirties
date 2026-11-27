"""A move the model makes on today stands, and the result says which items the user's words didn't cover.

Which item a move request means is the model's reading, and that stands; a careless model also moved a second
item nobody named so the first would fit. That second move is not refused (a move can be undone): it goes ahead,
and its result tells the model it also moved an item the user didn't name, so the model owns up to it in the
reply. Items the words do cover (named or pointed to, words for a group or a cleared stretch of the day, or a tie
the user set before) carry no such note.
Invented fixtures: a potter with a day off from the studio who also restores a canoe.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from tests.test_if_time import Base
from thirties_core import move_scope
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

TRIM = "Trim the bowl feet"
SEAT = "Re-cane the canoe seat"
GLAZE = "Mix a celadon glaze batch"
PORTAGE = "Portage practice at the boathouse"
ITEMS = ((TRIM, "1:30 PM"), (SEAT, "2:00 PM"), (GLAZE, "3:30 PM"), (PORTAGE, "4:30 PM"))


def _start(spans, title):
    return next(s for s in spans if s.title == title and s.status == SpanStatus.PLANNED).start_dt


class ScopeBase(Base):
    def setUp(self):
        super().setUp()
        self.t.execute("remove_event", {"event": "Work"})  # a day off from the studio
        for title, start in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def hull_inspection(self):
        """A fixed event from the user's calendar, 3:10 to 4:20."""
        self.t.day_plan.spans.append(EventSpan("cal:hull", "Hull inspection", at("15:10"), at("16:20"),
                                               SpanCategory.CALENDAR, source="calendar", source_event_id="hull",
                                               is_firm=True))
        self.t.day_plan.spans.sort(key=lambda s: s.start_dt)

    def turn(self, message, calls, before=()):
        """One turn of a careless model: each (title, args) update_event call for the user's ``message``. Returns
        (title, result, start before the turn) for each call."""
        self.t.recent_said = list(before)
        self.t.prior_exchange = (before[0] if before else "", "")
        self.t.user_message = message
        was = {title: self.start(title) for title, _ in calls}
        outs = []
        try:
            with model_call():
                for title, args in calls:
                    outs.append((title, self.t.execute("update_event", {"event": title, **args}), was[title]))
        finally:
            self.t.user_message = None
            self.t.recent_said = None
            self.t.prior_exchange = None
        return outs

    def start(self, title):
        return _start(self.t.day_plan.spans, title)

    def assert_unnamed(self, call):
        title, out, was = call
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn(f"{title}, which the user didn't name", out)
        self.assertNotEqual(self.start(title), was, title)

    def assert_moved(self, call):
        title, out, was = call
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("which the user didn't name", out)
        self.assertNotEqual(self.start(title), was, title)


class TestAnUnnamedNeighbour(ScopeBase):
    def test_named_item_moves_and_the_neighbour_is_said_to_be_unnamed(self):
        trim, seat = self.turn("Push Trim the bowl feet to 2:40, the wheel is out of action",
                               [(TRIM, {"start": "2:30 PM"}), (SEAT, {"start": "3:30 PM"})])
        self.assert_moved(trim)
        self.assert_unnamed(seat)

    def test_neighbour_moved_first_is_said_to_be_unnamed_too(self):
        seat, trim = self.turn("Push Trim the bowl feet to 2:40, the wheel is out of action",
                               [(SEAT, {"start": "3:30 PM"}), (TRIM, {"start": "2:30 PM"})])
        self.assert_unnamed(seat)
        self.assert_moved(trim)

    def test_a_pointer_is_the_item_and_nothing_else(self):
        self.t.prior_exchange = None
        trim, seat = self.turn("could it go later, nearer 2:40?", [(TRIM, {"start": "2:30 PM"}),
                                                                 (SEAT, {"start": "3:30 PM"})],
                               before=["what's first after lunch? Trim the bowl feet?"])
        self.assert_moved(trim)
        self.assert_unnamed(seat)

    def test_sharing_time_with_a_calendar_event_is_no_reason(self):
        """The moved item lands on a fixed calendar event's time; the unnamed item there is moved and said."""
        self.hull_inspection()
        glaze, portage = self.turn("glaze batch later on, nearer 4:40", [(GLAZE, {"start": "4:30 PM"}),
                                                                       (PORTAGE, {"start": "5:30 PM"})])
        self.assert_moved(glaze)
        self.assert_unnamed(portage)

    def test_a_calendar_event_running_over_moves_nothing_else(self):
        self.hull_inspection()
        hull, portage = self.turn("hull inspection is running over, it ends 4:50 now",
                                  [("Hull inspection", {"end": "4:50 PM"}), (PORTAGE, {"start": "5:30 PM"})])
        self.assertFalse(hull[1].startswith(NOT_DONE), hull[1])
        self.assert_unnamed(portage)


class TestTheModelsOwnReading(ScopeBase):
    def test_one_item_by_the_models_reading_moves(self):
        trim, = self.turn("the first clay job can slide an hour later", [(TRIM, {"start": "2:30 PM"})])
        self.assert_moved(trim)

    def test_a_second_by_its_reading_is_said_to_be_unnamed(self):
        trim, glaze = self.turn("the first clay job can slide an hour later",
                                [(TRIM, {"start": "2:30 PM"}), (GLAZE, {"start": "4:30 PM"})])
        self.assert_moved(trim)
        self.assert_unnamed(glaze)

    def test_plural_words_cover_as_many_as_they_say(self):
        trim, glaze, seat = self.turn("the two clay jobs: slide them past 4:40",
                                      [(TRIM, {"start": "5:30 PM"}), (GLAZE, {"start": "6:30 PM"}),
                                       (SEAT, {"start": "7:30 PM"})])
        self.assert_moved(trim)
        self.assert_moved(glaze)
        self.assert_unnamed(seat)

    def test_a_request_from_just_before_still_counts_once(self):
        trim, seat = self.turn("hm, still showing the old time", [(TRIM, {"start": "2:30 PM"}),
                                                                 (SEAT, {"start": "3:30 PM"})],
                               before=["can the first clay job slide an hour later?"])
        self.assert_moved(trim)
        self.assert_unnamed(seat)


class TestGroupWords(ScopeBase):
    def test_words_for_the_rest_of_a_stretch_cover_every_item(self):
        glaze, portage = self.turn("Kiln is cooling slow. Rest of the afternoon slides forty minutes on.",
                                   [(GLAZE, {"start": "4:30 PM"}), (PORTAGE, {"start": "5:30 PM"})])
        self.assert_moved(glaze)
        self.assert_moved(portage)

    def test_a_cleared_stretch_covers_what_is_in_it(self):
        trim, seat, glaze = self.turn("the kiln needs unloading, so clear 1:20 till 2:40 for that",
                                      [(TRIM, {"start": "2:30 PM"}), (SEAT, {"start": "3:30 PM"}),
                                       (GLAZE, {"start": "5:30 PM"})])
        self.assert_moved(trim)
        self.assert_moved(seat)
        self.assert_unnamed(glaze)

    def test_the_group_words_it_knows(self):
        for said in ("make space", "nudge the others", "all of them", "rest of the evening", "anything past the kiln",
                     "my whole evening", "whatever it takes", "rearrange the evening", "plan around the kiln"):
            self.assertTrue(move_scope.GROUP_RE.search(said), said)
        for said in ("everything went fine", "slide the glaze later", "fit it in somewhere", "move it to 4:40",
                     "the paddle is after the kiln"):
            self.assertFalse(move_scope.GROUP_RE.search(said), said)

    def test_sliding_along_needs_group_words(self):
        trim, = self.turn("Trim the bowl feet later, nearer 2:10", [(TRIM, {"start": "2:00 PM", "push_later": True})])
        self.assertTrue(trim[1].startswith(NOT_DONE), trim[1])
        self.assertIn(SEAT, trim[1])
        self.assertEqual(self.start(TRIM), trim[2])
        seat_was = self.start(SEAT)
        trim, = self.turn("Trim the bowl feet later, nearer 2:10, and make room after it",
                          [(TRIM, {"start": "2:00 PM", "push_later": True})])
        self.assert_moved(trim)
        self.assertNotEqual(self.start(SEAT), seat_was)


class TestATieFollows(ScopeBase):
    def setUp(self):
        super().setUp()
        for message, at_ in (("canoe seat later today, nearer 2:40", "2:30 PM"),
                             ("re-cane the seat right after the bowl trimming", "2:00 PM")):
            (_, out, _), = self.turn(message, [(SEAT, {"start": at_})])
            self.assertFalse(out.startswith(NOT_DONE), out)
        from thirties_core.relations import tied
        seat = next(s for s in self.t.day_plan.spans if s.title == SEAT)
        self.assertEqual([s.title for s in tied(self.t, seat)], [TRIM])

    def test_the_tied_item_may_follow_and_an_untied_one_stays(self):
        message = "bowl trimming has to slide to 3:50"
        seat, trim, glaze = self.turn(message, [(SEAT, {"start": "4:30 PM"}), (TRIM, {"start": "4:00 PM"}),
                                                (GLAZE, {"start": "5:30 PM"})])
        self.assert_moved(seat)
        self.assert_moved(trim)
        self.assert_unnamed(glaze)


class TestInAConversation(ConversationTestCase):
    """Whole turns with a mock model that moves a neighbour as well."""

    def setUp(self):
        super().setUp()
        for title, start in ((TRIM, "6:30 PM"), (SEAT, "7:00 PM"), (GLAZE, "7:30 PM")):
            out = self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def start(self, title):
        return _start(self.manager.day_plan.spans, title)

    def test_the_neighbour_moves_on_the_models_call_and_the_reply_stands(self):
        trim, seat = self.start(TRIM), self.start(SEAT)
        said = "Trim the bowl feet is at 7:00 PM now."
        self.engine.responses = [
            reply('CALL update_event {"event": "Trim the bowl feet", "start": "7:00 PM"}'),
            reply('CALL update_event {"event": "Re-cane the canoe seat", "start": "8:30 PM"}'),
            reply(said)]
        out = self.manager.send_user_message("can trim the bowl feet slip half an hour later")
        self.assertNotEqual(self.start(TRIM), trim)
        self.assertNotEqual(self.start(SEAT), seat)
        self.assertIn(said, out)

    def test_group_words_let_both_move(self):
        trim, seat = self.start(TRIM), self.start(SEAT)
        self.engine.responses = [
            reply('CALL update_event {"event": "Trim the bowl feet", "start": "7:00 PM"}'),
            reply('CALL update_event {"event": "Re-cane the canoe seat", "start": "7:30 PM", "alongside": true}'),
            reply("Trim the bowl feet is at 7:00 PM and Re-cane the canoe seat at 7:30 PM.")]
        self.manager.send_user_message("Supper ran on. Trim the bowl feet and the rest of the evening, half an hour on.")
        self.assertNotEqual(self.start(TRIM), trim)
        self.assertNotEqual(self.start(SEAT), seat)


if __name__ == "__main__":
    unittest.main()
