"""What every message keeps to, and what Thirties says by itself: no done items in a schedule, never a raw id
, plain lines in the app as on the phone, a reminder that carries details, holding every message
until the user says something is over, and "have you started X yet, or are you still on Y?".
Invented items; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_one_conversation import OneConversationCase
from tests.test_schedule_reach import ServiceCase
from tests.test_whats_next import WhatsNextCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


class TestNoDoneRows(WhatsNextCase):
    """A schedule sent to the user never lists what was already done that morning."""

    def test_the_rest_of_the_day_leaves_done_items_out_and_the_whole_day_records_them(self):
        from thirties_core.messages import render_schedule
        plan = self.manager.day_plan
        plan.spans.append(EventSpan("wtr", "Water the plants", at("08:00"), at("08:15"), SpanCategory.ACTIVITY,
                                    status=SpanStatus.DONE))
        next(s for s in plan.spans if s.id == "glaze").status = SpanStatus.DONE  # done early
        text = render_schedule(plan, at("17:10"))
        self.assertNotIn("Water the plants", text)
        self.assertNotIn("Glazing", text)
        self.assertNotIn("(done)", text)
        whole = render_schedule(plan, at("17:10"), whole_day=True).splitlines()
        self.assertIn("8:00 AM – 8:15 AM Water the plants (done)", whole)  # the whole day is the day's record
        self.assertIn("7:00 PM – 8:00 PM Glazing (done)", whole)
        self.assertIn("8:00 PM – 9:30 PM Movie", whole)

    def test_a_done_row_in_the_models_own_reply_is_left_out(self):
        out = self.turn("Here's the day:\n- 8:00 AM – Water the plants (done)\n- 6:00 PM – Dinner\n- 7:00 PM – Glazing",
                        said="when's dinner?")
        self.assertEqual(out, "Here's the day:\n6:00 PM – Dinner\n7:00 PM – Glazing")


    def test_asked_what_got_done_the_done_rows_are_the_answer(self):
        out = self.turn("Done so far:\n- 8:00 AM – Water the plants (done)", said="what did I get done today?")
        self.assertEqual(out, "Done so far:\n8:00 AM – Water the plants (done)")


class TestNeverAnId(WhatsNextCase):
    """Never display a raw id: an id in a reply becomes the item's name."""

    def test_an_id_in_a_reply_becomes_its_name_and_id_notes_go(self):
        out = self.turn("Glazing (id glaze) is at 7, and bins out is 9f8e7d6c5b4a3210 [id 9f8e7d6c5b4a3210].",
                        said="when's glazing?")
        self.assertEqual(out, "Glazing is at 7, and bins out is.")  # unknown ids are dropped, never shown
        self.manager.day_plan.spans.append(EventSpan("7e3b9a1c4d2f8065", "Birthday cards", at("21:00"), at("21:30"),
                                                     SpanCategory.ACTIVITY))
        out = self.turn("7e3b9a1c4d2f8065 is at 9:00 PM.", said="what's at 9?")
        self.assertEqual(out, "Birthday cards is at 9:00 PM.")

    def test_the_guard_itself(self):
        from thirties_core.messages import names_not_ids
        self.assertEqual(names_not_ids("Work (id routine-2026-03-04-work) till 4"), "Work till 4")
        self.assertEqual(names_not_ids("Ping set (id 4f2a9c1e7b3d)."), "Ping set.")
        self.assertEqual(names_not_ids("id=cal:0a1b2c3d4e5f6789 moved", {}), "moved")
        self.assertEqual(names_not_ids("Room 2026, call 5551234567, colour ffffff"), "Room 2026, call 5551234567, "
                                                                                     "colour ffffff")  # not ids
        link = "the notes: https://example.org/commit/4f2a9c1e7b3d5a6b"
        self.assertEqual(names_not_ids(link), link)  # part of a link: left alone


class TestPlainLines(OneConversationCase):
    """11-05: never "- " bullets: the app's chat and the phone get the same plain lines."""

    def test_a_reply_with_bullets_is_plain_in_both(self):
        self.engine.set_next_response(reply("Tonight:\n- 7:00 PM – Bake bread\n- 8:00 PM – Read\n\n1. A choice"))
        self.svc.bridge.handle_updates([self.update("what's tonight?", 600)])
        said = self.said()[-1]
        self.assertEqual(said, "Tonight:\n7:00 PM – Bake bread\n8:00 PM – Read\n\n1. A choice")  # numbers stay
        self.assertSymmetric()
        conv = self.svc.manager().messages
        self.assertNotIn("\n- ", [m for m in conv if m.get("role") == "assistant"][-1]["content"])  # stored plain

    def test_a_notice_is_plain_too(self):
        self.svc.send("Not marked yet:\n- Bake bread\n- Read", "start")
        self.assertEqual(self.said()[-1], "Not marked yet:\nBake bread\nRead")
        self.assertSymmetric()


class TestReminderDetails(unittest.TestCase):
    """A reminder carries the details the user gave it, in their words."""

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.sched = make_scheduler()
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        from tests.fixtures import mark_up
        mark_up(self.sched.state_db, at("07:00"))

    def test_a_ping_carries_its_details(self):
        self.svc.manager().execute_tool("set_reminder", {"text": "Parcels at the front desk", "time": "3:00 PM",
                                                         "ping_only": True,
                                                         "details": "One from a bookshop and one from a pet shop"})
        self.svc.reminders(at("15:00"))
        self.assertEqual(self.sent[-1], ("reminder", "⏰ Reminder: Parcels at the front desk\n"
                                                     "One from a bookshop and one from a pet shop"))

    def test_a_thing_to_do_carries_them_in_its_ping_and_its_bell(self):
        out = self.svc.manager().execute_tool("set_reminder", {"text": "Collect the parcels", "time": "5:00 PM",
                                                               "details": "Two boxes, ask at the desk"})
        self.assertIn("with a ping", out)
        self.svc._manager = None
        self.svc.reminders(at("17:00"))  # medium: the ping
        self.assertEqual(self.sent[-1][1], "⏰ Reminder: Collect the parcels\nTwo boxes, ask at the desk")
        self.sched.config.assistant.message_level = "heavy"  # heavy: the bell is the ping
        self.svc.starts(at("17:00"))
        self.assertEqual(self.sent[-1][1].split("\n\n")[0], "🔔 5:00 PM: Collect the parcels\nTwo boxes, ask at the desk")


class TestHold(ServiceCase):
    """Everything Thirties would say by itself is held until the user says something is over."""

    def hold(self, **args):
        return self.svc.manager().execute_tool("hold_messages", args)

    def test_everything_waits_until_the_item_is_done(self):
        from thirties_core.memory import ReminderStore
        self.add("intv", "Interview", "16:30", "17:30")
        out = self.hold(until_done="Interview")
        self.assertEqual(out, "Holding all messages until Interview is done (or you say it's over).")
        ReminderStore(self.sched.state_db).add(at("17:05"), "Take the bins in")
        self.svc.starts(at("17:00"))
        self.svc.reminders(at("17:06"))
        self.svc.check_ins(at("17:45"))
        self.assertEqual(self.desktop, [])  # no bell, no reminder, no check-in, on any screen
        self.svc.bridge.broadcast.assert_not_called()
        self.svc.manager().execute_tool("log_outcome", {"event": "intv", "status": "done"})  # "interview done"
        self.svc.hold_check(at("17:50"))
        self.assertTrue(self.last("hold").startswith("Messages are back on."))
        self.svc.reminders(at("17:51"))
        self.assertEqual(self.last("reminder"), "⏰ Reminder: Take the bins in")  # kept, then sent

    def test_it_ends_on_the_users_words(self):
        self.hold(until_said="the interview is over")
        self.svc.manager().record_exchange("hold on, still in the waiting room", "OK.")
        self.svc.hold_check(at("17:20"))
        self.svc.starts(at("17:00"))
        self.assertEqual(self.desktop, [])  # other words: still held
        m = self.svc.manager()
        m.brain.messages.append({"role": "user", "content": "Interview over!", "at": at("17:30").isoformat()})
        m._save()
        self.svc.hold_check(at("17:31"))
        self.assertEqual(len([k for k, _ in self.desktop if k == "hold"]), 1)

    def test_end_hold_and_the_end_of_the_day(self):
        from thirties_core import holds
        self.hold()
        self.assertIsNotNone(holds.active(self.sched.state_db, at("22:00")))
        self.assertIsNone(holds.active(self.sched.state_db, at("07:30", 1)))  # never past the day
        self.assertEqual(self.svc.manager().execute_tool("end_hold", {}), "Messages are back on.")
        self.svc.starts(at("17:00"))
        self.assertIn("Sort the receipts", self.last("start"))

    def test_never_past_the_day_and_no_offer_while_held(self):
        from thirties_core import holds
        self.hold(until="10:00 AM")  # tomorrow morning
        self.assertIsNone(holds.active(self.sched.state_db, at("07:30", 1)))  # the morning always comes
        self.svc.manager().execute_tool("end_hold", {})
        self.hold()
        for sid, start in (("a", "10:00"), ("b", "10:30")):
            self.add(sid, f"Errand {sid}", start, (at(start) + timedelta(minutes=15)).strftime("%H:%M"))
        set_debug_time(at("19:10"))
        self.svc.evening_catch_up(at("19:10"))
        from thirties_core import catch_up
        self.assertIsNone(catch_up.pending(self.svc.manager().tools, at("19:10")))  # never offered unseen
        self.assertEqual(self.desktop, [])

    def test_a_reply_still_goes_out_during_a_hold(self):
        self.hold()
        self.svc.send("Here's your answer.", "reply", to_conversation=False)
        self.assertEqual(self.desktop, [("reply", "Here's your answer.")])


class TestStartedOrStillOn(ServiceCase):
    """A check-in asks whether the next item has started, or the user is still on the one before."""

    def test_paired_with_whats_on_now(self):
        self.svc.check_ins(at("18:15"))
        self.assertTrue(self.last("checkin").startswith(
            "👋 Just checking in: have you started Dinner yet, or are you still on Sort the receipts?"))

    def test_with_nothing_on_now_as_before(self):
        self.svc.check_ins(at("21:45"))  # Film night ended; nothing on now
        self.assertTrue(self.last("checkin").startswith(
            "👋 Just checking in: did you get to Film night, or are you still on it?"))

    def test_several_finished_and_one_on_now(self):
        self.add("pot", "Water the pots", "17:30", "17:45")
        self.svc.check_ins(at("18:15"))
        self.assertTrue(self.last("checkin").startswith(
            "👋 How did it go? Did you get to Sort the receipts and Water the pots? And have you started Dinner yet?"))


if __name__ == "__main__":
    unittest.main()
