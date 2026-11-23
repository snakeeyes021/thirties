"""An item is called pressing only when its urgency or due date says so.

The model reads each item's urgency (now, soon, a date; unmarked is no hurry) and its due date. The rule it
follows when it plans the day: call an item pressing, urgent or due only when those say so, and otherwise give the
real reason it was chosen, or none. Python's own words about the day's options never call an item pressing
either: the options are listed best first, each with its own reason.

Invented: someone who sews and binds small books at a bindery bench."""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase
from thirties_core import morning
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem

SEW = "Sew the bindery endpapers"


class TestTheRule(unittest.TestCase):

    def test_the_rule_is_in_what_the_model_reads_when_proposing(self):
        from thirties_core.day_proposal import MORNING_INSTRUCTION, PROPOSAL_TOOL_SPECS
        spec = next(s for s in PROPOSAL_TOOL_SPECS if s["name"] == "propose_day")
        for text in (morning.POLICY, MORNING_INSTRUCTION, spec["description"]):
            self.assertIn("pressing, urgent or due only when its urgency", text)
            self.assertIn("otherwise give the real reason you chose it, or none", text)

    def test_the_morning_doesnt_ask_for_what_is_pressing(self):
        from thirties_core.day_proposal import MORNING_INSTRUCTION
        self.assertNotIn("what's pressing first", MORNING_INSTRUCTION)


class TestNoUrgencyNoPressingWords(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:40"))
        TITLES.setdefault("n_bench", "Bench picks")
        TITLES.setdefault("n_bindery", "Bindery")
        self.api.notes["n_bench"] = {"id": "n_bench", "body": f"- [ ] {SEW}\n"}
        self.api.notes["n_bindery"] = {"id": "n_bindery", "body": "- [ ] Press the quill case\n"}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_bench", "menu", "Bench picks")
        catalog.set_role("n_bindery", "backlog", "Bindery")
        task = TaskItem(id="t_sew", source_notebook="1. Tasks", title=SEW, is_checkbox_item=True,
                        parent_note_title="Bench picks", parent_note_id="n_bench")
        catalog.apply([task])
        self.services.tasks.append(task)
        self.a = self.t.assistant

    def test_the_options_give_its_own_reason_and_no_pressing(self):
        options = self.t.execute("day_options", {})
        self.assertIn(SEW, options)
        self.assertIn("a free-time pick", options)
        self.assertIn("Time on Bindery (keeps the project moving", options)
        for word in ("pressing", "urgent"):
            self.assertNotIn(word, options.lower())

    def test_a_proposal_of_it_says_nothing_pressing(self):
        out = self.t.execute("propose_day", {"items": [{"item": "t_sew", "start": "8:10 PM"},
                                                       {"item": "Time on Bindery", "start": "9:10 PM"}]})
        self.assertIn(SEW, out)
        for word in ("pressing", "urgent"):
            self.assertNotIn(word, out.lower())

    def test_the_prompts_day_options_say_nothing_pressing(self):
        section = self.a.proposals.prompt_section()
        self.assertIn(SEW, section)
        self.assertNotIn("pressing", section.lower())


if __name__ == "__main__":
    unittest.main()
