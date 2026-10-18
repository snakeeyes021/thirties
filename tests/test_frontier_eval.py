"""The frontier eval's checks must pass when the model does exactly the right thing.

Each scenario is replayed with a scripted model that makes the ideal tool calls,
so a failing check here means the scenario or its checks are wrong, not the model.
"""

import unittest

from tests.frontier_eval import SCENARIOS, run_scenario
from thirties_core.inference import MockInferenceEngine, parse_model_directives


def r(text: str) -> dict:
    content, calls = parse_model_directives(text)
    return {"role": "assistant", "content": content, "tool_calls": calls}


IDEAL = {
    "work_ran_long": [r('Noted.\nCALL update_event {"event": "Work", "end": "4:30 PM"}')],
    "work_starts_late": [r('Got it.\nCALL update_event {"event": "Work", "start": "10:00 AM"}')],
    "day_off": [r('Enjoy!\nCALL remove_event {"event": "Work"}')],
    "duration_task": [r('Done.\nCALL add_event {"title": "Composing", "start": "5:00 PM", "duration": "2 hours"}')],
    "push_back": [r('Moved.\nCALL update_event {"event": "Composing", "shift": "+1 hour"}')],
    "sleep_tonight": [r('Sleep well.\nCALL update_event {"event": "Sleep", "start": "10:30 PM", "end": "6:30 AM"}')],
    "suggestion_no_mutation": [r("You could compose from 5 to 7, then read. Want me to add that?")],
    "calendar_protected": [
        r('CALL remove_event {"event": "Work"}\nCALL add_event {"title": "Gym", "start": "2:30 PM", "end": "4:00 PM"}'),
        r('CALL add_event {"title": "Gym", "start": "2:30 PM", "end": "3:00 PM"}'),
        r("Your dentist is at 3, so the gym is 2:30 to 3."),
    ],
    "travel_buffer": [r('CALL remove_event {"event": "Work"}\nCALL add_event {"title": "Travel to dentist", "start": "2:30 PM", "end": "3:00 PM"}\nAdded.')],
    "cancel_and_replace": [r('CALL remove_event {"event": "Composing"}\nCALL add_event {"title": "Laundry", "start": "7:30 PM", "duration": "1 hour"}\nDone.')],
    "attend_ambiguous": [r('CALL resolve_calendar_event {"event": "Team dinner", "attend": true}\nHave fun!')],
    "wrap_up_without_ceremony": [
        r('CALL add_event {"title": "Walk", "start": "5:00 PM", "duration": "30 minutes"}\nAdded.'),
        r("Great, you're all set."),
    ],
    "together_at_once": [r('CALL add_event {"title": "Dinner", "start": "6:00 PM", "end": "7:00 PM"}\n'
                           'CALL add_event {"title": "Dungeon Crawler Carl", "start": "6:00 PM", "end": "7:00 PM", "alongside": true}\nDone.')],
    "tomorrow_not_today": [r('CALL add_event {"title": "Move NAS and router", "start": "9:00 AM", "duration": "1 hour", "day": "tomorrow"}\nDone.')],
}


class TestFrontierEvalScenarios(unittest.TestCase):

    def test_every_scenario_has_an_ideal_script(self) -> None:
        self.assertEqual(set(IDEAL), {s.id for s in SCENARIOS})

    def test_ideal_behaviour_passes_every_check(self) -> None:
        for scn in SCENARIOS:
            with self.subTest(scenario=scn.id):
                run = run_scenario(scn, "scripted", engine=MockInferenceEngine(IDEAL[scn.id]))
                self.assertEqual(run.error, "")
                self.assertEqual(run.failures, [])

    def test_doing_nothing_fails_scenarios_that_need_changes(self) -> None:
        silent = [r("Okay!")] * 3
        needs_change = [s for s in SCENARIOS if s.id != "suggestion_no_mutation"]
        for scn in needs_change:
            with self.subTest(scenario=scn.id):
                self.assertTrue(run_scenario(scn, "scripted", engine=MockInferenceEngine(silent)).failures)


if __name__ == "__main__":
    unittest.main()
