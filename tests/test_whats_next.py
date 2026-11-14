"""What's next after a change: Python's slice of the day at the end of the reply to a turn that changed something.
Scripted model replies; invented items. DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory


class WhatsNextCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("17:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "next"
        for sid, title, start, end in (("feed", "Feed the cat", "17:00", "17:15"),
                                       ("dinner", "Dinner", "18:00", "19:00"),
                                       ("glaze", "Glazing", "19:00", "20:00"),
                                       ("movie", "Movie", "20:00", "21:30"),
                                       ("bins", "Bins out", "21:30", "22:00")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        self.manager = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine,
                                           active_notes=[])
        self.manager.day_plan.spans.append(EventSpan("cal-book", "Book group", at("20:00"), at("21:00"),
                                                     SpanCategory.CALENDAR, source="calendar", shown_only=True))

    def turn(self, *replies: str, said: str = "ok") -> str:
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.manager.send_user_message(said)

    def tail(self, out: str) -> list:
        return out.split("\n\n")[-1].splitlines()


class TestWhatsNext(WhatsNextCase):

    def test_after_an_add(self) -> None:
        out = self.turn('CALL add_event {"title": "Quilting", "start": "5:30 PM", "duration": "30 minutes"}',
                        "Quilting's on at 5:30.", said="add quilting at 5:30")
        self.assertTrue(out.startswith("Quilting's on at 5:30."))
        self.assertEqual(self.tail(out), ["Now: Feed the cat", "Next:", "5:30 PM – Quilting", "6:00 PM – Dinner",
                                          "7:00 PM – Glazing"])
        self.assertFalse(any(line.startswith("- ") for line in self.tail(out)))  # plain lines

    def test_after_a_done(self) -> None:
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Nice, noted.", said="fed the cat")
        self.assertEqual(self.tail(out)[0], "Next:")  # the cat's done: nothing on now, so no "Now" line
        self.assertIn("6:00 PM – Dinner", self.tail(out))

    def test_a_move_that_bumps_something_names_it_where_it_went(self) -> None:
        out = self.turn('CALL update_event {"event": "bins", "start": "10:30 PM"}', "Moved the bins to 10:30.",
                        said="bins at 10:30")
        self.assertIn("Also moved: Bins out, now 10:30 PM", self.tail(out))  # past the next few: named
        self.manager.tools.user_message = None
        out = self.turn('CALL add_event {"title": "Kiln check", "start": "6:00 PM", "duration": "30 minutes", '
                        '"push_later": true}', "Kiln check at 6, the rest pushed back.",
                        said="kiln check at 6 and push the rest back")
        self.assertIn("6:00 PM – Kiln check", self.tail(out))
        self.assertIn("6:30 PM – Dinner", self.tail(out))  # bumped, and it shows where
        self.assertIn("Also moved: Movie, now 8:30 PM", self.tail(out))  # bumped past the next few: named

    def test_moved_items_by_their_block_joined_like_the_rows(self) -> None:
        from thirties_core.whats_next import render
        plan = self.manager.day_plan
        moved = [EventSpan("r1", "Shower", at("22:00"), at("22:10"), SpanCategory.ACTIVITY),
                 EventSpan("r2", "Journal", at("22:10"), at("22:25"), SpanCategory.ACTIVITY)]  # pushed past the rows
        plan.spans.extend(moved)
        lines = render(plan, at("17:10"), moved, said="push the rest back").splitlines()
        self.assertIn("Also moved: Shower, then Journal, now 10:00 PM", lines)  # the block, never a minute (10:10)
        self.assertFalse(any("10:10" in line for line in lines))

    def test_a_time_the_user_gave_keeps_its_minutes(self) -> None:
        from thirties_core.whats_next import _moved_line
        plan = self.manager.day_plan
        bins = next(s for s in plan.spans if s.id == "bins")
        bins.start_dt, bins.end_dt = at("21:45"), at("22:15")
        self.assertEqual(_moved_line([bins], plan, "put the bins out at 9:45"), "Also moved: Bins out, now 9:45 PM")
        self.assertEqual(_moved_line([bins], plan, "bins later"), "Also moved: Bins out, now 9:30 PM")

    def test_after_a_drop(self) -> None:
        out = self.turn('CALL remove_event {"event": "dinner"}', "Took dinner off.", said="drop dinner")
        self.assertNotIn("Dinner", "\n".join(self.tail(out)))
        self.assertIn("7:00 PM – Glazing", self.tail(out))

    def test_nothing_after_a_turn_that_changed_nothing(self) -> None:
        out = self.turn("You've got dinner at 6.", said="when's dinner?")
        self.assertEqual(out, "You've got dinner at 6.")
        out = self.turn('CALL update_event {"event": "nonexistent thing", "start": "6:00 PM"}',
                        "I couldn't find that one.", said="move the thing to 6")
        self.assertNotIn("Next:", out)  # a refused change is no change

    def test_never_two_schedules_in_one_reply(self) -> None:
        out = self.turn('CALL add_event {"title": "Quilting", "start": "5:30 PM", "duration": "30 minutes"}',
                        "Added, here's the evening.", said="add quilting at 5:30 and what's next after that?")
        self.assertNotIn("Next:", out)  # asked for the schedule: the reply shows it its own way
        self.manager.tools.shown_schedule = None
        out = self.turn('CALL add_event {"title": "Sanding", "start": "10:00 PM", "duration": "30 minutes"}',
                        'CALL show_schedule {}', "Here you go.", said="sanding at 10")
        self.assertEqual(out.count("Dinner"), 1)  # show_schedule's layout only

    def test_each_setting(self) -> None:
        self.sched.config.assistant.after_change = "nothing"
        out = self.turn('CALL add_event {"title": "Quilting", "start": "5:30 PM", "duration": "30 minutes"}',
                        "Added.", said="quilting at 5:30")
        self.assertEqual(out, "Added.")
        self.sched.config.assistant.after_change = "rest"
        out = self.turn('CALL update_event {"event": "quilting", "start": "10:30 PM"}', "Moved.", said="later")
        self.assertIn("The rest of today:", self.tail(out))
        self.assertIn("10:30 PM – Quilting", self.tail(out))  # the whole rest of today, not three rows
        self.assertIn("9:30 PM – Bins out (until 10:00 PM)", self.tail(out))

    def test_routine_parts_and_what_runs_across_them(self) -> None:
        for sid, title, start, end in (("r1", "Shower", "19:00", "19:10"), ("r2", "Journal", "19:10", "19:25")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan = self.sched.build_day_plan(DAY)[0]
        self.manager.day_plan.spans[:] = plan.spans
        out = self.turn('CALL remove_event {"event": "glaze"}', "Done, glazing's off.", said="drop the glazing")
        self.assertIn("7:00 PM – Shower, then Journal (until 7:30 PM)", self.tail(out))  # a routine's parts

    def test_someone_elses_event_has_its_own_line(self) -> None:
        self.sched.config.assistant.after_change = "rest"
        out = self.turn('CALL remove_event {"event": "dinner"}', "Dinner's off.", said="no dinner tonight")
        self.assertIn("Not yours: Book group, 8:00 PM", self.tail(out))
        self.assertFalse(any("Book group" in line for line in self.tail(out) if not line.startswith("Not yours")))


class TestOneRowFormatter(WhatsNextCase):
    """The schedule and what's next lay their rows out with the same formatter: a row reads the same in both."""

    def test_the_same_row_renders_the_same_in_both(self) -> None:
        from thirties_core.messages import plain_rows, render_schedule, schedule_rows
        from thirties_core.whats_next import render
        plan, now = self.manager.day_plan, at("17:10")
        nxt = render(plan, now).splitlines()
        rows = nxt[nxt.index("Next:") + 1:nxt.index("Next:") + 4]
        self.assertEqual(rows, plain_rows(schedule_rows(plan, at("17:30"), planned_only=True))[:3])
        shown = render_schedule(plan, now).splitlines()
        for row in rows:
            self.assertIn("- " + row, shown)  # the schedule's own row, word for word (it adds only its marker)
        self.assertIn("8:00 PM – Movie", render(plan, at("19:40")).splitlines())  # the same (until) rule too


class TestTheSlice(unittest.TestCase):
    """render() on its own: outside the day, nothing; an item placed across a routine part shows with it."""

    def test_render(self) -> None:
        from thirties_core.whats_next import render
        sched = make_scheduler()
        for sid, title, start, end in (("r1", "Shower", "19:00", "19:10"), ("radio", "Radio play", "19:00", "19:30")):
            sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan = sched.build_day_plan(DAY)[0]
        self.assertIn("7:00 PM – Radio play + Shower", render(plan, at("18:10")).splitlines())  # across it, with it
        self.assertEqual(render(plan, at("18:10"), mode="nothing"), "")
        self.assertEqual(render(plan, at("12:00", 3)), "")  # another day


class TestNotMarkedYet(WhatsNextCase):
    """At the bottom: earlier items whose time is over with nothing said (neither done nor skipped), plain lines,
    the most recent few; no section when there's nothing."""

    def earlier(self, *items) -> None:
        for sid, title, start, end in items:
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        self.manager.day_plan.spans[:] = self.sched.build_day_plan(DAY)[0].spans

    def test_after_a_done_at_the_bottom(self) -> None:
        self.earlier(("seed", "Water the seedlings", "07:00", "07:15"), ("tune", "Tune the ukulele", "16:00", "16:30"),
                     ("mail", "Post the parcel", "16:30", "17:00"))
        out = self.turn('CALL log_outcome {"event": "mail", "status": "skipped"}',  # skipped is marked: not listed
                        'CALL log_outcome {"event": "feed", "status": "done"}', "Nice, noted.",
                        said="fed the cat, and the parcel can wait")
        tail = self.tail(out)
        self.assertEqual(tail[0], "Next:")
        self.assertEqual(tail[-3:], ["Not marked yet:", "7:00 AM – Water the seedlings", "4:00 PM – Tune the ukulele"])
        self.assertFalse(any("Post the parcel" in line or "Feed the cat" in line for line in tail))

    def test_only_the_most_recent_five(self) -> None:
        self.earlier(*[(f"c{n}", f"Chore {n}", f"{9 + n:02d}:00", f"{9 + n:02d}:30") for n in range(7)])
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        tail = self.tail(out)
        section = tail[tail.index("Not marked yet:"):]
        self.assertEqual(section, ["Not marked yet:", "+2 more earlier", "11:00 AM – Chore 2", "12:00 PM – Chore 3",
                                   "1:00 PM – Chore 4", "2:00 PM – Chore 5", "3:00 PM – Chore 6"])

    def test_none_when_nothing_is_unmarked(self) -> None:
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.assertNotIn("Not marked yet", out)

    def test_not_in_the_block_still_under_way(self) -> None:
        self.earlier(("tea", "Brew tea", "17:00", "17:05"))  # over, but in the 5:00 block the user is still in
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.assertNotIn("Not marked yet", out)

    def test_the_setting_covers_it(self) -> None:
        self.earlier(("tune", "Tune the ukulele", "16:00", "16:30"))
        self.sched.config.assistant.after_change = "nothing"
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.assertEqual(out, "Noted.")

    def test_never_twice_and_counted_as_shown(self) -> None:
        from thirties_core.messages import MissedTracker
        self.earlier(("tune", "Tune the ukulele", "16:00", "16:30"))
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}',
                        "Noted.\n\nNot marked yet:\n- Tune the ukulele", said="fed the cat")
        self.assertEqual(out.count("Not marked yet:"), 1)  # the reply has it already
        self.manager.tools.user_message = None
        self.turn('CALL add_event {"title": "Quilting", "start": "5:30 PM", "duration": "30 minutes"}', "Added.",
                  said="quilting at 5:30")
        self.assertIn("tune the ukulele", MissedTracker(self.sched.state_db).titles_shown(at("17:10")))

    def test_a_status_view_every_slice_while_notices_stay_quiet(self) -> None:
        from unittest.mock import MagicMock
        from thirties_core.background import BackgroundService
        self.earlier(("tune", "Tune the ukulele", "16:00", "16:30"))
        first = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.manager.tools.user_message = None
        second = self.turn('CALL add_event {"title": "Quilting", "start": "5:30 PM", "duration": "30 minutes"}',
                           "Added.", said="quilting at 5:30")
        for out in (first, second):  # nothing ticked: listed again, fresh from the plan, not hidden as "shown"
            self.assertEqual(self.tail(out)[-2:], ["Not marked yet:", "4:00 PM – Tune the ukulele"])
        svc = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=MagicMock(),
                                inference_engine=self.engine, sync_engine=MagicMock())
        svc.today, svc._manager, svc._day = (lambda: DAY), self.manager, DAY
        self.assertEqual(svc._with_missed("🔔 6:00 PM: Dinner", at("17:20")), "🔔 6:00 PM: Dinner")  # no repeat

    def test_a_notice_still_lists_what_no_slice_showed(self) -> None:
        from unittest.mock import MagicMock
        from thirties_core.background import BackgroundService
        self.earlier(("tune", "Tune the ukulele", "16:00", "16:30"))
        svc = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=MagicMock(),
                                inference_engine=self.engine, sync_engine=MagicMock())
        svc.today, svc._manager, svc._day = (lambda: DAY), self.manager, DAY
        self.assertIn("Tune the ukulele", svc._with_missed("🔔 6:00 PM: Dinner", at("17:20")))


if __name__ == "__main__":
    unittest.main()


class TestOneLinePerItemEverywhere(WhatsNextCase):
    """Each line of what's next stays its own line wherever the reply is shown: Telegram, and the stored
    conversation read back after a restart. (The desktop chat: test_gui, ChatPanelTests.)"""

    def reply_with_slice(self) -> tuple:
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Nice, the cat's fed.",
                        said="fed the cat")
        rows = self.tail(out)
        self.assertGreaterEqual(len(rows), 3)
        return out, rows

    def test_telegram_sends_each_line_as_its_own_line(self) -> None:
        from unittest.mock import patch
        from thirties_core.telegram_bridge import TelegramClient
        out, rows = self.reply_with_slice()
        sent = []
        client = TelegramClient("1:X")
        with patch.object(TelegramClient, "_request", lambda self_, method, payload=None, timeout=30.0:
                          sent.append(payload) or {"ok": True, "result": {"message_id": 1}}):
            client.send_message(5, out)
        lines = sent[0]["text"].split("\n")
        for row in rows:
            self.assertIn(row, lines)  # each row on its own line in what Telegram gets (HTML keeps newlines)

    def test_the_stored_conversation_keeps_each_line_after_a_restart(self) -> None:
        out, rows = self.reply_with_slice()
        plan, pending = self.sched.build_day_plan(DAY)
        again = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(),
                                    active_notes=[])  # the app started again: the day's conversation, from the store
        shown = [m["content"] for m in again.history() if m["role"] == "assistant"][-1]
        self.assertEqual(shown, out)
        for row in rows:
            self.assertIn(row, shown.splitlines())
