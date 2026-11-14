"""A conversation's title is the user's own first words, never Thirties' own messages (a system check, the
morning's instruction), which reached the chat's header; titles already stored that way are repaired when the
store opens. Invented messages."""

import unittest
from datetime import date

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.memory import ConversationStore, day_title

CHECK = "[System check, not from the user] This is the user's first message of the day; plan the free time."


class TestConversationTitles(unittest.TestCase):

    def setUp(self) -> None:
        self.sched = make_scheduler()
        self.store = ConversationStore(self.sched.state_db)

    def titles(self) -> list:
        with self.sched.state_db._get_connection() as conn:
            return [r["title"] for r in conn.execute("SELECT title FROM conversations")]

    def test_a_new_conversation_takes_the_users_first_words_never_a_check(self) -> None:
        conv = self.store.create(DAY)
        self.assertEqual(conv.title, day_title(DAY))  # nothing said yet: the day ("Thursday, October 15")
        self.assertEqual(day_title(date(2026, 11, 14)), "Saturday, November 14")
        conv.messages = [{"role": "user", "hidden": True, "content": CHECK}]
        self.store.save(conv)
        self.assertEqual(conv.title, day_title(DAY))  # still the day: that wasn't the user
        conv.messages.append({"role": "user", "content": "plan a quiet evening with some sanding"})
        self.store.save(conv)
        self.assertEqual(conv.title, "plan a quiet evening with some sanding")
        conv.messages.append({"role": "user", "content": "and the kiln check"})
        self.store.save(conv)
        self.assertEqual(conv.title, "plan a quiet evening with some sanding")  # the first words stay

    def test_an_unhidden_check_is_still_never_a_title(self) -> None:
        conv = self.store.create(DAY)
        conv.messages = [{"role": "user", "content": "[Something, not from the user] a note for the model"},
                         {"role": "user", "content": "kiln check at 6"}]
        self.store.save(conv)
        self.assertEqual(conv.title, "kiln check at 6")

    def test_stored_titles_from_a_check_are_repaired_when_the_store_opens(self) -> None:
        conv = self.store.create(DAY)
        conv.messages = [{"role": "user", "hidden": True, "content": CHECK},
                         {"role": "user", "content": "move the bins to 9:30"}]
        self.store.save(conv)
        empty = self.store.create(DAY)
        empty.messages = [{"role": "user", "hidden": True, "content": CHECK}]
        self.store.save(empty)
        with self.sched.state_db._get_connection() as conn:  # as an earlier version stored them
            conn.execute("UPDATE conversations SET title = ?", (CHECK[:60] + "…",))
        with self.assertLogs("thirties_core.memory", level="INFO") as logs:
            ConversationStore(self.sched.state_db)  # the app starting again
        self.assertIn("Repaired 2 conversation title(s)", "\n".join(logs.output))
        self.assertEqual(self.store.get(conv.id).title, "move the bins to 9:30")
        self.assertEqual(self.store.get(empty.id).title, day_title(DAY))
        self.assertEqual(ConversationStore(self.sched.state_db).repair_titles(), 0)  # once is enough

    def test_no_title_in_the_store_starts_with_a_bracket_after_a_morning(self) -> None:
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        engine = MockInferenceEngine()
        plan, pending = self.sched.build_day_plan(DAY)
        manager = ConversationManager(plan, [], pending, self.sched, inference_engine=engine, active_notes=[])
        manager.brain.messages.append({"role": "user", "hidden": True, "content": CHECK})  # the morning's own
        engine.set_next_response(reply("Morning! Here's your day."))
        manager.send_user_message("good morning")
        self.assertTrue(self.titles())
        self.assertFalse([t for t in self.titles() if t.startswith("[")], self.titles())
        self.assertIn("good morning", self.titles())


if __name__ == "__main__":
    unittest.main()
