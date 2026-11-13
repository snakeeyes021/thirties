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
        self.assertEqual(split_line("- Fix the squeaky door (as of 2026-10-19)"),
                         ("Fix the squeaky door", date(2026, 10, 19)))
        self.assertEqual(split_line("* Ship the garden app"), ("Ship the garden app", None))

    def test_stale_and_undated_goals_are_listed_oldest_first(self):
        sections = {"Near-term goals": "- Fix the squeaky door (as of 2026-10-10)\n- Ship the garden app\n"
                                       "- Garden app testing (as of 2026-10-25)",
                    "Long-term goals": "- Learn piano (as of 2026-09-01)"}
        self.assertEqual([g[1] for g in stale_goals(sections, TODAY)], ["Ship the garden app", "Fix the squeaky door"])
        self.assertEqual(prompt_lines(sections, TODAY)[1], "- Fix the squeaky door (near-term; last checked Oct 10)")

    def test_rewriting_a_section_doesnt_freshen_old_goals(self):
        old = "- Ship the garden app\n- Garden app testing (as of 2026-10-01)"
        new = stamp_new_lines(old, "- Garden app testing\n- Ship the garden app\n- Ship 0.2", TODAY)
        self.assertEqual(new, "- Garden app testing (as of 2026-10-01)\n- Ship the garden app\n- Ship 0.2 (as of 2026-10-30)")

    def test_checking_a_goal(self):
        sections = {"Near-term goals": "- Fix the squeaky door\n- Garden app testing (as of 2026-10-01)"}
        section, content, said = check_goal(sections, "squeaky door", "done", TODAY)
        self.assertEqual((section, content), ("Near-term goals", "- Garden app testing (as of 2026-10-01)"))
        _, content, _ = check_goal(sections, "Garden app testing", "current", TODAY)
        self.assertIn("- Garden app testing (as of 2026-10-30)", content)
        with self.assertRaises(ValueError):
            check_goal(sections, "t", "current", TODAY)  # both goals have a "t": ambiguous
        with self.assertRaises(ValueError):
            check_goal(sections, "squeaky door", "maybe", TODAY)

    def test_the_tools_and_the_prompt(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        services = Services.for_db(sched.state_db)
        services.profile.set_section("Near-term goals", "- Fix the squeaky door")
        t = PlanTools(plan, pending, sched, services=services)
        t.execute("update_profile", {"section": "Near-term goals", "text": "Ship 0.2"})
        self.assertIn("- Ship 0.2 (as of 2026-10-15)", services.profile.sections()["Near-term goals"])

        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.inference import MockInferenceEngine
        brain = FrontierBrain(t, MockInferenceEngine(), [], [])
        prompt = brain.build_system_prompt()
        self.assertIn("GOALS TO CHECK", prompt)
        self.assertIn("- Fix the squeaky door (near-term; never checked)", prompt)
        self.assertNotIn("Ship 0.2 (near-term", prompt)

        self.assertEqual(t.execute("check_goal", {"goal": "squeaky door", "state": "done"}),
                         "Took 'Fix the squeaky door' out of Near-term goals (done).")
        self.assertTrue(t.execute("check_goal", {"goal": "squeaky door", "state": "done"}).startswith(NOT_DONE))


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

    def test_rules_now_built_in_are_offered_for_removal(self):
        built_in = ["Line the cleaning up with the half-hour blocks.",
                    "Applies new plans without needing to ask me first.",
                    "Garden tickets get planned as project time.",
                    "Evening summaries should mention the routines.",
                    "Laundry routine in the same order as the note."]
        for note in built_in:
            self.assertIsNotNone(why_outdated(note), note)
        self.assertIsNone(why_outdated("Prefers tea to coffee after lunch."))  # a real preference stays

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

    def test_after_an_update_the_model_names_notes_the_patterns_miss(self):
        import json
        from thirties_core.memory_audit import BUILT_IN, VERSION_KEY, review_after_update
        sched = make_scheduler()
        memory = Services.for_db(sched.state_db).memory
        meals = memory.add("Add the meals to every day by themselves, unasked.")
        fact = memory.add("Prefers the window seat on trains.")
        k = next(i for i, (why, _) in enumerate(BUILT_IN, 1) if "anchors such as meals" in why)
        calls = []

        class Judge:
            def chat(self, messages):
                calls.append(messages[0]["content"])
                return {"content": json.dumps({"notes": [{"id": meals.id, "behaviour": k},
                                                         {"id": "no-such-note", "behaviour": 1},
                                                         {"id": fact.id, "behaviour": 999}]})}
        self.assertEqual(review_after_update(sched.state_db, "0.3.0", Judge()), 1)
        self.assertEqual([(n.id, why) for n, why in to_review(sched.state_db)], [(meals.id, BUILT_IN[k - 1][0])])
        self.assertIn(fact.text, calls[0])  # the notes the patterns missed, with the behaviours
        self.assertEqual(sched.state_db.get_sync_meta(VERSION_KEY), "0.3.0")
        review_after_update(sched.state_db, "0.3.0", Judge())
        self.assertEqual(len(calls), 1)  # once per version
        self.assertEqual(len(memory.notes()), 2)  # offered, never removed
        review_after_update(sched.state_db, "0.3.1", Judge())
        self.assertEqual(len(calls), 2)  # looked over again after the next update

    def test_without_a_model_or_a_version_only_the_patterns_count(self):
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.memory_audit import review_after_update
        sched = make_scheduler()
        Services.for_db(sched.state_db).memory.add("Add the meals to every day by themselves, unasked.")
        self.assertEqual(review_after_update(sched.state_db, "0.3.0", MockInferenceEngine()), 0)
        self.assertEqual(review_after_update(sched.state_db, "", None), 0)

    def test_a_kept_note_comes_back_only_for_something_built_in_later(self):
        sched = make_scheduler()
        memory = Services.for_db(sched.state_db).memory
        old = memory.add(self.NOTES[2])  # a 0.2 behaviour
        new = memory.add("Line the cleaning up with the half-hour blocks.")  # a 0.3 one
        review = MemoryReview(sched.state_db)
        review.keep(old.id, "0.2.4")
        review.keep(new.id, "0.2.4")
        self.assertEqual([n.id for n, _ in to_review(sched.state_db)], [new.id])  # built in since it was kept
        review.keep(new.id, "0.3.0")
        self.assertEqual(to_review(sched.state_db), [])

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
