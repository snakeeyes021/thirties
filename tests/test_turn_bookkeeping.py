"""The one-prompt brain's turn bookkeeping: failures per item, retries checked like any round, a broken tool
leaves no half change, an ambiguous "done" asks, history keeps the final reply, and a reply glued onto a call is
read as the call and the text. Scripted model replies; invented items."""

import unittest
from unittest.mock import patch

from tests.test_conversation import ConversationTestCase, reply
from thirties_core.inference import parse_model_directives
from thirties_core.plan_tools import NOT_DONE


class TestGluedCalls(unittest.TestCase):
    def test_text_glued_after_the_arguments_is_kept_and_the_call_runs(self):
        text, calls = parse_model_directives('CALL list_schedule {}Sorry about that, here is the day.')
        self.assertEqual([c["name"] for c in calls], ["list_schedule"])
        self.assertNotIn("_parse_error", calls[0]["arguments"])
        self.assertEqual(text, "Sorry about that, here is the day.")

    def test_arguments_then_text(self):
        text, calls = parse_model_directives('CALL add_event {"title": "Walk", "start": "5:00 PM"} Added it.')
        self.assertEqual(calls[0]["arguments"], {"title": "Walk", "start": "5:00 PM"})
        self.assertEqual(text, "Added it.")


class TestTurnBookkeeping(ConversationTestCase):

    def add(self, title, start):
        return self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})

    def test_a_failure_isnt_forgotten_because_something_else_worked(self):
        """A failure used to be forgotten when another change worked: "move A refused, add B" ended with "All set"."""
        self.engine.set_next_response(reply('CALL update_event {"event": "Nonexistent chore", "start": "6:00 PM"}\n'
                                            'CALL add_event {"title": "Water the ferns", "start": "7:00 PM", '
                                            '"duration": "30 min"}'))
        self.engine.set_next_response(reply("All set: moved and added."))
        self.engine.set_next_response(reply("All set: moved and added."))  # the rewrite, still claiming it
        out = self.manager.send_user_message("move the chore to 6 and add ferns at 7")
        self.assertIn("didn't go through", out)

    def test_a_later_call_on_the_same_item_clears_its_failure(self):
        self.add("Stretch", "5:00 PM")
        self.engine.set_next_response(reply('CALL update_event {"event": "Stretch", "start": "nonsense"}\n'
                                            'CALL update_event {"event": "Stretch", "start": "6:00 PM"}'))
        self.engine.set_next_response(reply("Moved Stretch to 6."))
        out = self.manager.send_user_message("stretch at 6")
        self.assertEqual(out, "Moved Stretch to 6.")

    def test_a_retry_s_calls_are_checked_too(self):
        """The empty-reply retry's calls used to run with no follow-up and no check."""
        self.engine.set_next_response(reply(""))  # no reply at all
        self.engine.set_next_response(reply('CALL update_event {"event": "Nonexistent chore", "start": "6:00 PM"}'))
        self.engine.set_next_response(reply("I couldn't find that chore. Which one did you mean?"))
        out = self.manager.send_user_message("move the chore to 6")
        self.assertIn("couldn't find", out)
        self.assertEqual(len(self.engine.call_history), 3)  # the refusal got its follow-up round

    def test_history_keeps_the_final_reply_only(self):
        """Drafts and the app's own checks went back to the model on every later turn."""
        self.engine.set_next_response(reply("Your plan has been updated."))  # a claim with no call: checked
        self.engine.set_next_response(reply("Nothing changed yet; what should I add?"))
        self.manager.send_user_message("hello")
        self.engine.set_next_response(reply("Sure."))
        self.manager.send_user_message("thanks")
        seen = self.engine.call_history[-1]
        texts = [str(m.get("content") or "") for m in seen]
        self.assertFalse(any("[System check" in t for t in texts))
        self.assertNotIn("Your plan has been updated.", texts)
        self.assertIn("Nothing changed yet; what should I add?", texts)


class TestBrokenTool(ConversationTestCase):
    def test_an_unexpected_error_leaves_the_plan_as_it_was(self):
        """A tool error of an unexpected type could leave half a change and no reply."""
        tools = self.manager.tools
        before = sorted((s.id, s.start_dt) for s in tools.day_plan.spans)

        def breaks_halfway(**_):
            tools.day_plan.spans[0].start_dt = tools.day_plan.spans[0].start_dt.replace(hour=1)
            raise KeyError("boom")
        with patch.object(type(tools), "add_event", lambda self, **kw: breaks_halfway(**kw)):
            out = tools.execute("add_event", {"title": "Anything", "start": "5:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("nothing was changed", out)
        self.assertEqual(sorted((s.id, s.start_dt) for s in tools.day_plan.spans), before)


class TestRefusalChangesNothing(ConversationTestCase):
    def test_a_change_that_fails_halfway_is_undone(self):
        """update_event could change part of an item and then refuse, leaving the half change."""
        from thirties_core.assistant_tools import ToolError
        tools = self.manager.tools
        tools.execute("add_event", {"title": "Repaint the shed door", "start": "5:00 PM", "duration": "1 hour"})
        door = next(s for s in tools.day_plan.spans if s.title == "Repaint the shed door")
        before = (door.title, door.start_dt, door.end_dt)

        def refuses(*a, **k):
            raise ToolError("it can't go there")
        # pushing what follows refuses after the item's title and times were already changed
        with patch.object(type(tools), "_push_wanted", lambda *a, **k: True), \
                patch.object(type(tools), "_push_following", refuses):
            out = tools.execute("update_event", {"event": door.id, "title": "Paint the shed door", "start": "6:00 PM",
                                                 "push_later": True})
        self.assertTrue(out.startswith(NOT_DONE), out)
        door = next(s for s in tools.day_plan.spans if s.id == door.id)
        self.assertEqual((door.title, door.start_dt, door.end_dt), before)


class TestShiftKeepsARealTime(ConversationTestCase):
    def test_a_time_the_user_gave_survives_a_shift(self):
        """A 7:15 item shifted an hour later went to 8:30 instead of 8:15."""
        from tests.fixtures import at
        tools = self.manager.tools
        tools.user_message = "stretching at 7:15"
        tools.execute("add_event", {"title": "Stretching", "start": "7:15 PM", "duration": "30 min"})
        tools.user_message = "push stretching back an hour"
        tools.execute("update_event", {"event": "Stretching", "shift": "+1 hour"})
        s = next(s for s in tools.day_plan.spans if s.title == "Stretching")
        self.assertEqual(s.start_dt, at("20:15"))


class TestDoneSettlesEarlierCopies(ConversationTestCase):
    def test_an_earlier_copy_of_a_done_one_off_stops_being_asked_about(self):
        """Planned again after a move failed, then done: the first copy stayed "not marked yet" all day."""
        from tests.fixtures import at
        from thirties_core.astronomy import set_debug_time
        from thirties_core.models import SpanStatus
        tools = self.manager.tools
        tools.execute("add_event", {"title": "File the warranty claim", "start": "11:00 AM", "duration": "30 min"})
        set_debug_time(at("13:40"))
        tools.execute("add_event", {"title": "File the warranty claim", "start": "1:00 PM", "duration": "30 min",
                                    "again": True})
        second = max((s for s in tools.day_plan.spans if s.title == "File the warranty claim"), key=lambda s: s.start_dt)
        tools.execute("log_outcome", {"event": second.id, "status": "done"})
        first = min((s for s in tools.day_plan.spans if s.title == "File the warranty claim"), key=lambda s: s.start_dt)
        self.assertEqual(first.status, SpanStatus.UNKNOWN)  # settled, not nagged about
        self.assertNotIn("File the warranty claim", tools.schedule_text().split("Not marked yet")[-1]
                         if "Not marked yet" in tools.schedule_text() else "")


class TestOnlySaidTimes(ConversationTestCase):
    def test_a_time_the_model_worked_out_is_dropped_and_a_said_one_kept(self):
        """The model estimated when things happened, and its arithmetic won over Python's."""
        from tests.fixtures import at
        from thirties_core.astronomy import set_debug_time
        tools = self.manager.tools
        tools.execute("add_event", {"title": "Sort the recycling", "start": "5:00 PM", "duration": "30 min"})
        tools.execute("add_event", {"title": "Water the ferns", "start": "6:00 PM", "duration": "30 min"})
        set_debug_time(at("18:40"))
        tools.user_message = "recycling's sorted"
        tools.execute("log_outcome", {"event": "Sort the recycling", "status": "done", "finished": "5:20 PM"})
        recycling = next(s for s in tools.day_plan.spans if s.title == "Sort the recycling")
        self.assertNotEqual(recycling.actual_end, at("17:20"))  # not said: Python's estimate instead
        tools.user_message = "watered the ferns, finished at 6:25"
        tools.execute("log_outcome", {"event": "Water the ferns", "status": "done", "finished": "6:25 PM"})
        ferns = next(s for s in tools.day_plan.spans if s.title == "Water the ferns")
        self.assertEqual(ferns.actual_end, at("18:25"))  # said: kept


class TestPhantomDone(ConversationTestCase):
    def test_done_with_two_matches_asks_which(self):
        """Saying done with a name matching two items recorded a new item and left both open."""
        tools = self.manager.tools
        tools.execute("add_event", {"title": "Call the dentist", "start": "5:00 PM", "duration": "30 min"})
        tools.execute("add_event", {"title": "Call the plumber", "start": "6:00 PM", "duration": "30 min"})
        count = len(tools.day_plan.spans)
        out = tools.execute("log_outcome", {"event": "Call", "status": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("matches several items", out)
        self.assertEqual(len(tools.day_plan.spans), count)  # no phantom item


if __name__ == "__main__":
    unittest.main()
