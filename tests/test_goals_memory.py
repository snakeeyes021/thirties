"""Stale goals get checked before they're offered (A7); memory notes that paper over fixed bugs are listed (D)."""

import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.goals import check_goal, prompt_lines, split_line, stale_goals, stamp_new_lines
from thirties_core.memory_audit import MemoryReview, main, to_review, why_outdated
from thirties_core.plan_tools import NOT_DONE, PlanTools

TODAY = date(2026, 10, 30)


class TestGoals(unittest.TestCase):
    def test_lines_carry_their_date(self):
        self.assertEqual(split_line("- Hide the Ethernet cable (as of 2026-10-19)"),
                         ("Hide the Ethernet cable", date(2026, 10, 19)))
        self.assertEqual(split_line("* Merge Redfox"), ("Merge Redfox", None))

    def test_stale_and_undated_goals_are_listed_oldest_first(self):
        sections = {"Near-term goals": "- Hide the Ethernet cable (as of 2026-10-10)\n- Merge Redfox\n"
                                       "- Torquio testing (as of 2026-10-25)",
                    "Long-term goals": "- Learn piano (as of 2026-09-01)"}
        self.assertEqual([g[1] for g in stale_goals(sections, TODAY)], ["Merge Redfox", "Hide the Ethernet cable"])
        self.assertEqual(prompt_lines(sections, TODAY)[1], "- Hide the Ethernet cable (near-term; last checked Oct 10)")

    def test_rewriting_a_section_doesnt_freshen_old_goals(self):
        old = "- Merge Redfox\n- Torquio testing (as of 2026-10-01)"
        new = stamp_new_lines(old, "- Torquio testing\n- Merge Redfox\n- Ship 0.2", TODAY)
        self.assertEqual(new, "- Torquio testing (as of 2026-10-01)\n- Merge Redfox\n- Ship 0.2 (as of 2026-10-30)")

    def test_checking_a_goal(self):
        sections = {"Near-term goals": "- Hide the Ethernet cable\n- Torquio testing (as of 2026-10-01)"}
        section, content, said = check_goal(sections, "ethernet", "done", TODAY)
        self.assertEqual((section, content), ("Near-term goals", "- Torquio testing (as of 2026-10-01)"))
        _, content, _ = check_goal(sections, "Torquio testing", "current", TODAY)
        self.assertIn("- Torquio testing (as of 2026-10-30)", content)
        with self.assertRaises(ValueError):
            check_goal(sections, "t", "current", TODAY)  # both goals have a "t": ambiguous
        with self.assertRaises(ValueError):
            check_goal(sections, "Ethernet", "maybe", TODAY)

    def test_the_tools_and_the_prompt(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        services = Services.for_db(sched.state_db)
        services.profile.set_section("Near-term goals", "- Hide the Ethernet cable")
        t = PlanTools(plan, pending, sched, services=services)
        t.execute("update_profile", {"section": "Near-term goals", "text": "Ship 0.2"})
        self.assertIn("- Ship 0.2 (as of 2026-10-15)", services.profile.sections()["Near-term goals"])

        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.inference import MockInferenceEngine
        brain = FrontierBrain(t, MockInferenceEngine(), [], [])
        prompt = brain.build_system_prompt()
        self.assertIn("GOALS TO CHECK", prompt)
        self.assertIn("- Hide the Ethernet cable (near-term; never checked)", prompt)
        self.assertNotIn("Ship 0.2 (near-term", prompt)

        self.assertEqual(t.execute("check_goal", {"goal": "Ethernet", "state": "done"}),
                         "Took 'Hide the Ethernet cable' out of Near-term goals (done).")
        self.assertTrue(t.execute("check_goal", {"goal": "Ethernet", "state": "done"}).startswith(NOT_DONE))


class TestMemoryAudit(unittest.TestCase):
    NOTES = [
        "Items don't cross block boundaries; move them to the next block.",       # contradicts A13
        "Shower things go after work.",                                          # never right
        "Don't copy routine items into the Today note.",                         # fixed in Block 0
        "Probation items are maybes.",                                           # a real preference
        "Dinner takes an hour.",                                                 # a real fact
        "Never schedule or check in on Relax.",                                  # held by a note, not the core
    ]

    def test_workarounds_are_recognised_and_preferences_are_not(self):
        flagged = [n for n in self.NOTES if why_outdated(n)]
        self.assertEqual(flagged, [self.NOTES[0], self.NOTES[1], self.NOTES[2], self.NOTES[5]])

    def test_listed_until_deleted_or_kept_and_never_deleted_on_its_own(self):
        sched = make_scheduler()
        services = Services.for_db(sched.state_db)
        ids = [services.memory.add(text).id for text in self.NOTES]
        listed = to_review(sched.state_db)
        self.assertEqual(len(listed), 4)
        self.assertEqual(len(services.memory.notes()), 6)  # listing deletes nothing
        MemoryReview(sched.state_db).keep(ids[5])
        self.assertEqual(len(to_review(sched.state_db)), 3)
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--db", str(sched.state_db.db_path), "--delete", ids[0], ids[1]])
        self.assertIn(f"Deleted {ids[0]}.", out.getvalue())
        self.assertEqual([n.text for n, _ in to_review(sched.state_db)], [self.NOTES[2]])
        self.assertEqual(len(services.memory.notes()), 4)

    def test_kept_answers_sync(self):
        from thirties_core.sync_engine import SyncEngine
        sched = make_scheduler()
        stores = SyncEngine(config=sched.config, state_db=sched.state_db, joplin_engine=MagicMock())._stores()
        self.assertIn("memory_review", stores)
        MemoryReview(sched.state_db).keep("n1")
        other = MemoryReview(make_scheduler().state_db)
        for record in MemoryReview(sched.state_db).all_records():
            self.assertTrue(other.merge_record(record))
        self.assertEqual(other.kept(), {"n1"})


if __name__ == "__main__":
    unittest.main()
