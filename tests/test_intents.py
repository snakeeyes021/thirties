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


class PipelineTest(TodayTestCase):
    """The on-device brain's actions are the shared intents, and Gemini can run on the same pipeline."""

    def test_every_on_device_action_is_an_intent(self):
        from thirties_core.brains.slm.parse import ACTION_INTENT, ACTION_TYPES
        self.assertEqual(set(ACTION_INTENT), set(ACTION_TYPES))
        self.assertTrue(set(ACTION_INTENT.values()) <= set(INTENTS))

    def test_gemini_on_the_pipeline_answers_through_the_same_steps(self):
        import json
        from thirties_core.brains import make_brain
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.cloud_runtime import CloudRuntime
        from thirties_core.inference import MockInferenceEngine
        engine = MockInferenceEngine()
        replies = iter([json.dumps({"actions": [{"type": "add", "title": "Water the ferns", "start": "5:30 PM",
                                                 "duration": "30 minutes"}]})] + ["Done."] * 10)
        engine.chat = lambda messages, tools=None: {"role": "assistant", "content": next(replies), "tool_calls": []}
        brain = make_brain(None, self.t, engine, tasks=self.services.tasks, choice="pipeline")
        self.assertIsInstance(brain, SlmBrain)
        self.assertIsInstance(brain.runtime, CloudRuntime)
        brain.respond("water the ferns at 5:30")
        self.assertTrue(any(s.title == "Water the ferns" for s in self.t.day_plan.spans))

    def test_a_cloud_failure_isnt_taken_for_an_answer(self):
        from thirties_core.brains.slm.cloud_runtime import CloudRuntime
        from thirties_core.inference import MockInferenceEngine
        engine = MockInferenceEngine()
        engine.chat = lambda messages, tools=None: {"content": "Error contacting model: offline", "tool_calls": []}
        with self.assertRaises(RuntimeError):
            CloudRuntime(engine).json_call("sys", "user", {"type": "object"})


if __name__ == "__main__":
    unittest.main()
