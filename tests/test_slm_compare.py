"""The parse benchmark and the model comparison harness, on a scripted stand-in for a model (no model is loaded)."""

import json
import re
import unittest

from thirties_core.astronomy import set_debug_time
from tests.slm_model_compare import measure_model, score_sorting, table
from tests.slm_parse_eval import check, run_benchmark

CASES = [
    {"category": "hand", "message": "cancel composing", "context": {}, "types": ["remove"],
     "fields": {0: {"target": "compos"}}},
    {"category": "hand", "message": "thanks!", "context": {}, "types": ["chat"], "fields": {}},
    {"category": "multi", "message": "work ran until 4:30, and move composing to 8", "context": {},
     "types": ["move", "move"], "fields": {0: {"target": "work", "end": "4:30"}, 1: {"target": "compos", "start": "8"}}},
    {"category": "fact", "message": "dinner is at 6 and it can't move", "context": {}, "types": ["fact"],
     "fields": {0: {"target": "dinner", "fixed": "True"}}},
]

ANSWERS = {  # what the stand-in "model" says for each message it's shown
    "cancel composing": [{"type": "remove", "target": "composing"}],
    "work ran until 4:30": [{"type": "move", "target": "work", "end": "4:30"}],
    "move composing to 8": [{"type": "move", "target": "composing", "start": "8"}],
    "work ran until 4:30, and move composing to 8": [{"type": "move", "target": "composing", "start": "8"}],
    "dinner is at 6 and it can't move": [{"type": "fact", "target": "dinner", "fixed": True}],
}


class ScriptedSession:
    def __init__(self, calls):
        self.calls = calls

    def ask(self, context):
        message = re.search(r'Message: "(.*)"', context).group(1)
        self.calls.append(message)
        return json.dumps({"actions": ANSWERS.get(message, [{"type": "chat"}])})

    def close(self):
        pass


class ScriptedRuntime:
    load_seconds = 1.5
    device = "test"

    def __init__(self):
        self.asked = []

    def parser(self, system):
        return ScriptedSession(self.asked)


class TestParseBenchmark(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)

    def test_the_steps_recover_the_part_the_whole_message_parse_loses(self):
        whole = run_benchmark(ScriptedRuntime(), [dict(c) for c in CASES], pipeline="whole", log=lambda s: None)
        steps = run_benchmark(ScriptedRuntime(), [dict(c) for c in CASES], pipeline="steps", log=lambda s: None)
        self.assertEqual((whole["cases"], whole["final"]), (4, 3))  # the one-shot parse dropped the work change
        self.assertEqual(steps["final"], 4)
        self.assertGreater(steps["calls_per_message"], whole["calls_per_message"])
        self.assertEqual(steps["by_category"]["multi"], [1, 1, 1])
        self.assertEqual([f["message"] for f in whole["failures"]], ["work ran until 4:30, and move composing to 8"])

    def test_a_label_for_a_new_field_is_checked(self):
        from thirties_core.brains.slm.parse import Action
        ok, _ = check([Action("fact", {"target": "dinner", "fixed": True})], ["fact"], {0: {"fixed": "True"}})
        self.assertTrue(ok)


class TestModelComparison(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)

    def test_a_row_per_model_and_the_table(self):
        row = measure_model("qwen3-4b", runtime=ScriptedRuntime(), cases=[dict(c) for c in CASES])
        self.assertEqual((row["label"], row["backend"], row["load_s"]), ("Qwen3 4B Instruct", "llama.cpp", 1.5))
        self.assertEqual(row["parse"]["final"], 4)
        text = table([row, {"model": "phi-4-mini", "label": "Phi-4 Mini", "backend": "llama.cpp", "params": "3.8B",
                            "licence": "MIT (verify)", "error": "Phi-4 Mini isn't in the models folder"}])
        self.assertIn("| Qwen3 4B Instruct | llama.cpp (test) | 4B | Apache-2.0 (verify) | 100% | 100% | 0 |", text)
        self.assertIn("not run: Phi-4 Mini isn't in the models folder", text)
        self.assertIn("- Qwen3 4B Instruct, weakest after Python:", text)

    def test_list_sorting_agreement(self):
        class Sorter:
            def role(self, note):
                return "list"

            def urgencies(self, note, role, items, today):
                return {"a": ("now", None), "b": ("whenever", None)}
        out = score_sorting(Sorter(), [{"note": {"title": "Errands", "body": ""}, "role": "list", "items": [],
                                         "urgencies": {"a": "now", "b": "soon"}}])
        self.assertEqual(out, {"roles": [1, 1], "urgencies": [1, 2]})


if __name__ == "__main__":
    unittest.main()
