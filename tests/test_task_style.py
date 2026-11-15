"""Help write tasks in the user's own style. Invented items throughout."""

import unittest

from tests.fixtures import DAY, make_scheduler
from thirties_core.assistant_tools import AssistantTools, Services
from thirties_core.models import TaskItem
from thirties_core.plan_tools import PlanTools
from thirties_core.task_style import describe, style


def item(tid, title, note):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, parent_note_title=note, parent_note_id=note)


HOUSE = [item("h1", "fix porch light", "House"), item("h2", "fix the gate latch", "House"),
         item("h3", "paint back steps", "House"), item("h4", "order gutter guards", "House"),
         item("h5", "fix shed door", "House")]
WORK = [item("w1", "Draft the Q3 brochure copy.", "Clients"), item("w2", "Send invoices to Harrow Lane.", "Clients"),
        item("w3", "Review the Fennel Press proofs.", "Clients")]


class StyleTest(unittest.TestCase):
    def test_how_a_list_is_written(self):
        lines = describe(style([t.title for t in HOUSE]))
        self.assertIn("Starts in lower case.", lines)
        self.assertIn("No full stop at the end.", lines)
        self.assertIn("Starts with a verb (an action).", lines)
        self.assertIn("Often starts with: fix.", lines)
        work = describe(style([t.title for t in WORK]))
        self.assertIn("Starts with a capital letter.", work)
        self.assertIn("Ends with a full stop.", work)


class ToolTest(unittest.TestCase):
    def setUp(self):
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        self.pt = PlanTools(plan, pending, sched)
        self.pt.assistant = AssistantTools(self.pt, Services.for_db(sched.state_db, HOUSE + WORK))

    def test_the_lists_own_style_and_close_examples(self):
        out = self.pt.execute("task_style", {"list": "House", "about": "door"})
        self.assertIn("Starts in lower case.", out)
        self.assertEqual(out.split("Their own lines:\n")[1].splitlines()[0], "fix shed door")
        self.assertNotIn("brochure", out)

    def test_too_few_items(self):
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        pt = PlanTools(plan, pending, sched)
        pt.assistant = AssistantTools(pt, Services.for_db(sched.state_db, HOUSE[:1]))
        self.assertIn("Too few", pt.execute("task_style", {}))

    def test_the_prompt_says_when_to_use_it(self):
        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.inference import MockInferenceEngine
        prompt = FrontierBrain(self.pt, MockInferenceEngine(), HOUSE, []).build_system_prompt()
        self.assertIn("call task_style", prompt)
        self.assertIn("call sky", prompt)


if __name__ == "__main__":
    unittest.main()
