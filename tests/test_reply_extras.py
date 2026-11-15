"""What Python adds to replies, and the person's say over it (reply_extras.py): what's next after a change, its Not
marked yet list, and the "You're in the … block" line. Asked in chat to stop one, it stops, and the assistant only
says so when it did. Scripted model replies; invented days. DAY is Thu Oct 15."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at
from tests.test_whats_next import WhatsNextCase
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.reply_extras import TODAY_KEY, clear_today, effective
from thirties_core.settings import settings_for

ADD = 'CALL add_event {"title": "Repot the fern", "start": "5:30 PM", "duration": "30 minutes"}'


def sent_messages(call) -> list:
    """The messages of one recorded model call."""
    return call if isinstance(call, list) else list(call.get("messages", []))


def status_lines(out: str) -> list:
    return [line for line in out.splitlines() if line.startswith("You're in the ")]


class TestStoppingWhatsNext(WhatsNextCase):

    def test_asked_to_delete_the_next_section_it_stops_and_stays_stopped(self) -> None:
        out = self.turn(ADD, "The fern's at 5:30.", said="repot the fern at 5:30")
        self.assertIn("Next:", out)
        out = self.turn('CALL set_reply_extras {"part": "whats_next", "show": "off"}', "Done: no more Next list.",
                        said="delete the whole Next section under it")
        self.assertNotIn("Next:", out)
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.assertEqual(out, "Noted.")  # a change, and nothing under the reply
        self.assertEqual(settings_for(self.sched.state_db).get("assistant.after_change"), "nothing")  # Preferences
        again = ConversationManager(self.manager.day_plan, [], [], self.sched, inference_engine=self.engine,
                                    active_notes=[])  # another device or a restart: the same
        self.engine.set_next_response(self.reply('CALL log_outcome {"event": "dinner", "status": "done"}'))
        self.engine.set_next_response(self.reply("Dinner's done."))
        self.assertEqual(again.send_user_message("dinner's done"), "Dinner's done.")

    def test_no_need_to_resend_the_list_and_the_way_back(self) -> None:
        self.turn('CALL set_reply_extras {"part": "whats_next", "show": "off"}', "Okay, I'll leave the list off.",
                  said="no need to resend the list pls")
        out = self.turn(ADD, "The fern's at 5:30.", said="repot the fern at 5:30")
        self.assertNotIn("Next:", out)
        self.turn('CALL set_reply_extras {"part": "whats_next", "show": "on"}', "It's back.",
                  said="show me what's next again")
        out = self.turn('CALL log_outcome {"event": "feed", "status": "done"}', "Noted.", said="fed the cat")
        self.assertIn("Next:", out)

    def test_asking_whats_next_always_works_even_when_its_off(self) -> None:
        self.turn('CALL set_reply_extras {"part": "whats_next", "show": "off"}', "Okay.", said="stop the list")
        out = self.turn("CALL show_schedule {}", "Here's what's left.", said="what's next?")
        self.assertIn("6:00 – Dinner", out)

    def test_only_for_today_when_said_so(self) -> None:
        self.turn('CALL set_reply_extras {"part": "whats_next", "show": "off", "for_today": true}', "Not today.",
                  said="no lists today please")
        self.assertEqual(effective(self.sched.config, self.sched.state_db, DAY)["whats_next"], "nothing")
        self.assertEqual(self.sched.config.assistant.after_change, "next")  # the lasting choice is untouched
        self.assertEqual(effective(self.sched.config, self.sched.state_db, DAY + timedelta(days=1))["whats_next"],
                         "next")  # tomorrow, as before

    def test_a_choice_in_preferences_replaces_todays(self) -> None:
        self.turn('CALL set_reply_extras {"part": "whats_next", "show": "off", "for_today": true}', "Not today.",
                  said="no lists today please")
        settings_for(self.sched.state_db).set("assistant.after_change", "rest")  # what the Preferences row does
        self.sched.config.assistant.after_change = "rest"
        clear_today(self.sched.state_db, "whats_next")
        self.assertEqual(effective(self.sched.config, self.sched.state_db, DAY)["whats_next"], "rest")

    def test_not_marked_yet_can_go_on_its_own(self) -> None:
        set_debug_time(at("19:40"))  # the cat and dinner went by with nothing said
        out = self.turn(ADD.replace("5:30 PM", "8:30 PM"), "Fern at 8:30.", said="fern at 8:30")
        self.assertIn("Not marked yet:", out)
        self.turn('CALL set_reply_extras {"part": "not_marked", "show": "off"}', "Okay.",
                  said="stop listing what's not marked")
        out = self.turn('CALL log_outcome {"event": "glaze", "status": "done"}', "Noted.", said="glazing's done")
        self.assertIn("Next:", out)
        self.assertNotIn("Not marked yet:", out)


class TestWhereTheChoiceIsKept(WhatsNextCase):
    """Every way a slice or a status line is made follows the choice: it's applied where they're built, so a new
    route (a notice, a phone button) gets it without doing anything."""

    def test_the_slice_follows_it_wherever_its_made(self) -> None:
        from thirties_core.whats_next import render
        plan, cfg, db = self.manager.day_plan, self.sched.config, self.sched.state_db
        set_debug_time(at("19:40"))
        self.assertIn("Not marked yet:", render(plan, at("19:40"), config=cfg, db=db))
        self.manager.tools.execute("set_reply_extras", {"part": "not_marked", "show": "off"})
        self.assertNotIn("Not marked yet:", render(plan, at("19:40"), config=cfg, db=db))
        self.manager.tools.execute("set_reply_extras", {"part": "whats_next", "show": "off"})
        self.assertEqual(render(plan, at("19:40"), config=cfg, db=db), "")
        self.assertIn("Not marked yet:", render(plan, at("19:40"), mode="next"))  # asked for outright: whole

    def test_the_block_line_follows_it_where_its_built(self) -> None:
        from thirties_core.messages import render_schedule, status_line
        plan, cfg, db = self.manager.day_plan, self.sched.config, self.sched.state_db
        self.manager.tools.execute("set_reply_extras", {"part": "status_line", "show": "off"})
        self.assertEqual(status_line(plan, at("17:10"), config=cfg, db=db), "")
        self.assertNotIn("You're in the", render_schedule(plan, at("17:10"), config=cfg, db=db))
        self.assertTrue(status_line(plan, at("17:10"), config=cfg, db=db, said="where am i").startswith(
            "You're in the 5:00"))
        self.assertTrue(status_line(plan, at("17:10")).startswith("You're in the"))  # no person to ask: as ever


class TestTheBlockLine(WhatsNextCase):

    def schedule_turn(self, said: str) -> str:
        return self.turn("CALL show_schedule {}", "Here's the plan.", said=said)

    def test_said_once_per_block_unless_asked(self) -> None:
        first = self.schedule_turn("show my plan")
        self.assertEqual(len(status_lines(first)), 1)
        set_debug_time(at("17:20"))
        again = self.turn(ADD, "CALL show_schedule {}", "Fern at 5:30; here's the plan.", said="fern at 5:30")
        self.assertEqual(status_lines(again), [])  # unchanged block: left out
        self.assertIn("5:30 – Repot the fern", again)
        set_debug_time(at("17:40"))
        later = self.schedule_turn("show my plan")
        self.assertEqual(len(status_lines(later)), 1)  # a new block: said again
        asked = self.schedule_turn("where am i right now?")
        self.assertEqual(len(status_lines(asked)), 1)  # asked: always

    def test_stop_saying_block(self) -> None:
        self.turn('CALL set_reply_extras {"part": "status_line", "show": "off"}', "Okay, no block line.",
                  said="skip the half hour thing at the top, only fix the times")
        set_debug_time(at("17:40"))
        out = self.schedule_turn("show my plan")
        self.assertEqual(status_lines(out), [])
        self.assertIn("6:00 – Dinner", out)
        self.assertFalse(settings_for(self.sched.state_db).get("assistant.status_line"))
        self.assertEqual(len(status_lines(self.schedule_turn("where am i?"))), 1)  # asked outright: still answered


class TestSayingSoMatchesDoingSo(WhatsNextCase):

    def test_got_it_without_the_tool_is_sent_back(self) -> None:
        out = self.turn("Got it, no block talk.", 'CALL set_reply_extras {"part": "status_line", "show": "off"}',
                        "Done: the block line is off.", said="stop saying block")
        checks = [m for call in self.engine.call_history for m in sent_messages(call)
                  if m.get("role") == "user" and "set_reply_extras wasn't called" in str(m.get("content"))]
        self.assertTrue(checks)  # the promise was sent back
        self.assertFalse(effective(self.sched.config, self.sched.state_db, DAY)["status_line"])  # and kept
        self.assertEqual(out, "Done: the block line is off.")

    def test_an_ordinary_reply_isnt_mistaken_for_one(self) -> None:
        self.turn(ADD, "The fern's at 5:30, and the rest stays.", said="fern at 5:30")
        checks = [m for call in self.engine.call_history for m in sent_messages(call)
                  if "set_reply_extras wasn't called" in str(m.get("content"))]
        self.assertEqual(checks, [])


class TestOnTelegram(WhatsNextCase):

    def test_stopped_on_the_phone_too_in_the_same_words(self) -> None:
        from thirties_core.config import TelegramConfig
        from thirties_core.telegram_bridge import TelegramClient, ThirtiesTelegramBridge
        self.sched.config.telegram = TelegramConfig(enabled=True, bot_token="123:FAKE", allowed_chat_ids=[7])
        with patch.object(TelegramClient, "get_me", return_value={"username": "bot"}):
            bridge = ThirtiesTelegramBridge(config=self.sched.config, scheduler=self.sched,
                                            joplin_engine=MagicMock(), inference_engine=self.engine,
                                            sync_engine=MagicMock())
        bridge.client = MagicMock()
        bridge.conv_manager = self.manager
        bridge.current_date = DAY
        bridge._check_and_refresh_date = lambda: None
        for r in ('CALL set_reply_extras {"part": "whats_next", "show": "off"}', "Okay, the list is off."):
            self.engine.set_next_response(self.reply(r))
        bridge.handle_message(7, "stop sending the list")
        for r in (ADD, "The fern's at 5:30."):
            self.engine.set_next_response(self.reply(r))
        bridge.handle_message(7, "fern at 5:30")
        sent = bridge.client.send_message.call_args[0][1]
        self.assertEqual(sent, "The fern's at 5:30.")
        self.assertEqual(self.manager.history()[-1]["content"], sent)  # the app's chat says the same


WhatsNextCase.reply = staticmethod(__import__("tests.test_conversation", fromlist=["reply"]).reply)

if __name__ == "__main__":
    unittest.main()
