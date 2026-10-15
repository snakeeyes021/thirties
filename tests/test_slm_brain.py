"""SLM brain: Python-side behavior with a scripted stand-in for Gemma."""

import json
import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, event, make_scheduler, occupant
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.slm.brain import SlmBrain
from thirties_core.brains.slm.parse import extract_json
from thirties_core.conversation import ConversationManager
from thirties_core.models import TaskItem

TASKS = [
    TaskItem(id="t1", source_notebook="1. Tasks", title="Costco customer service", parent_note_title="Errands/Admin",
             parent_note_id="n1", section="Active", is_checkbox_item=True),
    TaskItem(id="t2", source_notebook="1. Tasks", title="Message Brad about a weekend trip",
             parent_note_title="Errands/Admin", parent_note_id="n1", section="Probation", someday=True),
    TaskItem(id="t3", source_notebook="3. Creative", title="Text search for Natalie's piece",
             parent_note_title="Natalie Piece", parent_note_id="n2", deferred_count=3),
    TaskItem(id="t4", source_notebook="2. Dev", title="Fix the media server", parent_note_title="NAS", parent_note_id="n3"),
    TaskItem(id="t5", source_notebook="1. Tasks", title="Write wedding thank-you notes", parent_note_title="Wedding To-Do",
             parent_note_id="n4"),
    TaskItem(id="t6", source_notebook="4. Media", title="Read Three-Body Problem", parent_note_title="Recommendations",
             parent_note_id="n5"),
]


class FakeSession:
    def __init__(self, replies, default='{"actions":[{"type":"chat"}]}'):
        self.replies = replies
        self.seen = []
        self.default = default

    def ask(self, text):
        self.seen.append(text)
        if self.replies:
            return self.replies.pop(0)
        if "DRAFT:\n" in text:
            return text.split("DRAFT:\n", 1)[1]  # voice: echo the draft unchanged
        return self.default

    def close(self):
        pass


class FakeRuntime:
    def __init__(self):
        self.parse_replies, self.pick_replies, self.voice_replies = [], [], []
        self.parse = FakeSession(self.parse_replies)
        self.pick = FakeSession(self.pick_replies)
        self.voice = FakeSession(self.voice_replies)

    def parser(self, system):
        if system.startswith("You are Thirties, a warm"):
            return self.voice
        return self.pick if system.startswith("You help choose") else self.parse

    def json_call(self, system, user, schema, max_output_tokens=256):
        return {"actions": []}

    def text_call(self, system, user, max_output_tokens=120):
        return "Happy to help!"


def actions(*items):
    return json.dumps({"actions": list(items)})


class SlmTestCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("16:40"))
        self.addCleanup(set_debug_time, None)
        self.rt = FakeRuntime()
        self.sched = make_scheduler([event("dentist", "18:00", "18:30", "Dentist")])
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, list(TASKS), pending, self.sched, active_notes=[],
                                     brain=None, inference_engine=None)
        self.brain = SlmBrain(self.m.tools, tasks=list(TASKS), runtime=self.rt)
        self.m.brain = self.brain
        self.m._load(None)

    def say(self, text, *parse):
        self.rt.parse_replies.append(actions(*parse) if parse and isinstance(parse[0], dict) else (parse[0] if parse else ""))
        return self.m.send_user_message(text)


class TestSchedulingActions(SlmTestCase):

    def test_work_ran_long_and_pronoun_followup(self):
        out = self.say("work went til 4:30", {"type": "move", "target": "work", "end": "4:30"})
        self.assertIn("Work now ends at 4:30 PM.", out)
        self.say("walk at 5 for half an hour", {"type": "add", "title": "Walk", "start": "5", "duration": "half an hour"})
        self.assertEqual(occupant(self.m.day_plan, "17:00"), "Walk")
        out = self.say("push it back half an hour", {"type": "move", "target": "it", "shift": "+30 minutes"})
        self.assertIn("Moved Walk to 5:30 PM – 6:00 PM", out)
        out = self.say("and another half hour", {"type": "move", "target": "it", "shift": "+30 minutes"})
        self.assertIn("Dentist", out)  # can't run into the fixed appointment

    def test_time_without_am_pm_is_read_sensibly(self):
        self.say("composing at 7 for 2 chunks", {"type": "add", "title": "Composing", "start": "7", "duration": "2 chunks"})
        self.assertEqual(occupant(self.m.day_plan, "19:30"), "Composing")  # 7 PM: 7 AM has passed

    def test_move_reads_time_near_the_item(self):
        self.say("dinner at 7", {"type": "add", "title": "Dinner", "start": "7:00 PM", "duration": "1 hour"})
        self.say("dinner from 8 to 9 instead", {"type": "move", "target": "dinner", "start": "8", "end": "9"})
        self.assertEqual(occupant(self.m.day_plan, "20:00"), "Dinner")

    def test_relative_placement_and_slide(self):
        self.say("dinner at 7 then reading at 8", {"type": "add", "title": "Dinner", "start": "7:00 PM", "duration": "1 hour"},
                 {"type": "add", "title": "Reading", "start": "8:00 PM", "duration": "1 hour"})
        out = self.say("email the recruiter right after dinner, an hour, slide things back",
                       {"type": "add", "title": "Email the recruiter", "after": "dinner", "duration": "an hour", "push": True})
        self.assertIn("Added Email the recruiter: 8:00 PM – 9:00 PM", out)
        self.assertIn("Reading now 9:00 PM – 10:00 PM", out)

    def test_conflict_offers_options_and_choice_applies(self):
        out = self.say("gym 5:30 to 6:30", {"type": "add", "title": "Gym", "start": "5:30", "end": "6:30"})
        self.assertIn("would overlap Dentist", out)
        self.assertIn("1. do it 5:30 PM – 6:00 PM", out)
        out = self.say("the first one", {"type": "choose", "option": 1})
        self.assertIn("Added Gym: 5:30 PM – 6:00 PM", out)
        self.assertEqual(self.brain.state.proposal, [])

    def test_swap_with_ordinal_reference(self):
        for title, start in [("Upgrades", "7:00 PM"), ("Shower", "8:00 PM"), ("Upgrades", "8:30 PM")]:
            self.say(f"{title} at {start}", {"type": "add", "title": title, "start": start, "duration": "30 minutes"})
        out = self.say("swap the shower with the second upgrades", {"type": "swap", "target": "shower", "other": "second upgrades"})
        self.assertIn("Swapped", out)
        self.assertEqual(occupant(self.m.day_plan, "20:00"), "Upgrades")
        self.assertEqual(occupant(self.m.day_plan, "20:30"), "Shower")

    def test_ordinal_in_message_settles_ambiguity_and_thirties_work(self):
        self.say("upgrades at 7 and again at 9", {"type": "add", "title": "Upgrades", "start": "7", "duration": "30 minutes"},
                 {"type": "add", "title": "Upgrades", "start": "9", "duration": "30 minutes"})
        out = self.say("make that first upgrades block 3 thirties",
                       {"type": "move", "target": "upgrades", "duration": "3 thirties"})
        self.assertIn("Moved Upgrades to 7:00 PM – 8:30 PM", out)

    def test_clock_time_settles_ambiguity(self):
        self.say("upgrades at 7 and again at 9", {"type": "add", "title": "Upgrades", "start": "7", "duration": "30 minutes"},
                 {"type": "add", "title": "Upgrades", "start": "9", "duration": "30 minutes"})
        out = self.say("the 9 PM one should end at 10", {"type": "move", "target": "upgrades", "end": "10"})
        self.assertIn("Moved Upgrades to 9:00 PM – 10:00 PM", out)

    def test_start_now(self):
        out = self.say("start upgrades now for an hour", {"type": "add", "title": "Upgrades", "start": "now", "duration": "an hour"})
        self.assertIn("4:30 PM – 5:30 PM", out)  # 4:40 rounds to the half hour

    def test_invented_add_is_dropped_and_missing_move_becomes_add(self):
        out = self.say("dinner at 7", {"type": "add", "title": "Dinner", "start": "7", "duration": "1 hour"},
                       {"type": "add", "title": "Inspiration for Natalie", "start": "8", "duration": "1 hour"})
        self.assertNotIn("Natalie", out)
        out = self.say("put laundry at 9", {"type": "move", "target": "laundry", "start": "9"})
        self.assertIn("Added Laundry", out)

    def test_add_without_time_asks(self):
        out = self.say("I need to make dinner at some point", {"type": "add", "title": "Dinner"})
        self.assertEqual(out, "When should I put Dinner?")


class TestProposalsAndOutcomes(SlmTestCase):

    def test_suggestion_skips_someday_uses_picker_and_confirm_adds(self):
        self.rt.pick_replies.append('{"picks": [2, 1]}')
        out = self.say("what should I do tonight?", {"type": "suggest", "topic": "tonight"})
        self.assertNotIn("Brad", out)
        self.assertTrue(out.startswith("How about:"), out)
        self.assertIn("Candidates:", self.rt.pick.seen[-1])
        first_two = [l for l in out.splitlines() if l[:2] in ("1.", "2.")]
        self.assertEqual(len(first_two), 2)
        out = self.say("yes please", {"type": "confirm"})
        self.assertEqual(out.count("Added "), 2)

    def test_proposal_survives_a_restart(self):
        self.rt.pick_replies.append('{"picks": [1]}')
        self.say("ideas?", {"type": "suggest", "topic": "tonight"})
        again = ConversationManager(self.sched.build_day_plan(DAY)[0], list(TASKS), [], self.sched, active_notes=[])
        brain = SlmBrain(again.tools, tasks=list(TASKS), runtime=self.rt)
        again.brain = brain
        again._load(again.conversation)
        self.rt.parse_replies.append(actions({"type": "confirm"}))
        self.assertIn("Added", again.send_user_message("yes"))

    def test_outcomes_and_energy(self):
        self.say("taxes at 2", {"type": "add", "title": "Taxes", "start": "2:00 PM", "duration": "1 hour"})
        out = self.say("finished the taxes around 3:30", {"type": "outcome", "target": "taxes", "status": "done", "finished": "3:30"})
        self.assertIn("Nice work: Taxes is done.", out)
        span = next(s for s in self.sched.build_day_plan(DAY)[0].spans if s.title == "Taxes")
        self.assertEqual((span.status.value, span.actual_end, span.time_source), ("done", at("15:30"), "estimated"))
        out = self.say("I'm wiped", {"type": "energy", "level": 1})
        self.assertIn("lighten", out)

    def test_memory_and_profile(self):
        self.say("fyi probation is stuff I won't do", {"type": "remember", "text": "Probation items are maybes"})
        self.say("I want to finish Natalie's piece by December", {"type": "profile", "text": "Wants to finish Natalie's piece by December"})
        services = self.m.tools.services
        self.assertEqual(services.memory.notes()[0].text, "Probation items are maybes")
        self.assertIn("Natalie", services.profile.sections()["Near-term goals"])

    def test_questions_and_chat(self):
        out = self.say("when am I free tonight?", {"type": "question", "topic": "free time tonight"})
        self.assertTrue(out.startswith("You're free"), out)
        self.assertEqual(self.say("thanks!", {"type": "chat"}), "You're welcome!")
        self.assertEqual(self.say("how's it going", {"type": "chat"}), "Happy to help!")

    def test_unparseable_output_falls_back(self):
        out = self.say("hmm", "sorry I can't do JSON")
        self.assertEqual(out, "Happy to help!")


class TestVoice(SlmTestCase):

    def test_natural_rewrite_is_used_when_facts_survive(self):
        self.rt.voice_replies.append("Done! Walk is set for 5:00 PM – 5:30 PM. You're free after that.")
        out = self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 minutes"})
        self.assertTrue(out.startswith("Done! Walk is set"), out)

    def test_rewrite_that_changes_a_time_is_rejected(self):
        self.rt.voice_replies.append("Done! Walk is set for 6:00 PM.")
        out = self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 minutes"})
        self.assertIn("Added Walk: 5:00 PM – 5:30 PM", out)

    def test_rewrite_that_drops_a_title_is_rejected(self):
        self.rt.voice_replies.append("All set for 5:00 PM – 5:30 PM!")
        out = self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 minutes"})
        self.assertIn("Added Walk", out)


class TestParsingHelpers(unittest.TestCase):

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"actions": []}\n```'), {"actions": []})
        self.assertEqual(extract_json('Sure! {"a": {"b": 1}} trailing'), {"a": {"b": 1}})
        self.assertIsNone(extract_json("no json here"))


if __name__ == "__main__":
    unittest.main()
