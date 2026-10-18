"""Unit tests for the interactive qualitative evaluation harness."""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.interactive_eval import (
    EvalSession,
    InteractiveEvaluator,
    TurnGrade,
    detect_anomalies,
)


class TestInteractiveEval(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.session_id = "test_unit_session"
        self.target_date = date(2026, 10, 9)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_session_lifecycle_and_grading(self) -> None:
        session = InteractiveEvaluator.start_session(
            target_date=self.target_date,
            persona="Unit Test Persona",
            session_id=self.session_id,
            use_gpu=False,
        )
        self.assertEqual(session.session_id, self.session_id)
        self.assertIn("Good day!", session.initial_greeting)

        # Step 1
        turn = InteractiveEvaluator.step_session(
            session=session,
            user_message="I have work today from 8 to 4.",
            use_gpu=False,
        )
        self.assertEqual(turn.turn_index, 1)
        self.assertEqual(len(session.turns), 1)

        # Grade Turn 1
        grade = InteractiveEvaluator.grade_turn(
            session=session,
            turn_index=1,
            accuracy=4,
            tone=5,
            adherence=4,
            reasoning=4,
            notes="Solid turn, mock response handled cleanly.",
            flags=["good_posture"],
        )
        self.assertEqual(grade.accuracy, 4)
        self.assertEqual(session.turns[0].grade.notes, "Solid turn, mock response handled cleanly.")

        # Generate report
        report_path = InteractiveEvaluator.generate_report(session)
        self.assertTrue(report_path.exists())
        content = report_path.read_text()
        self.assertIn("Executive Scorecard", content)
        self.assertIn("Unit Test Persona", content)
        self.assertIn("Turn 1", content)
        self.assertIn("Solid turn, mock response handled cleanly.", content)

    def test_anomaly_detection_heuristics(self) -> None:
        # 1. Hallucinated action
        anomalies = detect_anomalies(
            user_text="Clear my schedule.",
            assistant_reply="I have cleared all your scheduled blocks for today.",
            executed_tools=[],
            delta_summary={"total_changes": 0},
        )
        self.assertTrue(any("HALLUCINATED ACTION" in a for a in anomalies))

        # 2. Defensive deflection
        anomalies = detect_anomalies(
            user_text="Why did you do that?",
            assistant_reply="I am not designed to explain the reasoning behind my decisions.",
            executed_tools=[],
            delta_summary={"total_changes": 0},
        )
        self.assertTrue(any("DEFENSIVE DEFLECTION" in a for a in anomalies))

        # 3. Unsolicited mutation on question
        anomalies = detect_anomalies(
            user_text="Why did you set work from 3 to 8?",
            assistant_reply="I have scheduled work from 3 to 8.",
            executed_tools=[{"name": "modify_blocks", "output": "Modified"}],
            delta_summary={"total_changes": 11},
        )
        self.assertTrue(any("UNSOLICITED MUTATION" in a for a in anomalies))


if __name__ == "__main__":
    unittest.main()
