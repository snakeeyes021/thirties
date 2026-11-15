"""All conversations: every conversation by day, newest first, with a search (thirties_core/conversation_index)."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY, TZ, make_scheduler
from thirties_core.conversation_index import all_conversations, day_heading
from thirties_core.memory import ConversationStore


class TestAllConversations(unittest.TestCase):

    def setUp(self):
        self.store = ConversationStore(make_scheduler().state_db)

    def say(self, day, *messages, summary=""):
        conv = self.store.create(day)
        conv.messages = [{"role": r, "content": c} for r, c in messages]
        conv.summary = summary
        self.store.save(conv)
        return conv

    def test_by_day_newest_first_and_nothing_empty(self):
        a = self.say(DAY - timedelta(days=2), ("user", "Plan the shed clear-out"), ("assistant", "Saturday 10 AM."))
        b = self.say(DAY, ("user", "What's next?"), ("assistant", "Lunch at noon."))
        c = self.say(DAY, ("user", "Move lunch to 1"), ("user", "and dinner to 7"))
        self.say(DAY)  # nothing said: not listed
        self.say(DAY, ("user", "[System check] not from the user"))  # Thirties' own: not a conversation of theirs
        days = all_conversations(self.store, tz=TZ)
        self.assertEqual([d for d, _ in days], [DAY, DAY - timedelta(days=2)])
        self.assertEqual([e.id for e in days[0][1]], [c.id, b.id])  # newest first within the day
        self.assertEqual(days[0][1][0].messages, 2)
        self.assertEqual(days[0][1][0].title, "Move lunch to 1")
        self.assertEqual(days[1][1][0].id, a.id)

    def test_search_keeps_conversations_with_every_word(self):
        self.say(DAY, ("user", "Plan the shed clear-out"), ("assistant", "Saturday 10 AM."))
        self.say(DAY - timedelta(days=1), ("user", "Book the dentist"), summary="Dentist on Tuesday at 3.")
        self.say(DAY - timedelta(days=3), ("user", "Shed or garden first?"), ("assistant", "The garden: rain later."))
        found = all_conversations(self.store, "shed saturday", TZ)
        self.assertEqual([e.title for _, es in found for e in es], ["Plan the shed clear-out"])
        found = all_conversations(self.store, "TUESDAY", TZ)  # the summary counts, any case
        self.assertEqual([e.title for _, es in found for e in es], ["Book the dentist"])
        self.assertEqual(all_conversations(self.store, "boat", TZ), [])

    def test_day_headings(self):
        self.assertEqual(day_heading(DAY, DAY), "Today")
        self.assertEqual(day_heading(DAY - timedelta(days=1), DAY), "Yesterday")
        self.assertEqual(day_heading(DAY - timedelta(days=2), DAY), "Tuesday, October 13")
        self.assertEqual(day_heading(date(2025, 12, 31), DAY), "Wednesday, December 31, 2025")


if __name__ == "__main__":
    unittest.main()
