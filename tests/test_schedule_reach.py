"""How the schedule reaches the user without being asked for: what's next under a bell or a check-in (when it says
something new), the menu on the phone, the slice after a failed change, a reply that talked the plan over, a tick in
the app's day panel, today's list items with no time, open stretches, where the morning's carried items went, and
the evening offer to move what's behind to tonight. Invented items; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep
from 11 PM)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from thirties_core.clock import format_clock
from tests.fixtures import DAY, at, make_scheduler, mark_up
from tests.test_one_conversation import CHAT, OneConversationCase, Phone
from tests.test_whats_next import WhatsNextCase
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


def lines(text: str) -> list:
    return [line for line in text.splitlines() if line.strip()]


class ServiceCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("17:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"
        self.sched.config.assistant.after_change = "next"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.desktop = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.svc.bridge.broadcast.return_value = True
        for sid, title, start, end in (("tx", "Sort the receipts", "17:00", "18:00"),
                                       ("din", "Dinner", "18:00", "19:00"),
                                       ("film", "Film night", "20:00", "21:30")):
            self.add(sid, title, start, end)

    def add(self, sid, title, start, end, category=SpanCategory.ACTIVITY):
        self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), category))
        self.svc._manager = None

    def last(self, kind=None):
        return [b for k, b in self.desktop if kind is None or k == kind][-1]


class TestTheBellCarriesWhatsNext(ServiceCase):
    """Coming back to the phone after bells and check-ins, none of which said what's next."""

    def test_the_bell_ends_with_whats_next_and_its_free_time(self):
        self.svc.starts(at("17:00"))
        self.assertEqual(self.last("start"), "🔔 5:00 PM: Sort the receipts\n\nNext:\n6:00 PM – Dinner (until 7:00 PM)\n"
                                             "Free 7:00 PM – 8:00 PM\n8:00 PM – Film night\nFree 9:30 PM – 11:00 PM")  # every time with AM/PM
        self.assertNotIn("Now:", self.last("start"))  # the bell says what's on now itself

    def test_not_repeated_every_half_hour_while_nothing_changed(self):
        self.svc.starts(at("17:00"))
        self.svc.starts(at("18:00"))
        self.assertEqual(self.last("start").split("\n\n")[0], f"🔔 {format_clock(at('18:00'))}: Dinner")  # the 5:00 one still holds
        self.assertNotIn("Next:", self.last("start"))  # just the bell, and what fell behind since (as before)

    def test_again_once_the_plan_changes(self):
        self.svc.starts(at("17:00"))
        self.add("call", "Call the plumber", "18:00", "18:15")  # added in the app, or by a turn
        self.svc.starts(at("18:00"))
        self.assertIn("Next:", self.last("start"))

    def test_again_once_the_last_one_ran_out_or_two_hours_passed(self):
        self.svc.starts(at("17:00"))  # it listed up to Film night at 8:00
        self.svc.starts(at("20:00"))
        self.assertTrue(self.last("start").startswith("🔔 8:00 PM: Film night\n\nNothing else is planned today.\n"
                                                      "Free 9:30 PM – 11:00 PM"), self.last("start"))  # it ran out: anew
        self.assertIn("Not marked yet:\n5:00 PM – Sort the receipts\n6:00 PM – Dinner", self.last("start"))
        from thirties_core.whats_next import SentSummaries
        plan = self.svc.manager().day_plan
        sent = SentSummaries(self.sched.state_db)
        sent.mark(plan, at("12:00"))
        self.assertFalse(sent.due(plan, at("13:00")))
        self.assertTrue(sent.due(plan, at("14:00")))  # two hours on

    def test_a_reply_that_showed_it_counts_as_shown(self):
        from thirties_core.whats_next import SentSummaries
        SentSummaries(self.sched.state_db).mark(self.svc.manager().day_plan, at("16:55"))
        self.svc.starts(at("17:00"))
        self.assertEqual(self.last("start"), "🔔 5:00 PM: Sort the receipts")

    def test_the_check_in_ends_with_whats_next_without_asking_twice(self):
        self.svc.check_ins(at("18:15"))
        text = self.last("checkin")
        self.assertTrue(text.startswith("👋 Just checking in: have you started Dinner yet, or are you still on Sort "
                                        "the receipts?\n\nNext:\n"), text)  # it names what's on now: no "Now" line
        self.assertNotIn("Not marked yet", text)  # what it asks about isn't listed under it again
        self.assertIn("8:00 PM – Film night", lines(text))

    def test_the_setting_nothing_keeps_notices_as_they_were(self):
        self.sched.config.assistant.after_change = "nothing"
        self.svc.starts(at("17:00"))
        self.assertEqual(self.last("start"), "🔔 5:00 PM: Sort the receipts")

    def test_plain_lines_no_bullets(self):
        self.add("old", "Water the ferns", "16:00", "16:15")
        self.svc.starts(at("17:00"))
        self.assertFalse(any(line.startswith("- ") for line in lines(self.last("start"))))
        self.assertIn("4:00 PM – Water the ferns", lines(self.last("start")))  # under Not marked yet


class TestTheUsersSayOverIt(ServiceCase):
    """Asked to stop what's next, its Not marked yet list or the block line (reply_extras.py), notices stop them
    too; /now and the words "what's next" always show everything."""

    def extras(self, part, show, for_today=True):
        what = {"whats_next": "what's next", "not_marked": "what's not marked yet",
                "status_line": "which block i'm in", "all": "the extra lines at the end"}[part]
        manager = self.svc.manager()
        manager.tools.user_message = f"stop {what}" if show == "off" else f"bring back {what} again"  # as asked
        manager.execute_tool("set_reply_extras", {"part": part, "show": show, "for_today": for_today})

    def test_whats_next_off_leaves_the_bell_alone(self):
        self.extras("whats_next", "off")
        self.svc.starts(at("17:00"))
        self.assertEqual(self.last("start"), "🔔 5:00 PM: Sort the receipts")

    def test_not_marked_off_leaves_the_list_out_of_every_notice(self):
        self.add("old", "Water the ferns", "16:00", "16:15")
        self.extras("not_marked", "off")
        self.svc.starts(at("17:00"))
        self.assertIn("Next:", self.last("start"))
        self.assertNotIn("Water the ferns", self.last("start"))
        self.add("call", "Call the plumber", "18:00", "18:15")
        from thirties_core.whats_next import SentSummaries
        SentSummaries(self.sched.state_db).mark(self.svc.manager().day_plan, at("17:59"))
        self.svc.starts(at("18:00"))  # just the bell: no list under it either
        self.assertEqual(self.last("start"), "🔔 6:00 PM: Call the plumber, Dinner")

    def test_block_line_off_means_no_now_line_in_a_check_in(self):
        self.extras("status_line", "off")
        self.svc.check_ins(at("21:45"))
        text = self.last("checkin")
        self.assertIn("Nothing else is planned today.", text)
        self.assertNotIn("Now:", text)

    def test_asked_for_it_always_shows(self):
        from thirties_core import whats_next
        self.extras("all", "off")
        tools = self.svc.manager().tools
        self.assertEqual(whats_next.summary(tools, at("17:10")), "")
        self.assertIn("Next:", whats_next.summary(tools, at("17:10"), "next"))
        self.assertIn("Now: Sort the receipts", whats_next.summary(tools, at("17:10"), "next"))


class TestTicksInTheDayPanel(ServiceCase):
    """10-25: ticks made in the app's day panel never reached the phone."""

    def tick(self, sid, status="done"):
        from thirties_core.plan_log import caused_by
        with caused_by("Day page"):  # as the window does (window._run_day_tool)
            self.svc.manager().execute_tool("log_outcome", {"event": sid, "status": status})

    def test_one_message_once_the_ticking_stops_with_whats_next(self):
        self.svc.panel_ticks(at("17:20"))  # the first look: only what's ticked from now on
        self.tick("tx")
        with patch("thirties_core.background.PANEL_QUIET", 3600):
            self.svc.panel_ticks(at("17:21"))
        self.assertEqual(self.desktop, [])  # still ticking: wait
        self.tick("din", "skipped")
        with patch("thirties_core.background.PANEL_QUIET", 0):
            self.svc.panel_ticks(at("17:25"))
            self.svc.panel_ticks(at("17:26"))
        self.assertEqual(self.desktop, [])  # never a desktop notification: the user did it there
        self.svc.bridge.broadcast.assert_called_once()
        sent = self.svc.bridge.broadcast.call_args[0][0]
        self.assertTrue(sent.startswith("Marked done in the app: Sort the receipts. Skipped in the app: Dinner."), sent)
        self.assertIn("Next:", sent)
        self.assertIn("8:00 PM – Film night", lines(sent))
        self.assertIn("Marked done in the app", self.svc.manager().history()[-1]["content"])  # one conversation

    def test_nothing_without_a_phone_or_when_unticked_again(self):
        self.svc.panel_ticks(at("17:20"))
        self.tick("tx")
        from thirties_core.plan_log import caused_by
        span = next(s for s in self.svc.manager().day_plan.spans if s.id == "tx")
        span.status = SpanStatus.PLANNED
        with caused_by("Day page"):  # unticked again
            self.sched.state_db.upsert_span(span)
        with patch("thirties_core.background.PANEL_QUIET", 0):
            self.svc.panel_ticks(at("17:25"))
        self.svc.bridge.broadcast.assert_not_called()
        self.svc.bridge = None
        self.tick("din")
        with patch("thirties_core.background.PANEL_QUIET", 0):
            self.svc.panel_ticks(at("17:30"))
        self.assertEqual(self.desktop, [])


class TestTheEveningCatchUp(ServiceCase):
    """11-14: five items listed as not marked in the evening, and the user had to ask to have them put back."""

    def setUp(self):
        super().setUp()
        for sid, title, start in (("fern", "Water the ferns", "10:00"), ("mail", "Post the parcel", "10:30"),
                                  ("bike", "Pump the bike tyres", "16:00")):
            self.add(sid, title, start, (at(start) + timedelta(minutes=15)).strftime("%H:%M"))
        for sid in ("tx", "din"):
            span = next(s for s in self.svc.manager().day_plan.spans if s.id == sid)
            span.status = SpanStatus.DONE
            self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        set_debug_time(at("19:10"))

    def offer(self):
        self.svc.evening_catch_up(at("19:10"))
        return self.last("catch_up")

    def test_offered_once_with_one_line(self):
        self.svc.evening_catch_up(at("17:30"))  # not before the evening
        self.assertEqual(self.desktop, [])
        text = self.offer()
        self.assertEqual(text, "Not marked yet:\n10:00 AM – Water the ferns\n10:30 AM – Post the parcel\n"
                               "4:00 PM – Pump the bike tyres\n\nMove these 3 to tonight?")
        self.svc.evening_catch_up(at("19:20"))
        self.assertEqual(len(self.desktop), 1)

    def test_a_yes_moves_them_to_tonight_and_says_whats_next(self):
        self.offer()
        out = self.svc.manager().send_user_message("yes", via="telegram")
        self.assertTrue(out.startswith("Moved to tonight: Water the ferns, Post the parcel and Pump the bike tyres."),
                        out)
        plan = self.svc.manager().day_plan
        moved = {s.id: s for s in plan.spans if s.id in ("fern", "mail", "bike")}
        self.assertTrue(all(s.start_dt >= at("19:30") and s.status == SpanStatus.PLANNED for s in moved.values()))
        self.assertTrue(all(s.start_dt.minute in (0, 15, 30, 45) for s in moved.values()))  # block times, stacked
        self.assertFalse(any(moved[a].start_dt < moved[b].end_dt and moved[b].start_dt < moved[a].end_dt
                             for a in moved for b in moved if a < b))  # one after another, never on top
        self.assertIn("Next:", out)
        stored = [m["content"] for m in self.svc.manager().history()]
        self.assertEqual(stored[-2:], ["yes", out])  # in the conversation, as on the phone
        from thirties_core import catch_up
        self.assertIsNone(catch_up.pending(self.svc.manager().tools, at("19:11")))  # answered: a later yes isn't it

    def test_anything_else_lets_it_go(self):
        self.offer()
        from thirties_core import catch_up
        self.assertEqual(catch_up.answer(self.svc.manager(), "hmm, maybe later", "app"), "")
        catch_up.let_go(self.svc.manager())  # as every message that isn't the yes does (conversation)
        self.assertEqual(catch_up.answer(self.svc.manager(), "yes", "app"), "")  # that yes is about something else
        self.assertIsNone(catch_up.pending(self.svc.manager().tools, at("19:12")))

    def test_a_thumbs_up_is_a_yes(self):
        from thirties_core import catch_up
        from thirties_core.background import YES_KINDS
        self.assertIn("catch_up", YES_KINDS)  # a 👍 reaction on the offer comes in as "👍"
        self.assertTrue(catch_up.YES_RE.match("👍"))
        self.assertFalse(catch_up.YES_RE.match("yes but not the parcel"))


class TestOneTapOnThePhone(OneConversationCase):
    """/now and /day went unused: not in Telegram's menu, and /now didn't say what's next."""

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.after_change = "next"
        self.calls = []
        phone = self.phone

        def request(method, payload=None, timeout=30.0):
            self.calls.append((method, payload))
            return Phone.request(phone, method, payload, timeout)
        self.client._request = request

    def test_the_commands_are_in_telegrams_menu_once(self):
        self.svc.bridge.register_commands()
        self.svc.bridge.register_commands()
        menus = [p for m, p in self.calls if m == "setMyCommands"]
        self.assertEqual(len(menus), 1)
        self.assertEqual([c["command"] for c in menus[0]["commands"]][:2], ["now", "day"])
        self.assertEqual(menus[0]["commands"][0]["description"], "What's next")

    def test_the_button_and_now_answer_with_whats_next_and_no_model_turn(self):
        with patch.object(self.svc.bridge, "_get_current_time", return_value=at("17:10")):
            self.svc.bridge.handle_updates([self.update("What's next", 500)])
            self.svc.bridge.handle_updates([self.update("/now", 501)])
        self.assertEqual(self.engine.call_history, [])  # Python answers it, at once
        first, second = self.said()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("Now: Taxes"), first)
        self.assertEqual([t for r, t, _ in self.stored() if r == "user"], ["What's next", "/now"])
        self.assertSymmetric()

    def test_the_button_shows_everything_even_with_every_extra_off(self):
        a = self.sched.config.assistant
        a.after_change, a.after_change_unmarked, a.status_line = "nothing", False, False
        self.sched.state_db.upsert_span(EventSpan("ol", "Oil the hinges", at("16:00"), at("16:15"),
                                                  SpanCategory.ACTIVITY))
        with patch.object(self.svc.bridge, "_get_current_time", return_value=at("17:10")):
            self.svc.bridge.handle_updates([self.update("What's next", 510)])
        said = self.said()[-1]
        self.assertTrue(said.startswith("Now: Taxes"), said)
        self.assertIn("Not marked yet:", said)


class TestRepliesEndWithIt(WhatsNextCase):
    """A reply that apologised or settled a dispute, and today's list items with no time."""

    def test_an_apology_ends_with_whats_next(self):
        out = self.turn("Sorry about that, the movie is at 8 as you said.", said="the movie is at 8, not 9")
        self.assertIn("Next:", out)
        self.assertTrue(out.startswith("Sorry about that"))

    def test_a_question_about_the_plan_answered_with_its_items(self):
        # the reason comes from what item_history records, never a story (reply_checks)
        out = self.turn('CALL item_history {"item": "Dinner"}', "Dinner is at 6 because Glazing needs the evening light.",
                        said="why is dinner so early?")
        self.assertIn("Next:", out)

    def test_a_plain_question_or_a_question_back_gets_none(self):
        self.assertEqual(self.turn("You've got dinner at 6.", said="when's dinner?"), "You've got dinner at 6.")
        out = self.turn("Sorry, do you mean the movie or the glazing?", said="why is that one late?")
        self.assertNotIn("Next:", out)  # the question comes first

    def test_list_items_with_no_time_are_in_it(self):
        with patch("thirties_core.whats_next.untimed_for",
                   return_value=[("On today's list, no time yet:", ["Return the drill", "Buy stamps"]),
                                 ("If there's time:", ["Sort the photos"])]):
            out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        text = out.split("\n\n", 1)[1]
        self.assertIn("On today's list, no time yet:\nReturn the drill\nBuy stamps", text)
        self.assertIn("If there's time:\nSort the photos", text)


class TestPieces(WhatsNextCase):

    def test_free_stretches_of_an_hour_or_more(self):
        from thirties_core.whats_next import free_line, free_stretches
        plan = self.manager.day_plan
        self.assertEqual(free_stretches(plan, at("17:10")), [(at("22:00"), at("23:00"))])  # to bedtime
        self.assertEqual(free_stretches(plan, at("12:10"))[0], (at("16:00"), at("17:00")))  # after Work
        self.assertEqual(free_line(at("12:30"), at("15:00")), "Free 12:30 PM – 3:00 PM")
        self.assertEqual(free_line(at("11:30"), at("13:00")), "Free 11:30 AM – 1:00 PM")
        self.assertEqual(free_line(at("17:00"), at("18:00"), at("17:10")), "Free until 6:00 PM")

    def test_a_done_frees_its_time(self):
        from thirties_core.whats_next import render
        plan = self.manager.day_plan
        next(s for s in plan.spans if s.id == "glaze").status = SpanStatus.DONE
        self.assertIn("Free 7:00 PM – 8:00 PM", lines(render(plan, at("17:10"))))

    def test_untimed_lines(self):
        from thirties_core.whats_next import untimed_lines
        self.assertEqual(untimed_lines("If there's time:", [f"Thing {i}" for i in range(7)]).splitlines(),
                         ["If there's time:"] + [f"Thing {i}" for i in range(7)])
        self.assertEqual(untimed_lines("If there's time:", []), "")

    def test_where_the_mornings_carried_items_went(self):
        from thirties_core.messages import carried_where
        plan = self.manager.day_plan
        self.assertEqual(carried_where(plan, {"title": "Movie", "when": "this week"}), "today at 8:00 PM")
        self.assertEqual(carried_where(plan, {"title": "Mend the kite", "when": "this week"}),
                         "not today, later this week")  # 11-12: "kept for this week" read as today's
        self.assertEqual(carried_where(plan, {"title": "Mend the kite", "when": "tomorrow"}), "tomorrow")
        self.assertEqual(carried_where(plan, {"title": "Mend the kite", "when": "today"}), "today, no time yet")


if __name__ == "__main__":
    unittest.main()
