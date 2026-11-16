"""The on-device brain's own answers follow the message rules too: lists as plain lines (never "- " bullets), and the
whole day as a record, one line per item with its start and end (messages.whole_day_lines), never a row per half
hour. Invented items; no model is called (a scripted runtime)."""

from tests.test_slm_brain import SlmTestCase
from thirties_core.brains.slm.parse import Action
from thirties_core.models import TaskItem


class TestPlainLines(SlmTestCase):

    def ask(self, topic, message=None):
        self.brain._message = message or topic
        out, _ = self.brain._do_question(Action("question", {"topic": topic}))
        return out

    def assertPlain(self, out):
        self.assertFalse(any(line.lstrip().startswith("- ") for line in out.splitlines()), out)

    def test_free_time(self):
        out = self.ask("when am I free today")
        self.assertIn("\n", out)  # several windows: a list
        self.assertPlain(out)

    def test_a_named_list(self):
        self.brain.tasks += [TaskItem(id="g1", source_notebook="1. Tasks", title="Weed the beds",
                                      parent_note_title="Garden", parent_note_id="ng"),
                             TaskItem(id="g2", source_notebook="1. Tasks", title="Turn the compost",
                                      parent_note_title="Garden", parent_note_id="ng")]
        self.brain._list_named = lambda message: "Garden"
        out = self.ask("what's on my garden list")
        self.assertEqual(out, "On Garden:\nWeed the beds\nTurn the compost")

    def test_some_open_items(self):
        out = self.ask("my tasks")
        self.assertTrue(out.startswith("Some open items:\n"), out)
        self.assertPlain(out)


class TestTheWholeDay(SlmTestCase):

    def test_one_line_per_item_with_its_start_and_end(self):
        self.m.tools.execute("add_event", {"title": "Paint the fence", "start": "8:00 AM", "end": "11:30 AM"})
        self.m.tools.execute("add_event", {"title": "Call the vet", "start": "9:00 AM", "duration": "30 min"})
        out = self.brain._schedule_answer(None, "show me the whole day")
        fence = [line for line in out.splitlines() if "Paint the fence" in line]
        self.assertEqual(len(fence), 1, out)  # never a row per half hour
        self.assertIn("8:00 AM – 11:30 AM", fence[0])
        vet = [line for line in out.splitlines() if "Call the vet" in line]
        self.assertEqual(len(vet), 1, out)
        self.assertIn("9:00 AM – 9:30 AM", vet[0])


if __name__ == "__main__":
    import unittest
    unittest.main()
