"""A long reply is never cut inside a word: one too long for a Telegram message goes as several, split where a
paragraph or line ends, and nothing is dropped. The simulated phone's preview stops at a line or a word too."""

import unittest
from unittest.mock import patch

from tests.study import PHONE_CUT, phone_view
from thirties_core.telegram_bridge import TelegramClient

PARAGRAPH = ("Your errands are spread through the afternoon so the evening stays open: the pharmacy pickup at "
             "2:00, the library returns at 3:30 and the bike tune-up at 4:00. ")


def long_reply(paragraphs: int = 60) -> str:
    return "\n\n".join(f"{k}. {PARAGRAPH * 2}".strip() for k in range(paragraphs))


class TestTelegramSplitsWithoutCutting(unittest.TestCase):
    def setUp(self):
        self.client = TelegramClient("123456:FAKE_TOKEN")

    def test_a_long_reply_goes_as_several_messages_split_at_paragraphs(self):
        text = long_reply()
        self.assertGreater(len(text), 4000)
        sent = []
        with patch.object(TelegramClient, "_send_chunk", lambda _self, chat, chunk, mode, key=None: sent.append(chunk) or True):
            self.assertTrue(self.client.send_message(42, text))
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(c) <= 4000 for c in sent))
        self.assertEqual("".join(sent), text)  # nothing dropped
        for chunk in sent[:-1]:
            self.assertTrue(chunk.endswith("\n\n"), chunk[-40:])  # each one ends where a paragraph does

    def test_one_long_paragraph_splits_between_words(self):
        text = (PARAGRAPH * 60).strip()
        self.assertNotIn("\n", text)
        chunks = self.client._chunk_text(text, max_len=500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(" "), chunk[-20:])  # never inside a word
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_lines_before_words(self):
        text = "\n".join(f"{k}:00 Something planned for this hour of the day" for k in range(1, 40))
        chunks = self.client._chunk_text(text, max_len=300)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"), chunk[-30:])

    def test_only_a_word_longer_than_a_message_is_ever_split(self):
        text = "see " + "x" * 120 + " then"
        chunks = self.client._chunk_text(text, max_len=50)
        self.assertEqual("".join(chunks), text)


class TestThePhonePreview(unittest.TestCase):
    def test_a_long_message_is_cut_where_a_line_ends(self):
        text = "\n".join(f"{k}:30 A step of the day with a name of some length" for k in range(1, 30))
        shown = phone_view(text)
        self.assertTrue(shown.endswith(" … [Read more]"))
        kept = shown[:-len(" … [Read more]")]
        self.assertTrue(text.startswith(kept))
        self.assertEqual(text[len(kept)], "\n")
        self.assertLessEqual(len(kept), PHONE_CUT)

    def test_one_long_line_is_cut_between_words(self):
        text = (PARAGRAPH * 10).strip()
        kept = phone_view(text)[:-len(" … [Read more]")]
        self.assertTrue(text.startswith(kept))
        self.assertIn(text[len(kept)], " \n")  # the next character is a space: no word cut in half


if __name__ == "__main__":
    unittest.main()
