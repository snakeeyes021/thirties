"""How much Thirties says unasked in a day (fewer_messages.py, background.py).

An invented person: a late shift at a pottery studio (11:00 AM - 6:00 PM), a canoe in the morning, test tiles in
the evening (tests/message_day.py). One rule per test: a bell at every thirty where an item starts, routine
steps too; bells carry no unmarked list; one question per item; a recent message doesn't swallow a bell; the
switch turns bells off; identical words go out once a day."""

import unittest
from datetime import timedelta

from tests.fixtures import at, mark_up
from tests.message_day import count, service, simulate
from thirties_core.astronomy import set_debug_time
from thirties_core.messages import MISSED_HEADING
from thirties_core.models import EventSpan, SpanCategory


class TheWholeDay(unittest.TestCase):

    def test_heavy_rings_once_for_each_thirty_where_something_starts(self):
        sent = simulate("heavy")
        bells = [text.split("\n")[0] for _, _, text in sent if text.startswith("🔔")]
        self.assertEqual([b.split(": ")[0] for b in bells],  # one per thirty where something starts, in order
                         ["🔔 8:00 AM", "🔔 8:30 AM", "🔔 9:00 AM", "🔔 10:30 AM", "🔔 6:30 PM", "🔔 7:00 PM",
                          "🔔 8:00 PM", "🔔 8:30 PM", "🔔 10:00 PM"], bells)
        c = count(sent)
        self.assertEqual(c["with an unmarked list"], 0, c)
        self.assertEqual(c["repeated"], 0, c)

    def test_medium_is_fewer_still(self):
        c = count(simulate("medium"))
        self.assertLessEqual(c["total"], 5, c)  # 7 before: one item asked about three times
        self.assertEqual(c.get("stretch start"), 2, c)  # the morning one too, though the user wrote just before
        self.assertEqual(c["with an unmarked list"], 0, c)

    def test_each_item_is_asked_about_once(self):
        for level in ("heavy", "medium"):
            asked = {}
            for clock, kind, text in simulate(level):
                if kind != "checkin" or text.startswith("💼"):
                    continue
                for title in ("Pack a pear and cheese", "Supper", "Glaze test tiles", "Oil the paddle",
                              "Mist the ferns", "Set out tomorrow's apron"):
                    if title in text:
                        asked.setdefault(title, []).append(clock)
            self.assertTrue(asked, level)
            self.assertEqual({t: c for t, c in asked.items() if len(c) > 1}, {}, level)

    def test_off_switch_for_start_notices(self):
        sent = simulate("heavy", block_notices=False)
        self.assertFalse([t for _, k, t in sent if k == "start"], sent)
        self.assertTrue([t for _, k, t in sent if k == "checkin"], sent)  # only the notices go
        sent = simulate("medium", block_notices=False)
        self.assertFalse([t for _, k, t in sent if t.startswith("🗓")], sent)


class Case(unittest.TestCase):
    level = "heavy"

    def setUp(self):
        self.svc, self.out = service(self.level)
        self.addCleanup(set_debug_time, None)
        mark_up(self.svc.scheduler.state_db, at("06:30"))  # up long ago; not talking now

    def add(self, sid, title, start, end, source="user"):
        self.svc.scheduler.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY,
                                                          source=source))
        self.svc._manager = None

    def run_from(self, start, end, duty):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            duty(t)
            t += timedelta(minutes=5)

    def sent(self, kind=None):
        return [t for _, k, t in self.out if kind is None or k == kind]


class BlockNotices(Case):

    def test_a_bell_at_each_thirty_of_a_run_of_items(self):
        self.run_from("18:25", "21:35", self.svc.starts)
        bells = [b.split("\n\n")[0] for b in self.sent("start")]
        self.assertEqual(bells, ["🔔 6:30 PM: Supper", "🔔 7:00 PM: Glaze test tiles", "🔔 8:00 PM: Oil the paddle",
                                 "🔔 8:30 PM: Read about kilns"])

    def test_a_thirty_of_routine_steps_gets_a_bell_naming_them(self):
        self.run_from("21:55", "22:05", self.svc.starts)
        self.assertEqual([b.split("\n\n")[0] for b in self.sent("start")],
                         ["🔔 10:00 PM: Set out tomorrow's apron, Write three lines in the logbook"])

    def test_no_list_of_whats_not_marked_under_a_bell(self):
        self.add("cl", "Wedge the reclaim clay", "10:00", "10:30")
        self.add("ki", "Sweep the kiln room", "21:35", "21:50")
        for t in ("18:30", "21:30"):
            set_debug_time(at(t))
            self.svc.starts(at(t))
        self.assertEqual(len(self.sent("start")), 2, self.out)
        self.assertFalse([b for b in self.sent("start") if MISSED_HEADING in b], self.out)

    def test_a_bell_rings_soon_after_the_user_wrote(self):
        mark_up(self.svc.scheduler.state_db, at("18:20"))
        set_debug_time(at("18:30"))
        self.svc.starts(at("18:30"))
        self.svc.starts(at("19:00"))
        self.assertEqual([b.split("\n\n")[0] for b in self.sent("start")],
                         ["🔔 6:30 PM: Supper", "🔔 7:00 PM: Glaze test tiles"])

    def test_a_reminder_on_an_item_still_rings(self):
        from thirties_core.memory import ReminderStore
        ReminderStore(self.svc.scheduler.state_db).add(at("19:00"), "Glaze test tiles", span_id="gl")
        mark_up(self.svc.scheduler.state_db, at("18:50"))  # talking, and inside the evening's run
        set_debug_time(at("19:00"))
        self.svc.reminders(at("19:00"))
        self.svc.starts(at("19:00"))
        self.assertEqual(len(self.out), 1, self.out)
        self.assertIn("Glaze test tiles", self.out[0][2])

    def test_with_start_notices_off_a_reminder_on_an_item_comes_as_a_reminder(self):
        self.svc.config.telegram.notify_block_transitions = False
        from thirties_core.memory import ReminderStore
        ReminderStore(self.svc.scheduler.state_db).add(at("19:00"), "Glaze test tiles", span_id="gl")
        set_debug_time(at("19:00"))
        self.svc.reminders(at("19:00"))
        self.svc.starts(at("19:00"))
        self.assertEqual([k for _, k, _ in self.out], ["reminder"])


class AskedOnce(Case):
    level = "medium"

    def test_the_evening_doesnt_ask_again_what_a_stretch_asked(self):
        self.run_from("11:00", "11:20", self.svc.stretches)
        self.assertEqual(len([q for q in self.sent("checkin") if "Pack a pear and cheese" in q]), 1, self.out)
        before = len(self.sent("checkin"))
        mark_up(self.svc.scheduler.state_db, at("12:00"))  # they answered (two unanswered would pause asking)
        self.run_from("18:00", "22:30", lambda t: (self.svc.evening_catch_up(t), self.svc.wrap_up(t)))
        later = self.sent("checkin")[before:]
        self.assertTrue(later, self.out)  # the evening asks about the rest
        self.assertFalse([q for q in later if "Pack a pear and cheese" in q], later)

    def test_the_last_stretch_isnt_asked_again_after_the_before_bed_question(self):
        both = lambda t: (self.svc.wrap_up(t), self.svc.stretches(t))  # noqa: E731
        self.run_from("21:00", "22:20", both)
        mark_up(self.svc.scheduler.state_db, at("22:20"))  # they answered: asking may go on
        self.run_from("22:25", "23:00", both)
        for title in ("Supper", "Glaze test tiles", "Oil the paddle", "Set out tomorrow's apron"):
            self.assertEqual(len([q for q in self.sent("checkin") if title in q]), 1, (title, self.out))


class CatchUpWaitsForTheRun(Case):

    def test_the_catch_up_leaves_an_item_to_its_runs_question(self):
        import json  # the morning's items were asked about at midday
        self.svc.scheduler.state_db.set_sync_meta("checkins_asked", json.dumps(
            {i: "2026-10-15" for i in ("r1", "r2", "r3", "r4", "cn", "sn")}))
        self.run_from("19:55", "21:45", lambda t: (self.svc.evening_catch_up(t), self.svc.check_ins(t)))
        glaze = [q for q in self.sent("checkin") if "Glaze test tiles" in q]
        self.assertEqual(len(glaze), 1, self.out)
        self.assertIn("Supper", glaze[0])  # asked with the rest of its run


class NoRepeats(Case):

    def test_the_same_words_go_out_once_a_day(self):
        for t in ("12:00", "12:05"):
            self.svc.send("👋 How did it go? Did you get to Trim the bowls?", "checkin", now=at(t))
        self.assertEqual(len(self.out), 1)

    def test_a_reminder_may_say_the_same_words_twice(self):
        for t in ("12:00", "15:00"):
            self.svc.send("⏰ Reminder: Turn the drying pots", "reminder", now=at(t))
        self.assertEqual(len(self.out), 2)


if __name__ == "__main__":
    unittest.main()
