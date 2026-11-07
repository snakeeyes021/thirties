"""The SLM brain's model-free helpers: separating requests, checking coverage, alongside items, effort."""

import unittest

from thirties_core.brains.slm.parse import Action
from thirties_core.brains.slm.steps import (effort_of, merge_alongside, parse_in_steps, sounds_tired, split_requests,
                                            uncovered_clauses)


class TestSplitRequests(unittest.TestCase):

    def test_different_requests_are_separated(self):
        cases = {
            "work ran until 4:30, and move budget review to 4:30": ["work ran until 4:30", "move budget review to 4:30"],
            "done with work, what should i do before dinner?": ["done with work", "what should i do before dinner?"],
            "finished work at 3:30. push composing by +30 min and put grocery run at 5 for an hour":
                ["finished work at 3:30.", "push composing by +30 min", "put grocery run at 5 for an hour"],
            "swap dinner and composing, and when am i going to sleep tonight?":
                ["swap dinner and composing", "when am i going to sleep tonight?"],
            "yeah do that, and also throw in a quick 20 min walk right after work":
                ["yeah do that", "throw in a quick 20 min walk right after work"],
            "took the day off! piano 10 to 12, then lunch with Sam at 12:30 for an hour":
                ["took the day off!", "piano 10 to 12", "lunch with Sam at 12:30 for an hour"],
            "email recruiter 9 to 10 and push the natalie listening to 10 to 11":
                ["email recruiter 9 to 10", "push the natalie listening to 10 to 11"],
            "gym at 4:30 p.m. and then dinner at 6 p.m.": ["gym at 4:30 pm", "dinner at 6 pm"],
        }
        for message, want in cases.items():
            self.assertEqual(split_requests(message), want, message)

    def test_one_request_stays_whole(self):
        for message in ("dinner 6 to 7, and I'll listen to Project Hail Mary while I eat",  # alongside
                        "3 thirties of upgrades first, then 2 chunks of email",              # one chain the parser reads
                        "put down Brad, Torquio, the walk, lunch and dinner",                # a list of things
                        "push dinner and composing back an hour",                            # two targets, one move
                        "fill the evening between 7 and 9",
                        "squeeze lunch in at 12:30 and push everything after it",            # the push modifies the add
                        "looks good, but swap the unpacking for something else",
                        "Sam and I are having lunch at 1",
                        "Dinner, cooking and eating, listening to Project Hail Mary at 6"):
            self.assertEqual(split_requests(message), [message], message)

    def test_a_self_correction_is_read_whole(self):
        for message in ("cancel composing tonight scratch that don't cancel it just push it back 30 minutes",
                        "can you move dinner to 7 no wait composing is at 7, move composing to 8:30",
                        "add grocery shopping after work for half an hour wait no make it 45 minutes"):
            self.assertEqual(split_requests(message), [message], message)

    def test_small_talk_rides_with_the_request(self):
        self.assertEqual(split_requests("hey thanks for the help earlier! put gym at 4:15 for 45 mins"),
                         ["hey thanks for the help earlier! put gym at 4:15 for 45 mins"])

    def test_the_number_of_requests_is_bounded(self):
        out = split_requests("cancel a, cancel b, cancel c, cancel d, cancel e, cancel f")
        self.assertEqual(len(out), 4)
        self.assertEqual(out[-1], "cancel d, cancel e, cancel f")


class TestCoverage(unittest.TestCase):

    def test_the_lost_part_is_found(self):
        # the parse benchmark's example: the work change was dropped
        message = "work ran until 4:30, and move budget review to 4:30"
        got = [Action("move", {"target": "budget review", "start": "4:30"})]
        self.assertEqual(uncovered_clauses(message, got), ["work ran until 4:30"])
        got.append(Action("move", {"target": "work", "end": "4:30"}))
        self.assertEqual(uncovered_clauses(message, got), [])

    def test_kinds_of_action_cover_their_words(self):
        self.assertEqual(uncovered_clauses("im exhausted, what should i do tonight?",
                                           [Action("energy", {"level": 1}), Action("suggest", {"topic": "tonight"})]), [])
        self.assertEqual(uncovered_clauses("option 2, and slide composing back 30 mins",
                                           [Action("choose", {"option": 2}), Action("move", {"target": "composing"})]), [])

    def test_corrections_and_what_python_handles_are_not_checked(self):
        self.assertEqual(uncovered_clauses("add a call with mom at 5 oh wait mom's not free, push dinner back an hour",
                                           [Action("move", {"target": "dinner", "shift": "+1 hour"})]), [])
        self.assertEqual(uncovered_clauses("move dinner to 7, and remind me to call mom tomorrow",
                                           [Action("move", {"target": "dinner", "start": "7"})],
                                           handled=lambda c: "remind" in c), [])

    def test_each_request_is_parsed_alone_and_a_lost_part_retried(self):
        seen = []
        replies = {"work ran until 4:30": [],  # the model misses it alone too: asked about
                   "move budget review to 4:30": [Action("move", {"target": "budget review", "start": "4:30"})]}

        def parse_one(text):
            seen.append(text)
            return list(replies.get(text, []))
        actions, unclear, requests = parse_in_steps("work ran until 4:30, and move budget review to 4:30", parse_one)
        self.assertEqual(requests, ["work ran until 4:30", "move budget review to 4:30"])
        self.assertEqual([a.type for a in actions], ["move"])
        self.assertEqual(unclear, ["work ran until 4:30"])
        self.assertEqual(seen, ["work ran until 4:30", "move budget review to 4:30", "work ran until 4:30"])

    def test_a_retry_that_works_is_kept(self):
        calls = []

        def parse_one(text):
            calls.append(text)
            if text.startswith("work"):
                return [Action("move", {"target": "work", "end": "4:30"})] if len(calls) > 1 else []
            return [Action("move", {"target": "budget review", "start": "4:30"})]
        actions, unclear, _ = parse_in_steps("move budget review to 4:30 and work ran until 4:30", parse_one,
                                             split=False)
        self.assertEqual(unclear, [])
        self.assertEqual({a.get("target") for a in actions}, {"budget review", "work"})

    def test_small_talk_is_never_asked_about(self):
        actions, unclear, _ = parse_in_steps("what do you think about jazz?", lambda t: [Action("chat", {})])
        self.assertEqual(([a.type for a in actions], unclear), (["chat"], []))


class TestAlongside(unittest.TestCase):

    def test_dinner_cooking_and_eating_listening_to_a_book_is_one_item_and_the_book(self):
        message = "Dinner, cooking and eating, listening to Project Hail Mary at 6"
        three = [Action("add", {"title": "Dinner", "start": "6"}), Action("add", {"title": "Cooking and eating"}),
                 Action("add", {"title": "Listening to Project Hail Mary"})]
        one = [Action("add", {"title": "Dinner, cooking and eating, listening to Project Hail Mary", "start": "6"})]
        for parsed in (three, one):
            out = merge_alongside(parsed, message)
            self.assertEqual([(a.get("title"), a.get("start"), a.get("alongside")) for a in out],
                             [("Dinner", "6", None), ("Project Hail Mary", "6", True)])

    def test_a_named_book_with_its_own_title_goes_alongside(self):
        out = merge_alongside([Action("add", {"title": "Walk", "start": "5", "duration": "30 min"}),
                               Action("add", {"title": "Hardcore History"})],
                              "walk at 5 for 30 min while listening to hardcore history")
        self.assertEqual(out[1].fields, {"title": "Hardcore History", "alongside": True, "start": "5",
                                         "duration": "30 min"})

    def test_separate_things_stay_separate(self):
        parsed = [Action("add", {"title": "Dinner", "start": "6"}), Action("add", {"title": "Laundry", "start": "7"})]
        self.assertEqual(merge_alongside(parsed, "dinner at 6 and laundry at 7"), parsed)


class TestEffort(unittest.TestCase):

    def test_rough_effort(self):
        self.assertEqual(effort_of("Call the bank"), 1)
        self.assertEqual(effort_of("Do the taxes"), 3)
        self.assertEqual(effort_of("Fix the media server"), 3)
        self.assertEqual(effort_of("Hang the picture"), 2)
        self.assertEqual(effort_of("Hang the picture", estimated_thirties=4), 3)
        self.assertEqual(effort_of("Hang the picture", is_substep=True), 1)
        self.assertEqual(effort_of("Email the landlord", deferred_count=5), 2)
        self.assertEqual(effort_of("Dune", is_media=True), 1)

    def test_sounds_tired(self):
        self.assertTrue(sounds_tired("ugh I'm so fried today"))
        self.assertTrue(sounds_tired("you pick", latest_energy=2))
        self.assertFalse(sounds_tired("you pick", latest_energy=4))


if __name__ == "__main__":
    unittest.main()
