"""The intent set and its destructive guard (intents.py): a destructive action runs only on the user's explicit
words; otherwise its safe reading runs, or it's held back with a question. Fixtures are invented."""

import unittest

from tests.test_today import TodayTestCase
from thirties_core.intents import INTENTS, destructive_intent, explicit, guard


class GuardTest(unittest.TestCase):
    def test_archive_without_explicit_words_becomes_a_drop(self):
        tool, args, note = guard("leftover_action", {"item": "Call the bank", "action": "archive"}, "drop the bank one")
        self.assertEqual((tool, args["action"]), ("leftover_action", "drop"))
        self.assertIn("archive it", note)

    def test_explicit_words_run_it(self):
        for said in ("archive it", "delete the bank one", "drop it, I'm never doing that", "get rid of it for good"):
            tool, args, note = guard("leftover_action", {"item": "x", "action": "archive"}, said)
            self.assertEqual((args["action"], note), ("archive", ""), said)

    def test_the_apps_own_buttons_are_explicit(self):
        tool, args, note = guard("leftover_action", {"item": "x", "action": "archive"}, None)
        self.assertEqual((args["action"], note), ("archive", ""))

    def test_forgetting_and_hiding_a_calendar_need_asking(self):
        tool, _, note = guard("forget", {"note": "n1"}, "lunch was fine today")
        self.assertEqual(tool, "")
        self.assertTrue(note.startswith("NOT_DONE"))
        self.assertEqual(guard("forget", {"note": "n1"}, "forget that I hate mornings")[0], "forget")
        self.assertEqual(guard("set_calendar_mode", {"calendar": "Team", "mode": "ignore"}, "busy day")[0], "")
        self.assertEqual(guard("set_calendar_mode", {"calendar": "Team", "mode": "ignore"},
                               "that calendar isn't mine")[0], "set_calendar_mode")
        self.assertEqual(guard("set_calendar_mode", {"calendar": "Team", "mode": "mine"}, "busy day")[0],
                         "set_calendar_mode")  # not destructive

    def test_only_these_are_destructive(self):
        self.assertEqual({i.name for i in INTENTS.values() if i.destructive}, {"archive", "forget", "hide_calendar"})
        self.assertIsNone(destructive_intent("remove_event", {"event": "x"}))  # off the plan, never off a list
        self.assertIsNone(destructive_intent("leftover_action", {"action": "drop"}))
        self.assertFalse(explicit("archive", "drop it"))


class CatalogueTest(TodayTestCase):
    def test_every_intent_names_real_tools(self):
        known = set(self.t.assistant.handlers()) | {"add_event", "update_event", "remove_event", "swap_events",
                                                    "clear_time", "find_open_time", "list_schedule", "set_item_facts",
                                                    "push_back", "show_schedule"}
        for intent in INTENTS.values():
            for tool in intent.tools:
                self.assertIn(tool, known, f"{intent.name}: {tool}")

    def test_the_guard_runs_where_tools_run(self):
        self.t.user_message = "lunch was fine today"
        try:
            out = self.t.execute("forget", {"note": "anything"})
        finally:
            self.t.user_message = None
        self.assertTrue(out.startswith("NOT_DONE"), out)


if __name__ == "__main__":
    unittest.main()
