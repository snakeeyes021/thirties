"""SLM brain: Python-side behavior with a scripted stand-in for Gemma."""

import json
import unittest
from datetime import timedelta

from tests.fixtures import DAY, FakeJoplinAPI, at, event, make_scheduler, occupant
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

    json_replies = []

    def json_call(self, system, user, schema, max_output_tokens=256):
        return self.json_replies.pop(0) if self.json_replies else {"actions": []}

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
        """The brain's own reply (the AM/PM pass that follows is tested in test_clock and below)."""
        self.rt.parse_replies.append(actions(*parse) if parse and isinstance(parse[0], dict) else (parse[0] if parse else ""))
        self.shown = self.m.send_user_message(text)
        return self.m.last_raw_reply


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

    def test_actions_that_depend_on_later_ones_are_retried(self):
        self.say("natalie listening 9 to 10", {"type": "add", "title": "Natalie listening", "start": "9", "end": "10"})
        out = self.say("email recruiter 9 to 10 and push the natalie listening to 10 to 11",
                       {"type": "add", "title": "Email recruiter", "start": "9", "end": "10"},
                       {"type": "move", "target": "natalie listening", "start": "10", "end": "11"})
        self.assertIn("Added Email recruiter: 9:00 PM – 10:00 PM", out)
        self.assertIn("Natalie listening now 10:00 PM – 11:00 PM", out)  # slid along by the add, as asked
        self.assertNotIn("overlap", out)
        self.assertNotIn("couldn't", out)

    def test_tool_errors_are_humane(self):
        self.say("reading 8 to 9", {"type": "add", "title": "Reading", "start": "8", "end": "9"})
        self.say("tv 9 to 10", {"type": "add", "title": "TV", "start": "9", "end": "10"})
        out = self.say("move reading to 9", {"type": "move", "target": "reading", "start": "9", "end": "10"})
        self.assertNotIn("replace_overlaps", out)
        self.assertNotIn("[id", out)

    def test_option_number_with_more_requests(self):
        self.say("gym 5:30 to 6:30", {"type": "add", "title": "Gym", "start": "5:30", "end": "6:30"})
        out = self.say("2. Also add 30 minutes of travel time at 7:30",
                       {"type": "add", "title": "Travel", "start": "7:30", "duration": "30 minutes"})
        self.assertIn("Added Gym: 6:30 PM", out)
        self.assertIn("Added Travel", out)

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

    def test_move_to_tomorrow(self):
        self.say("email at 5", {"type": "add", "title": "Email", "start": "5", "duration": "30 minutes"})
        out = self.say("push the email to tomorrow at 9", {"type": "move", "target": "email", "to_day": "tomorrow", "start": "9"})
        self.assertIn("Friday, October 16, 9:00 AM – 9:30 AM", out)

    def test_misfiled_end_times_are_repaired(self):
        out = self.say("reading at 8 until 9:30", {"type": "add", "title": "Reading", "start": "8", "duration": "until 9:30"})
        self.assertIn("8:00 PM – 9:30 PM", out)
        out = self.say("piano 10 to 11 tonight", {"type": "add", "title": "Piano", "start": "10 to 11"})
        self.assertIn("10:00 PM – 11:00 PM", out)

    def test_time_of_day_words(self):
        out = self.say("piano late afternoon for an hour", {"type": "add", "title": "Piano", "start": "late afternoon", "duration": "an hour"})
        self.assertIn("4:30 PM – 5:30 PM", out)
        self.say("x", {"type": "move", "target": "work", "end": "4:00"})
        out = self.say("walk after work", {"type": "add", "title": "Walk", "start": "after work", "duration": "30 min"})
        self.assertIn("Walk", out)

    def test_unreadable_time_asks_and_move_of_missing_item_becomes_add(self):
        out = self.say("piano sometime", {"type": "add", "title": "Piano", "start": "whenever", "duration": "2 hours"})
        self.assertEqual(out, "What time should I put Piano?")
        out = self.say("piano 7 to 9, and lunch with Sam at 9:30", {"type": "move", "target": "piano", "start": "7", "end": "9"},
                       {"type": "add", "title": "Lunch with Sam", "start": "9:30", "duration": "1 hour"})
        self.assertIn("Added Piano: 7:00 PM – 9:00 PM", out)
        self.assertIn("Added Lunch with Sam", out)

    def test_unknown_duration_is_guessed_so_chains_still_work(self):
        out = self.say("dinner at 6:30 then laundry then reading after laundry",
                       {"type": "add", "title": "Dinner", "start": "6:30", "duration": "1 hour"},
                       {"type": "add", "title": "Laundry", "after": "dinner", "duration": "unknown"},
                       {"type": "add", "title": "Reading", "after": "laundry", "duration": "1 hour"})
        self.assertIn("Added Laundry: 7:30 PM – 8:30 PM (1h). (I guessed an hour", out)
        self.assertIn("Added Reading: 8:30 PM – 9:30 PM", out)

    def test_slide_everything_after_and_never_touch_sleep_unasked(self):
        for title, start, end in [("Upgrades", "7:00", "8:00"), ("Chores", "8:00", "9:00")]:
            self.say(f"{title} {start} to {end}", {"type": "add", "title": title, "start": start, "end": end})
        out = self.say("lunch at 7 for 30 minutes and slide everything after it later",
                       {"type": "add", "title": "Lunch", "start": "7", "duration": "30 minutes"},
                       {"type": "move", "target": "chores", "shift": "+30 minutes"},
                       {"type": "move", "target": "sleep", "shift": "+30 minutes"})
        self.assertIn("Added Lunch: 7:00 PM – 7:30 PM", out)
        self.assertEqual(occupant(self.m.day_plan, "19:30"), "Upgrades")
        self.assertEqual(occupant(self.m.day_plan, "20:30"), "Chores")
        self.assertEqual(occupant(self.m.day_plan, "23:00"), "Sleep")

    def test_clean_list_for_part_of_the_day(self):
        self.say("reading at 7", {"type": "add", "title": "Reading", "start": "7", "duration": "1 hour"})
        out = self.say("show me a clean list of the evening", {"type": "question", "topic": "schedule"})
        self.assertTrue(out.startswith("Here's the evening:"), out)
        self.assertTrue(out.endswith("- 7:00 PM – Reading"), out)
        self.assertNotIn("Sleep", out)
        self.assertNotIn("daylight", out)

    def test_whole_day_wins_over_part_words(self):
        self.say("reading at 7", {"type": "add", "title": "Reading", "start": "7", "duration": "1 hour"})
        out = self.say("show me the whole day starting from the morning", {"type": "question", "topic": "schedule"})
        self.assertTrue(out.startswith("Here's today:"), out)
        self.assertIn("Work", out)
        self.assertIn("Reading", out)

    def test_bare_number_is_a_clock_time_and_a_length_alone_changes_the_last_item(self):
        self.say("piano 7 to 8", {"type": "add", "title": "Piano", "start": "7", "end": "8"})
        out = self.say("move piano to 9", {"type": "move", "target": "piano", "duration": "9"})
        self.assertIn("9:00 PM – 10:00 PM", out)
        out = self.say("for an hour and a half", {"type": "add", "title": "For 90 minutes"})
        self.assertNotIn("Added", out)
        piano = next(s for s in self.m.day_plan.spans if s.title == "Piano")
        self.assertEqual(piano.duration_minutes, 90)

    def test_items_on_other_days_can_be_referred_to(self):
        self.say("email the recruiter tomorrow at 8", {"type": "add", "title": "Email the recruiter", "start": "8:00 AM",
                                                         "duration": "30 minutes", "day": "tomorrow"})
        out = self.say("walk tmrw at 7:30 for an hour", {"type": "add", "title": "Walk", "start": "7:30 AM",
                                                          "duration": "1 hour", "day": "tomorrow"})
        self.assertIn("To make room: Email the recruiter now 8:30 AM – 9:00 AM", out)  # slid, not a question
        out = self.say("actually move the recruiter email to 9:30", {"type": "move", "target": "recruiter email",
                                                                      "start": "9:30 AM"})
        self.assertIn("Moved", out)
        tomorrow = self.sched.build_day_plan(DAY + timedelta(days=1))[0]
        email = next(s for s in tomorrow.spans if s.title == "Email the recruiter")
        self.assertEqual(email.start_dt.strftime("%H:%M"), "09:30")

    def test_guards_against_common_misparses(self):
        self.say("composing 7 to 8", {"type": "add", "title": "Composing", "start": "7", "end": "8"})
        self.say("reading 9 to 10", {"type": "add", "title": "Reading", "start": "9", "end": "10"})
        # "back" is later, whatever sign the model wrote
        out = self.say("slide composing back 30 mins", {"type": "move", "target": "composing", "shift": "-30 minutes"})
        self.assertIn("7:30 PM", out)
        # naming an item that's already planned moves it rather than adding a second one
        out = self.say("put composing right after the dentist", {"type": "add", "title": "Composing", "after": "dentist"})
        self.assertEqual([s.title for s in self.m.day_plan.spans].count("Composing"), 1)
        self.assertEqual(occupant(self.m.day_plan, "18:30"), "Composing")
        # "before X" in the start field is a placement, not a time
        out = self.say("stretch for 30 min before reading", {"type": "add", "title": "Stretch", "start": "before Reading",
                                                             "duration": "30 minutes"})
        self.assertEqual(occupant(self.m.day_plan, "20:30"), "Stretch")
        # a day off removes work, and only work
        out = self.say("I took the day off", {"type": "remove", "target": "work"}, {"type": "remove", "target": "reading"})
        titles = [s.title for s in self.m.day_plan.active_spans()]
        self.assertNotIn("Work", titles)
        self.assertIn("Reading", titles)

    def test_typos_and_self_corrections(self):
        out = self.say("meditatoin at 7pm for 20 mins", {"type": "add", "title": "Meditation", "start": "7pm", "duration": "20 mins"})
        self.assertIn("Added Meditation", out)
        out = self.say("I need to make meditation happen at some point", {"type": "add", "title": "Meditation"})
        self.assertNotIn("Moved", out)

    def test_no_is_never_yes(self):
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        self.say("ideas for tonight?", {"type": "suggest", "topic": "tonight"})
        out = self.say("nah don't do that, I'm wiped", {"type": "confirm"})
        self.assertNotIn("Done", out)
        self.assertEqual(self.brain.state.proposal, [])

    def test_what_the_user_sees_drops_obvious_am_pm(self):
        self.say("dinner at 7 for an hour", {"type": "add", "title": "Dinner", "start": "7", "duration": "1 hour"})
        self.assertTrue(self.shown.startswith("Added Dinner: 7:00 – 8:00 (1h)."), self.shown)  # it's 4:40 PM

    def test_no_length_given_uses_how_long_it_usually_takes(self):
        from thirties_core.models import EventSpan, SpanCategory, SpanStatus
        for d in range(1, 4):
            start = at("10:00", -d)
            span = EventSpan(f"r{d}", "Reading", start, start + timedelta(hours=1), SpanCategory.ACTIVITY)
            span.status, span.time_source = SpanStatus.DONE, "reported"
            span.actual_start, span.actual_end = start, start + timedelta(minutes=45)
            self.sched.state_db.upsert_span(span)
        out = self.say("reading at 8", {"type": "add", "title": "Reading", "start": "8"})
        self.assertIn("Added Reading: 8:00 PM – 8:45 PM (45m). (about as long as it usually takes you)", out)
        reading = next(s for s in self.m.day_plan.spans if s.title == "Reading")
        self.assertEqual(reading.duration_minutes, 45)  # the real length stays underneath

    def test_short_items_are_told_by_their_thirty(self):
        self.say("message brad at 7, 10 minutes", {"type": "add", "title": "Message Brad", "start": "7", "duration": "10 minutes"})
        out = self.say("and feed the cats at 7 too, 5 min", {"type": "add", "title": "Feed the cats", "start": "7", "duration": "5 min"})
        self.assertEqual(out.splitlines()[0], "Added Feed the cats at 7:00 PM, after Message Brad.")

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
        self.assertTrue(out.startswith("Done:\n"), out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("- ")]), 2, out)
        self.assertNotIn("(1h)", out)

    def test_replies_to_a_proposal_are_read_deterministically(self):
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        self.say("ideas for tonight?", {"type": "suggest", "topic": "tonight"})
        proposed = self.brain.state.proposal[1]["title"]
        out = self.say("the second one", {"type": "add", "title": proposed, "start": "8"})  # model re-adds it
        self.assertEqual(out.count("Added "), 1)
        self.assertIn(proposed, out)
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        self.say("more ideas?", {"type": "suggest", "topic": "tonight"})
        out = self.say("nah", "")
        self.assertIn("leave those off", out)
        self.assertEqual(self.brain.state.proposal, [])

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
        self.assertIn("low on energy", out)
        self.assertIn("already light", out)  # nothing heavy is left today: nothing to propose

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

    def test_show_my_day_never_becomes_small_talk(self):
        self.say("dinner at 7", {"type": "add", "title": "Dinner", "start": "7", "duration": "1 hour"})
        out = self.say("can you show me the whole day as a clean list now?", {"type": "chat"})
        self.assertTrue(out.startswith("Here's today:"), out)
        self.assertTrue(out.endswith("- 7:00 PM – Dinner"), out)

    def test_answers_in_chunks_when_the_user_counts_in_chunks(self):
        out = self.say("when am I free tonight?", {"type": "question", "topic": "free time tonight"})
        self.assertNotIn("chunk", out)
        out = self.say("where can I fit something that takes three chunks tonight?",
                       {"type": "question", "topic": "free time tonight"})
        self.assertRegex(out, r"\(\d+ chunks\)")
        out = self.say("meeting at 8, three chunks", {"type": "add", "title": "Meeting", "start": "8", "duration": "3 chunks"})
        self.assertIn("Added Meeting: 8:00 PM – 9:30 PM (3 chunks).", out)

    def test_fill_the_evening_until_a_time(self):
        self.rt.pick_replies.append('{"picks": [2, 1]}')
        out = self.say("could you fill up my evening with stuff from my backlog, until 9?",
                       {"type": "question", "topic": "backlog"})
        self.assertTrue(out.startswith("How about:"), out)
        starts = [l.split(" – ")[0].split(". ", 1)[1] for l in out.splitlines() if l[:1].isdigit()]
        self.assertEqual(starts, ["5:00 PM", "6:30 PM", "7:30 PM", "8:30 PM"], out)  # around the dentist, until 9
        self.assertIn("4. 8:30 PM – Write wedding thank-you notes\n", out)
        out = self.say("sure", {"type": "confirm"})
        self.assertTrue(out.startswith("Done:"), out)

    def test_readding_proposed_items_takes_them_as_proposed(self):
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        out = self.say("fill up my free time this evening until 9 from the backlog", {"type": "suggest", "topic": "evening"})
        self.assertNotIn("You're free", out)
        proposal = list(self.brain.state.proposal)
        out = self.say("those sound good, let's do all of them. show me the evening",
                       *[{"type": "add", "title": p["title"], "start": p["start"], "duration": "30 minutes"} for p in proposal],
                       {"type": "question", "topic": "schedule"})
        self.assertTrue(out.startswith("Done:"), out)
        self.assertNotIn("Added", out)
        first = next(s for s in self.m.day_plan.spans if s.title == proposal[0]["title"])
        self.assertEqual(first.duration_minutes, 60)

    def test_fill_an_explicit_window_without_a_placeholder_item(self):
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        out = self.say("can you fill the four chunks from 7:00 PM to 9:00 PM with tasks from my backlog?",
                       {"type": "add", "title": "Backlog tasks", "start": "7:00 PM", "end": "9:00 PM"})
        self.assertTrue(out.startswith("How about:"), out)
        self.assertNotIn("Backlog tasks", out)
        starts = [l.split(" – ")[0].split(". ", 1)[1] for l in out.splitlines() if l[:1].isdigit()]
        self.assertEqual(starts, ["7:00 PM", "8:00 PM"], out)

    def test_swap_one_item_of_a_pending_plan(self):
        self.rt.pick_replies.append('{"picks": [2, 1]}')
        self.say("could you fill up my evening with stuff from my backlog, until 9?", {"type": "suggest", "topic": "evening"})
        before = [p["title"] for p in self.brain.state.proposal]
        out = self.say("looks good, but not feeling the costco call today, swap it for something else",
                       {"type": "suggest", "topic": "evening"})
        after = [p["title"] for p in self.brain.state.proposal]
        self.assertTrue(out.startswith("Here's the plan with Costco customer service swapped out:"), out)
        self.assertEqual(len(after), len(before))
        self.assertNotIn("Costco customer service", after)
        self.assertEqual([p for p in after if p in before], [p for p in before if p != "Costco customer service"])
        out = self.say("perfect, thanks", {"type": "confirm"})
        self.assertTrue(out.startswith("Done:"), out)

    def test_you_pick_makes_the_plan(self):
        self.rt.pick_replies.append('{"picks": [1]}')
        out = self.say("idk, just pick whatever and put together a schedule for my evening",
                       {"type": "question", "topic": "what kind of activities"})
        self.assertTrue(out.startswith("Done:"), out)
        self.assertNotEqual(occupant(self.m.day_plan, "17:00"), None)

    def test_pick_for_me_accepts_a_pending_proposal(self):
        self.rt.pick_replies.append('{"picks": [1, 2]}')
        self.say("ideas for tonight?", {"type": "suggest", "topic": "tonight"})
        out = self.say("any of those work honestly, just pick a few and slot them in", {"type": "chat"})
        self.assertTrue(out.startswith("Done:"), out)

    def test_where_could_it_go_is_about_open_time(self):
        out = self.say("I have a client call tonight, at least 3 chunks. where could it go?",
                       {"type": "suggest", "topic": "tonight"})
        self.assertTrue(out.startswith("You're free"), out)
        self.assertIn("chunks", out)
        self.assertNotIn("5:00 PM", out)  # only an hour before the dentist: too short

    def test_answering_when_completes_the_pending_add(self):
        out = self.say("I want a walk soon", {"type": "add", "title": "Walk", "duration": "30 minutes"})
        self.assertEqual(out, "When should I put Walk?")
        out = self.say("5 pm. how much free time do I have before bed?", {"type": "chat"})
        self.assertIn("Added Walk: 5:00 PM – 5:30 PM", out)
        self.assertIn("You're free", out)

    def test_unparseable_output_falls_back(self):
        out = self.say("hmm", "sorry I can't do JSON")
        self.assertEqual(out, "Happy to help!")


class TestVoice(SlmTestCase):

    def setUp(self):
        super().setUp()
        self.say("taxes 8 to 9", {"type": "add", "title": "Taxes", "start": "8", "end": "9"})

    def test_natural_rewrite_is_used_when_facts_survive(self):
        self.rt.voice_replies.append("Woohoo, Taxes is done! Nice work.")
        out = self.say("did the taxes", {"type": "outcome", "target": "taxes", "status": "done"})
        self.assertEqual(out, "Woohoo, Taxes is done! Nice work.")

    def test_rewrite_that_drops_a_title_is_rejected(self):
        self.rt.voice_replies.append("Nice work, all done!")
        out = self.say("did the taxes", {"type": "outcome", "target": "taxes", "status": "done"})
        self.assertEqual(out, "Nice work: Taxes is done.")

    def test_rewrite_that_changes_a_time_is_rejected(self):
        self.rt.voice_replies.append("You're open from 4:30 PM to 7:00 PM!")
        out = self.say("when am I free tonight?", {"type": "question", "topic": "free time tonight"})
        self.assertNotIn("7:00 PM!", out)

    def test_long_replies_skip_the_rewrite(self):
        out = self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 minutes"})
        self.assertTrue(out.startswith("Added Walk"), out)
        self.assertEqual(self.rt.voice.seen, [])


class TestParsingHelpers(unittest.TestCase):

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"actions": []}\n```'), {"actions": []})
        self.assertEqual(extract_json('Sure! {"a": {"b": 1}} trailing'), {"a": {"b": 1}})
        self.assertIsNone(extract_json("no json here"))



class TestJoplinLists(SlmTestCase):

    def setUp(self):
        super().setUp()
        from unittest.mock import MagicMock
        self.api = FakeJoplinAPI({"n1": {"id": "n1", "body": "- [ ] Costco customer service\n"},
                                  "n2": {"id": "n2", "body": "- [ ] Text search for Natalie's piece\n"}})
        joplin = MagicMock()
        joplin.api_client = self.api
        self.m.tools.services.joplin = joplin
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.rt.json_replies = []
        natalie = TaskItem(id="t3c", source_notebook="3. Creative", title="Text search for Natalie's piece",
                           parent_note_title="Natalie Piece", parent_note_id="n2", is_checkbox_item=True, deferred_count=3)
        self.brain.tasks = [t for t in self.brain.tasks if t.id != "t3"] + [natalie]
        self.m.tools.services.tasks = [t for t in self.m.tools.services.tasks if t.id != "t3"] + [natalie]

    def test_add_to_a_list(self):
        out = self.say("add buy stamps to my errands/admin list", {"type": "add", "title": "Buy stamps"})
        self.assertEqual(out, "Added 'Buy stamps' to Errands/Admin.")
        self.assertIn("- [ ] Buy stamps", self.api.notes["n1"]["body"])

    def test_put_it_on_a_list_means_the_item_just_planned(self):
        self.say("message brad back at 5", {"type": "add", "title": "Message Brad back", "start": "5", "duration": "15 min"})
        out = self.say("also put it on my errands/admin list", {"type": "add", "title": "It"})
        self.assertEqual(out, "Added 'Message Brad back' to Errands/Admin.")
        self.api.notes["n1"]["body"] = "- [ ] Costco customer service\n"
        out = self.say("can you also make sure that's on my errands/admin list?", {"type": "add", "title": "Message Brad back"})
        self.assertEqual(out, "Added 'Message Brad back' to Errands/Admin.")

    def test_break_down_then_schedule_the_first_step(self):
        self.say("natalie text search at 5", {"type": "add", "title": "Text search for Natalie's piece", "start": "5"})
        self.rt.json_replies.append({"steps": ["List three poets", "Read one poem aloud", "Note lines that fit"]})
        out = self.say("ugh, that's too big, I can't get started", {"type": "chat"})
        self.assertIn("1. List three poets", out)
        self.assertIn("add these to Joplin", out)
        out = self.say("yes please", {"type": "confirm"})
        self.assertIn("Added 3 steps under Text search for Natalie's piece in Joplin", out)
        self.assertIn("    - [ ] List three poets", self.api.notes["n2"]["body"])
        self.assertIn('Want to start with "List three poets"', out)
        out = self.say("sure", {"type": "confirm"})
        self.assertIn("List three poets", out)



class TestMessages(SlmTestCase):

    def test_remind_me_parses_day_time_and_text_in_python(self):
        out = self.say("remind me next week to water my pet alligator", {"type": "add", "title": "Water alligator"})
        self.assertEqual(out, "Okay: Water my pet alligator, Monday, October 19 at 9:00 AM, on the plan, "
                              "and I'll ping you then.")
        out = self.say("hey remind me saturday at 3 to call grandma", {"type": "chat"})
        self.assertEqual(out, "Okay: Call grandma, Saturday, October 17 at 3:00 PM, on the plan, and I'll ping you "
                              "then.")
        out = self.say("ping me in 20 minutes about the laundry", {"type": "chat"})
        self.assertEqual(out, "Okay, I'll ping you Thursday, October 15 at 5:00 PM: The laundry")  # just a message

    def test_remind_me_what_is_a_question_and_today_gets_a_sensible_time(self):
        out = self.say("remind me what we agreed on for composing", {"type": "question", "topic": "past composing"})
        self.assertNotIn("ping you", out)
        out = self.say("remind me to pay the electric bill sometime today", {"type": "add", "title": "Electric bill"})
        self.assertEqual(out, "Okay: Pay the electric bill, Thursday, October 15 at 6:30 PM, on the plan, and I'll "
                              "ping you then.")

    def test_stop_asking_mutes_the_item_just_asked_about(self):
        self.say("taxes 2 to 3", {"type": "add", "title": "Taxes", "start": "2:00 PM", "end": "3:00 PM"})
        self.m.add_assistant_message("👋 Just checking in: did you get to Taxes?")
        out = self.say("not yet, but no need to keep asking, I'll let you know", {"type": "chat"})
        self.assertEqual(out, "I won't ask about Taxes again.")
        out = self.say("and stop checking in for today please", {"type": "chat"})
        self.assertIn("No more check-ins until", out)


    def test_yes_to_the_morning_rollover(self):
        y = DAY - timedelta(days=1)
        from tests.fixtures import TZ
        from datetime import datetime, time as _t
        from thirties_core.models import EventSpan, SpanCategory
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", datetime.combine(y, _t(19), TZ),
                                                  datetime.combine(y, _t(20), TZ), SpanCategory.TASK))
        self.m.add_assistant_message(self.m.execute_tool("rollover_proposal", {}))
        out = self.say("yes please", {"type": "chat"})
        self.assertIn("Rolled into today:\n- 7:00 PM – Taxes", out)


class TestFromTheMilestoneRun(SlmTestCase):
    """Failures the 0.1 milestone day sim found."""

    def test_starting_something_isnt_finishing_it(self):
        self.say("laundry at 5", {"type": "add", "title": "Laundry", "start": "5:00 PM", "duration": "1 hour"})
        out = self.say("starting laundry now", {"type": "outcome", "target": "laundry", "status": "done"})
        self.assertEqual(out, "Got it, Laundry is underway.")
        laundry = next(s for s in self.m.day_plan.spans if s.title == "Laundry")
        self.assertEqual(laundry.status.value, "planned")

    def test_done_words_win_over_starting_words(self):
        from thirties_core.brains.slm.brain import FINISHED_RE, STARTING_RE
        for text in ("flour order in, made the cutoff, starting on the bread", "sent the email, heading out now"):
            self.assertTrue(STARTING_RE.search(text) and FINISHED_RE.search(text), text)

    def test_a_reported_time_is_the_most_recent_one(self):
        set_debug_time(at("19:40"))
        self.say("dinner at 6:30", {"type": "add", "title": "Dinner", "start": "6:30 PM", "duration": "1 hour"})
        self.say("finished dinner at 7:30, started at 6:40", {"type": "outcome", "target": "dinner", "status": "done",
                                                               "started": "6:40", "finished": "7:30"})
        dinner = next(s for s in self.m.day_plan.spans if s.title == "Dinner")
        self.assertEqual(dinner.actual_start.strftime("%H:%M"), "18:40")  # this evening, not 6:40 this morning

    def test_a_question_about_one_list(self):
        from thirties_core.models import TaskItem
        self.brain.tasks = [TaskItem(id=f"h{i}", source_notebook="Tasks", title=t, is_checkbox_item=True,
                                     parent_note_title="Home", parent_note_id="n_home")
                            for i, t in enumerate(("Hide the cable", "Fix the door"))]
        out = self.say("what's on my home list?", {"type": "question", "topic": "home list"})
        self.assertEqual(out, "On Home:\n- Hide the cable\n- Fix the door")

    def test_calendar_events_need_no_checking_off(self):
        out = self.say("dentist done", {"type": "outcome", "target": "dentist", "status": "done"})
        self.assertEqual(out, "Dentist is on your calendar, so there's nothing to check off.")


class TestFromTheDaySim(unittest.TestCase):
    """Failures the day sim found on 0.0.1."""

    def setUp(self):
        set_debug_time(at("10:40"))
        self.addCleanup(set_debug_time, None)
        self.rt = FakeRuntime()
        self.sched = make_scheduler([event("show1", "15:00", "17:00", "SB Show", invite=True),
                                     event("show2", "19:30", "21:30", "SB Show", invite=True)])
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, list(TASKS), pending, self.sched, active_notes=[],
                                     brain=None, inference_engine=None)
        self.brain = SlmBrain(self.m.tools, tasks=list(TASKS), runtime=self.rt)
        self.m.brain = self.brain
        self.m._load(None)

    def say(self, text, *parse):
        self.rt.parse_replies.append(actions(*parse))
        self.m.send_user_message(text)
        return self.m.last_raw_reply

    def shows(self):
        return [s for s in self.m.day_plan.spans if s.title == "SB Show"]

    def test_yes_answers_the_morning_offer_not_an_invite(self):
        y = DAY - timedelta(days=1)
        from tests.fixtures import TZ
        from datetime import datetime, time as _t
        from thirties_core.models import EventSpan, SpanCategory
        self.sched.state_db.upsert_span(EventSpan("br", "Message Brad back", datetime.combine(y, _t(21, 30), TZ),
                                                  datetime.combine(y, _t(21, 45), TZ), SpanCategory.ACTIVITY))
        self.m.add_assistant_message(self.m.execute_tool("rollover_proposal", {}))
        out = self.say("yeah put that on today", {"type": "confirm"})
        self.assertIn("Rolled into today", out)
        self.assertTrue(all(s.tentative for s in self.shows()))  # the invites are still unanswered

    def test_declining_both_invites_by_name(self):
        out = self.say("decline both SB Show invites, not going", {"type": "remove", "target": "SB Show"})
        self.assertTrue(out.startswith("Declined SB Show (3:00 PM and 7:30 PM): that time is free."), out)
        self.assertEqual(self.shows(), [])

    def test_tomorrow_when_the_parse_drops_the_day(self):
        out = self.say("what does tomorrow look like?", {"type": "question", "topic": "schedule"})
        self.assertNotIn("Here's today", out)


class ParityTestCase(SlmTestCase):
    """Joplin with a Today note, an errands list and the user's morning routine (a template, ticked there)."""

    STEPS = ["Feed kitties", "Caffeinate", "Water bottle", "Breakfast", "Throne", "Shower", "PT", "Brush teeth"]
    TITLES = {"n_today": "Today To-Do", "n1": "Errands/Admin", "n_routine": "Morning Routine"}

    def setUp(self):
        super().setUp()
        from unittest.mock import MagicMock
        from thirties_core.lists import ListCatalog
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n1": {"id": "n1", "body": "- [ ] Costco customer service\n"},
                                  "n_routine": {"id": "n_routine", "body": "".join(f"- [x] {s}\n" for s in self.STEPS)}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": self.TITLES[nid], "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        self.m.tools.services.joplin = joplin
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_today", "today", "Today To-Do")
        catalog.set_role("n1", "list", "Errands/Admin")
        catalog.set_role("n_routine", "routine", "Morning Routine")
        self.sched.state_db.set_sync_meta(f"day_started:{DAY.isoformat()}", "done")  # this morning's reset ran

    def span(self, title):
        return next(s for s in self.m.day_plan.spans if s.title == title)


class TestParityWithGemini(ParityTestCase):
    """0.2: a Gemma user can do what a Gemini user can (today's list, urgency, item facts, routines)."""

    def test_put_things_on_todays_list(self):
        out = self.say("I want to get the costco customer service thing done, stick it on my list for today",
                       {"type": "today", "title": "costco customer service"})
        self.assertEqual(out, "Added to today's list: Costco customer service.")
        self.assertEqual(self.api.notes["n_today"]["body"], "- [ ] Costco customer service\n")
        self.say("laundry at 7", {"type": "add", "title": "Laundry", "start": "7", "duration": "1 hour"})
        out = self.say("and put that on the list for today too", {"type": "today", "title": "that"})
        self.assertEqual(out, "Already on it: Laundry.")  # what's planned today is on Today already (Block 0)

    def test_an_invented_list_item_is_dropped(self):
        out = self.say("what a day", {"type": "today", "title": "Buy milk"})
        self.assertNotIn("Buy milk", self.api.notes["n_today"]["body"])
        self.assertNotIn("Added", out)

    def test_thats_not_urgent_names_the_item_just_planned(self):
        self.say("costco customer service at 5", {"type": "add", "title": "Costco customer service", "start": "5",
                                                  "duration": "30 min"})
        out = self.say("eh, that's not urgent", {"type": "urgency", "target": "that", "urgency": "now"})
        self.assertEqual(out, "Noted: 'Costco customer service' is whenever.")  # the words win over the model's field
        task = next(t for t in self.m.tools.services.tasks if t.id == "t1")
        self.assertEqual(task.urgency, "whenever")

    def test_urgency_on_a_near_match_asks_first(self):
        out = self.say("the costco call has to happen by friday",
                       {"type": "urgency", "target": "costco call", "day": "friday"})
        self.assertEqual(out, "Do you mean Costco customer service?")
        task = next(t for t in self.m.tools.services.tasks if t.id == "t1")
        self.assertFalse(task.due)  # nothing written on a near match
        out = self.say("yes", {"type": "confirm"})
        self.assertTrue(out.startswith("Noted: 'Costco customer service' is by"), out)
        self.assertEqual(task.due, "2026-10-16")

    def test_after_the_shower_is_kept_and_applied_now(self):
        from thirties_core.facts import SpanFacts
        self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 min"})
        self.say("shower at 7", {"type": "add", "title": "Shower", "start": "7", "duration": "30 min"})
        out = self.say("oh the walk has to wait until after the shower",
                       {"type": "move", "target": "walk", "after": "shower"})  # a rule said as a move
        self.assertIn("Noted for Walk: after Shower.", out)
        self.assertIn("Moved Walk to 7:30 PM – 8:00 PM to keep it.", out)
        self.assertEqual(SpanFacts(self.sched.state_db).get(self.span("Walk").id).after, [self.span("Shower").id])

    def test_outside_things_wait_for_the_shower(self):
        self.say("walk at 5", {"type": "add", "title": "Walk", "start": "5", "duration": "30 min"})
        self.say("shower at 7", {"type": "add", "title": "Shower", "start": "7", "duration": "30 min"})
        out = self.say("remember, outside things wait until after the shower",
                       {"type": "remember", "text": "Outside things wait until after the shower"})
        self.assertIn("I'll keep outside things after Shower from now on.", out)
        self.assertEqual(occupant(self.m.day_plan, "19:30"), "Walk")
        self.assertIn("Outside things wait until after the shower", [n.text for n in self.m.tools.services.memory.notes()])

    def test_it_cant_move(self):
        from thirties_core.facts import SpanFacts
        self.say("cats dinner at 5", {"type": "add", "title": "Cats' dinner", "start": "5", "duration": "15 min"})
        out = self.say("that one's not allowed to move", {"type": "fact", "target": "that"})
        self.assertIn("Noted for Cats' dinner", out)
        self.assertTrue(SpanFacts(self.sched.state_db).get(self.span("Cats' dinner").id).anchored)

    def test_put_the_morning_routine_on_as_two_thirties(self):
        out = self.say("put my morning routine on", {"type": "routine", "title": "Morning routine"})
        self.assertTrue(out.startswith("Put Morning Routine on the plan: 5:00 PM: Feed kitties, Caffeinate, Water "
                                       "bottle, Breakfast; 5:30 PM: Throne, Shower, PT, Brush teeth."), out)
        self.assertIn("It's on today's list too.", out)
        self.assertEqual(len([s for s in self.m.day_plan.spans if s.title in self.STEPS]), 8)
        self.assertTrue(self.api.notes["n_today"]["body"].startswith("- [ ] Morning Routine\n    - [ ] Feed kitties"))
        self.assertEqual(self.api.notes["n_routine"]["body"], "".join(f"- [x] {s}\n" for s in self.STEPS))

    def test_a_routine_parsed_as_an_add_at_a_time(self):
        out = self.say("morning routine at 7 tonight, I'm off schedule",
                       {"type": "add", "title": "Morning routine", "start": "7"})
        self.assertIn("7:00 PM: Feed kitties", out)
        self.assertIn("7:30 PM: Throne", out)
        self.assertNotIn("Morning routine", [s.title for s in self.m.day_plan.spans])  # never one item

    def test_the_routine_goes_on_the_way_it_did_last_time(self):
        from tests.fixtures import TZ
        from datetime import datetime, time as _t
        from thirties_core.models import EventSpan, SpanCategory
        y = DAY - timedelta(days=1)
        layout = {"Feed kitties": "09:00", "Caffeinate": "09:00", "Water bottle": "09:00", "Breakfast": "09:00",
                  "Throne": "15:00", "Shower": "15:00", "PT": "15:00", "Brush teeth": "15:00"}
        for i, (title, hhmm) in enumerate(layout.items()):
            h, m = (int(x) for x in hhmm.split(":"))
            start = datetime.combine(y, _t(h, m + (i % 4) * 5), TZ)
            self.sched.state_db.upsert_span(EventSpan(f"y{i}", title, start, start + timedelta(minutes=5),
                                                      SpanCategory.TASK))
        set_debug_time(at("08:10"))
        out = self.say("put the morning routine on please", {"type": "routine", "title": "morning routine"})
        self.assertIn("9:00 AM: Feed kitties, Caffeinate, Water bottle, Breakfast; 3:00 PM: Throne, Shower, PT, "
                      "Brush teeth.", out)
        set_debug_time(at("10:10"))  # the morning's thirty has passed: after work on a work day (B3's fallback)
        note = self.brain._routine_note("morning")
        parts = self.brain._routine_parts(note, self.STEPS, None)
        self.assertEqual([p["start"] for p in parts], ["4:00 PM", "4:30 PM"])
        self.m.tools.execute("remove_event", {"event": "Work"})  # a day off: the next free thirty, 3 PM kept
        parts = self.brain._routine_parts(note, self.STEPS, None)
        self.assertEqual([p["start"] for p in parts], ["10:30 AM", "3:00 PM"])


class TestHelpersForGemma(ParityTestCase):
    """ROADMAP C3 and "After 0.1" 2: separate the requests, parse each, check nothing was lost."""

    def test_each_request_is_parsed_on_its_own(self):
        self.rt.parse_replies.extend([actions({"type": "move", "target": "work", "end": "4:30"}),
                                      actions({"type": "add", "title": "Reading", "start": "7", "duration": "1 hour"})])
        self.m.send_user_message("work ran until 4:30, and put reading at 7 for an hour")
        out = self.m.last_raw_reply
        self.assertIn("Work now ends at 4:30 PM.", out)
        self.assertIn("Added Reading: 7:00 PM – 8:00 PM", out)
        asked = [s.split('Message: "')[1] for s in self.rt.parse.seen[-2:]]
        self.assertEqual(asked, ['work ran until 4:30"', 'put reading at 7 for an hour"'])

    def test_a_part_nothing_covered_is_retried_then_asked_about(self):
        self.rt.parse_replies.extend([actions({"type": "chat"}),
                                      actions({"type": "add", "title": "Reading", "start": "7", "duration": "1 hour"}),
                                      actions({"type": "chat"})])  # the retry of the lost part
        self.m.send_user_message("move the budget review to thursday, and put reading at 7 for an hour")
        out = self.m.last_raw_reply
        self.assertIn("Added Reading", out)
        self.assertTrue(out.endswith('I didn\'t catch what to do about "move the budget review to thursday". Could '
                                     'you say that part again?'), out)
        self.assertEqual(len(self.rt.parse.seen), 3)  # two requests, then the lost one again on its own

    def test_a_lost_part_found_by_the_retry_is_done(self):
        self.rt.parse_replies.extend([actions({"type": "add", "title": "Reading", "start": "7", "duration": "1 hour"}),
                                      actions({"type": "move", "target": "work", "end": "4:30"})])
        self.brain.SPLIT_REQUESTS = False  # the old one-shot parse, which dropped the work change
        self.m.send_user_message("put reading at 7 for an hour and work ran until 4:30")
        out = self.m.last_raw_reply
        self.assertIn("Added Reading", out)
        self.assertIn("Work now ends at 4:30 PM.", out)

    def test_dinner_with_a_book_alongside_is_one_dinner(self):
        out = self.say("Dinner at 7, cooking and eating, listening to Project Hail Mary",
                       {"type": "add", "title": "Dinner", "start": "7", "duration": "1 hour"},
                       {"type": "add", "title": "Cooking and eating"},
                       {"type": "add", "title": "Listening to Project Hail Mary"})
        titles = sorted(s.title for s in self.m.day_plan.spans if not s.is_envelope and s.source != "calendar")
        self.assertEqual(titles, ["Dinner", "Project Hail Mary"], out)
        book = self.span("Project Hail Mary")
        self.assertEqual((book.start_dt, book.end_dt), (self.span("Dinner").start_dt, self.span("Dinner").end_dt))

    def test_low_energy_offers_one_lighter_plan_applied_on_sure(self):
        self.say("grant report at 7", {"type": "add", "title": "Write the grant report", "start": "7",
                                        "duration": "1 hour"})
        self.say("call mom at 8:30", {"type": "add", "title": "Call mom", "start": "8:30", "duration": "30 min"})
        out = self.say("ugh I'm wiped", {"type": "energy", "level": 1})
        self.assertEqual(out, "Noted that you're low on energy. For a lighter rest of today, I'd move these to "
                              "tomorrow:\n- Write the grant report (7:00 PM)\nOkay?")
        out = self.say("sure", {"type": "chat"})
        self.assertTrue(out.startswith("Moved to tomorrow: Write the grant report. Go easy on yourself."), out)
        titles = [s.title for s in self.m.day_plan.spans]
        self.assertNotIn("Write the grant report", titles)
        self.assertIn("Call mom", titles)
        tomorrow = self.m.tools.plan_for("tomorrow")
        self.assertIn("Write the grant report", [s.title for s in tomorrow.spans])

    def easy_task(self):
        bank = TaskItem(id="t7", source_notebook="1. Tasks", title="Call the bank", parent_note_title="Errands/Admin",
                        parent_note_id="n1", is_checkbox_item=True)
        self.brain.tasks.append(bank)

    def test_you_pick_when_tired_picks_easy_things(self):
        self.easy_task()
        out = self.say("I'm fried, you pick something for tonight", {"type": "energy", "level": 1})
        self.assertIn("Call the bank", out)
        self.assertIn("Call the bank", [s.title for s in self.m.day_plan.spans])  # "you pick": made, not asked
        for hard in ("Fix the media server", "Write wedding thank-you notes", "Text search for Natalie's piece"):
            self.assertNotIn(hard, out)

    def test_ideas_when_tired_are_one_easy_plan_to_say_yes_to(self):
        self.easy_task()
        out = self.say("I'm beat. any ideas for tonight?", {"type": "suggest", "topic": "tonight"})
        self.assertTrue(out.startswith("Here's an easy plan:"), out)
        self.assertTrue(out.endswith("Want it?"), out)
        self.assertNotIn("say the number", out)
        out = self.say("sure", {"type": "confirm"})
        self.assertIn("Added Call the bank", out)


if __name__ == "__main__":
    unittest.main()
