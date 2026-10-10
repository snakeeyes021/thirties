"""Unit tests for thirties_core.inference."""

import unittest
from unittest.mock import MagicMock, patch

from thirties_core.config import ThirtiesConfig
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine, parse_model_directives


class TestInference(unittest.TestCase):

    def test_mock_inference_engine(self) -> None:
        engine = MockInferenceEngine()
        resp = engine.chat([{'role': 'user', 'content': 'allocate block 32 to Dorico'}])
        self.assertIn('Dorico', resp['content'])
        self.assertEqual(len(resp['tool_calls']), 1)
        self.assertEqual(resp['tool_calls'][0]['name'], 'modify_blocks')
        self.assertEqual(resp['tool_calls'][0]['arguments']['start_block'], 32)

    def test_litert_availability_detection(self) -> None:
        engine = LiteRTInferenceEngine()
        self.assertTrue(engine.is_available())

    def test_modify_blocks_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 24 | label=Writing Session
Sure, I scheduled that for you.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Please allocate block 24 to Writing Session'}])
                self.assertEqual(res['content'], 'Sure, I scheduled that for you.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 24)
                self.assertEqual(res['tool_calls'][0]['arguments']['label'], 'Writing Session')

    def test_multi_block_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 28-31 | label=Composing Session
Allocated 2 hours of composing.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': "Let's go block 28, I'll probably want to go for at least two hours"}])
                self.assertEqual(res['content'], 'Allocated 2 hours of composing.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 28)
                self.assertEqual(res['tool_calls'][0]['arguments']['end_block'], 31)
                self.assertEqual(res['tool_calls'][0]['arguments']['label'], 'Composing Session')

    def test_clear_work_blocks_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
CLEAR_BLOCKS: WORK
I have deallocated all your work blocks for today.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': "I don't actually have work today. Can you deallocate all my work blocks from work?"}])
                self.assertEqual(res['content'], 'I have deallocated all your work blocks for today.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'clear_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['clear_kind'], 'WORK')

    def test_work_envelope_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 7-18 | kind=WORK locked=true
Work scheduled from 10:00 AM to 4:00 PM.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'My work hours are from 10:00 AM to 4:00 PM today.'}])
                self.assertEqual(res['content'], 'Work scheduled from 10:00 AM to 4:00 PM.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 7)
                self.assertEqual(res['tool_calls'][0]['arguments']['end_block'], 18)
                self.assertEqual(res['tool_calls'][0]['arguments']['kind'], 'WORK')
                self.assertTrue(res['tool_calls'][0]['arguments']['is_locked'])

    def test_sleep_envelope_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 32-47 | kind=SLEEP locked=true
Sleep window set from 10:00 PM to 6:00 AM.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'I am going to bed at 10pm and waking up at 6am.'}])
                self.assertEqual(res['content'], 'Sleep window set from 10:00 PM to 6:00 AM.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['kind'], 'SLEEP')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 32)
                self.assertEqual(res['tool_calls'][0]['arguments']['end_block'], 47)

    def test_resolve_event_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
RESOLVE_EVENT: evt_123 | attend
Confirmed doctor appointment.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Yes, I will attend the doctor appointment.'}])
                self.assertEqual(res['content'], 'Confirmed doctor appointment.')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'resolve_event')
                self.assertEqual(res['tool_calls'][0]['arguments']['event_id'], 'evt_123')
                self.assertEqual(res['tool_calls'][0]['arguments']['action'], 'attend')

    def test_inspect_and_finalize_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout='''---THIRTIES_RESPONSE_START---
INSPECT_BLOCKS: 1-12
FINALIZE_PLAN
Here is your plan.
---THIRTIES_RESPONSE_END---''',
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': "Looks good, let's lock it in."}])
                self.assertEqual(res['content'], 'Here is your plan.')
                self.assertEqual(len(res['tool_calls']), 2)
                self.assertEqual(res['tool_calls'][0]['name'], 'inspect_blocks')
                self.assertEqual(res['tool_calls'][1]['name'], 'finalize_day_plan')


    def test_multiple_modify_directives_in_single_turn(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 19-21 | label=Composing
MODIFY_BLOCKS: 22 | label=Lunch
I scheduled both.
---THIRTIES_RESPONSE_END---""",
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Schedule composing 19-21 and lunch 22'}])
                self.assertEqual(res['content'], 'I scheduled both.')
                self.assertEqual(len(res['tool_calls']), 2)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 19)
                self.assertEqual(res['tool_calls'][0]['arguments']['end_block'], 21)
                self.assertEqual(res['tool_calls'][0]['arguments']['label'], 'Composing')

                self.assertEqual(res['tool_calls'][1]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][1]['arguments']['start_block'], 22)
                self.assertEqual(res['tool_calls'][1]['arguments']['end_block'], 22)
                self.assertEqual(res['tool_calls'][1]['arguments']['label'], 'Lunch')

    def test_space_separated_attributes_no_greedy_swallow(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
MODIFY_BLOCKS: 19-22 | label=Composing Session locked=true
Scheduled composing session.
---THIRTIES_RESPONSE_END---""",
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Schedule composing'}])
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['arguments']['label'], 'Composing Session')
                self.assertTrue(res['tool_calls'][0]['arguments']['is_locked'])

    def test_combined_clear_and_modify_directives(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
CLEAR_BLOCKS: WORK
MODIFY_BLOCKS: 25-28 | label=Gym Session
Cleared work and added gym.
---THIRTIES_RESPONSE_END---""",
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'No work today, let us do gym at 25-28'}])
                self.assertEqual(res['content'], 'Cleared work and added gym.')
                self.assertEqual(len(res['tool_calls']), 2)
                self.assertEqual(res['tool_calls'][0]['name'], 'clear_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['clear_kind'], 'WORK')
                self.assertEqual(res['tool_calls'][1]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][1]['arguments']['start_block'], 25)
                self.assertEqual(res['tool_calls'][1]['arguments']['label'], 'Gym Session')

    def test_lowercase_directive_handling(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
modify_blocks: 10 | label=Break
Enjoy your break!
---THIRTIES_RESPONSE_END---""",
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Block 10 break'}])
                self.assertEqual(res['content'], 'Enjoy your break!')
                self.assertEqual(len(res['tool_calls']), 1)
                self.assertEqual(res['tool_calls'][0]['name'], 'modify_blocks')
                self.assertEqual(res['tool_calls'][0]['arguments']['start_block'], 10)
                self.assertEqual(res['tool_calls'][0]['arguments']['label'], 'Break')

    def test_conversational_reply_with_no_directives(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, '_find_host_runner', return_value={'python': 'mock_py', 'script': 'mock_sc'}):
            with patch('subprocess.run') as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
I recommend scheduling that during the daylight hours around 2:00 PM.
---THIRTIES_RESPONSE_END---""",
                    stderr='',
                    returncode=0,
                )
                res = engine.chat([{'role': 'user', 'content': 'Where should I compose?'}])
                self.assertEqual(res['content'], 'I recommend scheduling that during the daylight hours around 2:00 PM.')
                self.assertEqual(len(res['tool_calls']), 0)


    def test_modify_blocks_clock_time_directive_parsing(self) -> None:
        raw = """I have scheduled your work shift for today.
MODIFY_BLOCKS: 07:00 AM - 03:00 PM | kind=WORK locked=true
Have a productive shift!"""
        clean, calls = parse_model_directives(raw)
        self.assertIn("I have scheduled your work shift for today.", clean)
        self.assertIn("Have a productive shift!", clean)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "modify_blocks")
        self.assertEqual(calls[0]["arguments"]["start_time"], "07:00 AM")
        self.assertEqual(calls[0]["arguments"]["end_time"], "03:00 PM")
        self.assertEqual(calls[0]["arguments"]["kind"], "WORK")
        self.assertTrue(calls[0]["arguments"]["is_locked"])

    def test_modify_blocks_natural_clock_time_parsing(self) -> None:
        raw = "MODIFY_BLOCKS: 7am - 3pm | kind=WORK"
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["start_time"], "7am")
        self.assertEqual(calls[0]["arguments"]["end_time"], "3pm")
        self.assertEqual(calls[0]["arguments"]["kind"], "WORK")

    def test_clear_and_inspect_clock_time_directive_parsing(self) -> None:
        raw = """CLEAR_BLOCKS: 02:00 PM - 03:00 PM
INSPECT_BLOCKS: 10:00 AM - 12:00 PM"""
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["name"], "clear_blocks")
        self.assertEqual(calls[0]["arguments"]["start_time"], "02:00 PM")
        self.assertEqual(calls[0]["arguments"]["end_time"], "03:00 PM")
        self.assertEqual(calls[1]["name"], "inspect_blocks")
        self.assertEqual(calls[1]["arguments"]["start_time"], "10:00 AM")
        self.assertEqual(calls[1]["arguments"]["end_time"], "12:00 PM")


if __name__ == '__main__':
    unittest.main()
