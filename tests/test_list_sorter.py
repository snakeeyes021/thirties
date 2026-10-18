"""Guessing list roles and item urgency: what the models see, what comes back, what gets stored."""

import json
import unittest
from datetime import date

from tests.fixtures import make_scheduler
from thirties_core.brains.list_sorter import FrontierListSorter, GemmaListSorter, note_outline
from thirties_core.lists import ListCatalog, sort_lists
from thirties_core.models import TaskItem

SHOPPING = """- [ ] Thermals for THIS winter

- [ ] Two more suits
- [ ] NAS upgrades
    - [ ] 16 HDDs
### Do I want?:
- [x] Vocal trainer
"""
TODAY = date(2026, 10, 18)


def item(tid, title, note="n_shop"):
    return TaskItem(id=tid, source_notebook="Tasks", title=title, is_checkbox_item=True, parent_note_title="Shopping",
                    parent_note_id=note)


class FakeRuntime:
    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def json_call(self, system, user, schema, max_output_tokens=256):
        self.asked.append(user)
        return self.answers.pop(0) if self.answers else {}


class FakeEngine:
    def __init__(self, replies):
        self.replies, self.asked = list(replies), []

    def chat(self, messages, tools=None):
        self.asked.append(messages[-1]["content"])
        return {"role": "assistant", "content": self.replies.pop(0) if self.replies else "", "tool_calls": []}


class TestOutline(unittest.TestCase):

    def test_the_note_as_its_owner_sees_it(self):
        out = note_outline(SHOPPING, mark="Two more suits")
        self.assertEqual(out.splitlines(), ["- Thermals for THIS winter", "(gap)", "- → Two more suits",
                                            "- NAS upgrades", "    - 16 HDDs", "## Do I want?:",
                                            "- Vocal trainer (ticked)"])
        numbered = note_outline(SHOPPING, numbered=["Thermals for THIS winter", "NAS upgrades"])
        self.assertIn("- 1. Thermals for THIS winter", numbered)
        self.assertIn("- 2. NAS upgrades", numbered)


class TestSorters(unittest.TestCase):

    def test_gemma_one_item_per_call_with_the_item_marked(self):
        rt = FakeRuntime([{"urgency": "soon"}, {"urgency": "bogus"}])
        out = GemmaListSorter(rt).urgencies({"title": "Shopping", "body": SHOPPING}, "list",
                                            [item("a", "Thermals for THIS winter"), item("b", "Two more suits")], TODAY)
        self.assertEqual(out, {"a": ("soon", None)})  # a bad answer is left for next time
        self.assertIn("→ Thermals for THIS winter", rt.asked[0])

    def test_gemmas_now_needs_a_sign_of_urgency(self):
        rt = FakeRuntime([{"urgency": "now"}, {"urgency": "now"}])
        out = GemmaListSorter(rt).urgencies({"title": "Errands", "body": ""}, "list",
                                            [item("a", "Message Brad back"), item("b", "Wash sweaters")], TODAY)
        self.assertEqual(out, {"a": ("now", None), "b": ("soon", None)})

    def test_gemini_a_whole_note_per_call(self):
        reply = json.dumps({"items": [{"n": 1, "urgency": "now", "due": "2026-11-01"}, {"n": 2, "urgency": "whenever"},
                                      {"n": 9, "urgency": "soon"}]})
        eng = FakeEngine([f"Here you go:\n```json\n{reply}\n```"])
        out = FrontierListSorter(eng).urgencies({"title": "Shopping", "body": SHOPPING}, "list",
                                                [item("a", "Thermals for THIS winter"), item("b", "Two more suits")],
                                                TODAY)
        self.assertEqual(out, {"a": ("now", date(2026, 11, 1)), "b": ("whenever", None)})
        self.assertIn("1. Thermals for THIS winter", eng.asked[0])
        self.assertIn("Sunday, October 18, 2026", eng.asked[0])

    def test_roles(self):
        self.assertEqual(GemmaListSorter(FakeRuntime([{"role": "menu"}])).role({"title": "Self", "body": ""}), "menu")
        self.assertIsNone(FrontierListSorter(FakeEngine(['{"role": "nonsense"}'])).role({"title": "X", "body": ""}))


class TestSortLists(unittest.TestCase):

    def test_guesses_whats_missing_and_retries_what_failed(self):
        catalog = ListCatalog(make_scheduler().state_db)
        notes = [{"id": "n_shop", "title": "Shopping", "body": SHOPPING}, {"id": "n_x", "title": "Mystery", "body": ""}]
        tasks = [item("a", "Thermals for THIS winter"), item("b", "Two more suits")]
        sorter = GemmaListSorter(FakeRuntime([{"role": "list"}, {}, {"urgency": "soon"}, {}]))
        counts = sort_lists(catalog, notes, tasks, sorter, TODAY)
        self.assertEqual(counts, {"roles": 1, "items": 2})
        self.assertIsNone(catalog.role("n_x"))  # no answer: asked again next time
        u = catalog.urgencies()
        self.assertEqual((u["a"]["urgency"], u["a"]["source"]), ("soon", "guess"))
        self.assertEqual(u["b"]["urgency"], "whenever")  # skipped within an answered note: not asked forever
        again = GemmaListSorter(FakeRuntime([{"role": "ignore"}]))
        self.assertEqual(sort_lists(catalog, notes, tasks, again, TODAY), {"roles": 1, "items": 0})


if __name__ == "__main__":
    unittest.main()
