"""A calendar event can run to the time the user says, on their plan, without the calendar being written.

Seen in a simulated day: the user asked three times for an appointment to run later than its calendar end, and
was told each time to change it in their calendar app. Calendar events aren't edited, but the plan keeps the
user's own start or end for one, shown as they said it, and it stays when the day is built again. On invented
events only.
"""

from tests.fixtures import DAY, at, event
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.plan_tools import NOT_DONE


class OwnTimesCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.events = [event("cal-dentist", "14:00", "14:45", "Dentist check-up")]
        self.manager = self.make_manager(events=self.events)
        self.t = self.manager.tools

    def dentist(self, plan=None):
        return next(s for s in (plan or self.manager.day_plan).spans if s.title == "Dentist check-up")

    def change(self, said, **args):
        self.t.user_message = said
        try:
            return self.t.execute("update_event", {"event": "Dentist check-up", **args})
        finally:
            self.t.user_message = None


class TestTheUsersOwnEnd(OwnTimesCase):
    def test_the_end_the_user_says_is_kept_on_the_plan(self):
        out = self.change("the dentist runs till 3:15, not 2:45", end="3:15 PM")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.dentist().end_dt, at("15:15"))
        self.assertEqual(self.dentist().start_dt, at("14:00"))
        self.assertIn("calendar", out)  # the answer says the calendar itself is as it was

    def test_the_calendar_is_never_written(self):
        self.change("dentist is 2 to 3:15", end="3:15 PM")
        self.assertEqual((self.events[0].start_dt, self.events[0].end_dt), (at("14:00"), at("14:45")))

    def test_it_stays_when_the_day_is_built_again(self):
        self.change("dentist is 2 to 3:15", end="3:15 PM")
        plan, _ = self.manager.scheduler.build_day_plan(DAY)
        self.assertEqual(self.dentist(plan).end_dt, at("15:15"))

    def test_shown_as_the_user_said_it(self):
        self.change("dentist is 2 to 3:15", end="3:15 PM")
        shown = self.t.execute("show_schedule", {"whole_day": True})
        self.assertIn("2:00 PM – 3:15 PM Dentist check-up", shown)
        self.assertNotIn("2:45 PM", shown)

    def test_a_start_the_user_says_too(self):
        out = self.change("the dentist actually starts 1:45", start="1:45 PM")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.dentist().start_dt, at("13:45"))
        self.assertEqual(self.dentist().end_dt, at("14:45"))

    def test_a_time_the_user_didnt_give_is_still_refused(self):
        out = self.change("plan my afternoon", end="3:30 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.dentist().end_dt, at("14:45"))

    def test_an_end_before_its_start_is_refused(self):
        out = self.change("the dentist ends at 1:30", end="1:30 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.dentist().end_dt, at("14:45"))


class TestOtherDevices(OwnTimesCase):
    def test_the_users_time_syncs(self):
        from tests.fixtures import make_scheduler
        from thirties_core.calendar_times import CalendarOwnTimes
        from thirties_core.sync_engine import SyncEngine
        self.change("dentist is 2 to 3:15", end="3:15 PM")
        records = CalendarOwnTimes(self.manager.scheduler.state_db).all_records()
        other = make_scheduler([event("cal-dentist", "14:00", "14:45", "Dentist check-up")])
        self.assertTrue(CalendarOwnTimes(other.state_db).merge_record(records[0]))
        self.assertFalse(CalendarOwnTimes(other.state_db).merge_record(records[0]))
        plan, _ = other.build_day_plan(DAY)
        self.assertEqual(self.dentist(plan).end_dt, at("15:15"))
        self.assertIn("calendar_own_times", SyncEngine._stores(type("E", (), {"state_db": other.state_db})()))


class TestInATurn(OwnTimesCase):
    def test_asked_once_it_is_done_and_the_reply_stands(self):
        said = "Dentist check-up now runs until 3:15 PM on your plan."
        self.engine.responses = [reply('CALL update_event {"event": "Dentist check-up", "end": "3:15 PM"}'),
                                 reply(said)]
        out = self.manager.send_user_message("the dentist goes 2 to 3:15, not 2:45")
        self.assertEqual(self.dentist().end_dt, at("15:15"))
        self.assertEqual(out, said)


if __name__ == "__main__":
    import unittest
    unittest.main()
