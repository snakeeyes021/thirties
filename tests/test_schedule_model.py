"""0.2 Block 3, the schedule model: items that share time, placement with judgement, pushing the day back, and times
(items 17-20; frictions S2, S4-S7, T4 and soak frictions 1 and 3)."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.clock import format_clock, real_times, to_thirties
from thirties_core.day_layout import build_segments, card_rows, segment_context, segment_title, status_label
from thirties_core.models import SpanStatus
from thirties_core.placement import candidate_slots, early_tick_times, fullness, part_of_day
from thirties_core.plan_tools import NOT_DONE, PlanTools
from tests.test_slm_brain import SlmTestCase


def tools_for(events=None, now="12:00") -> PlanTools:
    set_debug_time(at(now))
    sched = make_scheduler(events)
    plan, pending = sched.build_day_plan(DAY)
    return PlanTools(plan, pending, sched)


def span(tools: PlanTools, title: str):
    return next(s for s in tools.day_plan.spans if s.title == title)


def times(tools: PlanTools, title: str):
    s = span(tools, title)
    return format_clock(s.start_dt), format_clock(s.end_dt)


class ScheduleCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_debug_time, None)


class TestSharedTime(ScheduleCase):

    def test_items_sharing_time_are_one_entry_in_the_schedule(self):
        t = tools_for()
        t.execute("add_event", {"title": "Redfox dev", "start": "5:00 PM", "duration": "2 hours"})
        t.execute("add_event", {"title": "Thirties dev", "start": "5:00 PM", "duration": "2 hours"})
        t.execute("add_event", {"title": "Message Brad", "start": "5:30 PM", "duration": "15 min"})
        t.execute("add_event", {"title": "Dinner", "start": "7:30 PM", "duration": "1 hour"})
        text = t.list_schedule()
        line = next(l for l in text.splitlines() if "Redfox dev" in l)
        self.assertTrue(line.startswith("- 5:00 PM – 7:00 PM: Redfox dev [activity] id="), line)
        self.assertIn(" + Thirties dev [activity] id=", line)
        self.assertIn(" + Message Brad (5:30 PM – 6:00 PM) [activity] id=", line)
        self.assertTrue(line.endswith("(sharing this time: one entry)"))
        self.assertEqual(sum("Thirties dev" in l for l in text.splitlines()), 1)  # never listed twice (S3)
        self.assertIn("- 7:30 PM – 8:30 PM: Dinner [activity]", text)  # back to back is not sharing

    def test_a_fixed_thing_is_reported_never_overwritten(self):
        t = tools_for([event("dentist", "15:00", "16:00", "Dentist")])
        t.user_message = "gym from 2:30 to 4, replace whatever's there"
        out = t.execute("add_event", {"title": "Gym", "start": "2:30 PM", "end": "4:00 PM", "replace_overlaps": True})
        self.assertTrue(out.startswith(NOT_DONE) and "fixed calendar commitment" in out, out)
        out = t.execute("add_event", {"title": "Podcast", "start": "3:00 PM", "duration": "30 min", "alongside": True})
        self.assertFalse(out.startswith(NOT_DONE), out)  # really during it: allowed when said
        t.execute("add_event", {"title": "Cats' dinner", "start": "5:00 PM", "duration": "15 min", "fixed": True})
        out = t.execute("add_event", {"title": "Call mom", "start": "5:00 PM", "duration": "30 min", "fixed": True})
        self.assertTrue(out.startswith(NOT_DONE) and "which is fixed" in out, out)

    def test_pushing_others_along_only_when_asked(self):
        t = tools_for()
        t.execute("add_event", {"title": "Reading", "start": "5:00 PM", "duration": "1 hour"})
        t.user_message = "laundry at 5"
        out = t.execute("add_event", {"title": "Laundry", "start": "5:00 PM", "duration": "30 min", "push_later": True})
        self.assertTrue(out.startswith(NOT_DONE) and "push_later" in out, out)
        self.assertEqual(times(t, "Reading"), ("5:00 PM", "6:00 PM"))
        t.user_message = "laundry at 5, push the rest back"
        out = t.execute("add_event", {"title": "Laundry", "start": "5:00 PM", "duration": "30 min", "push_later": True})
        self.assertIn("Reading now 5:30 PM", out)

    def test_moving_an_item_onto_another_leaves_the_other_alone(self):
        t = tools_for()
        t.execute("add_event", {"title": "Reading", "start": "5:00 PM", "duration": "1 hour"})
        t.execute("add_event", {"title": "Piano", "start": "7:00 PM", "duration": "1 hour"})
        out = t.execute("update_event", {"event": "Piano", "start": "5:30 PM"})
        self.assertIn("alongside Reading", out)
        self.assertEqual(times(t, "Reading"), ("5:00 PM", "6:00 PM"))


class TestOverlapOrPush(ScheduleCase):
    """10-30, the user: "Anything can overlap anything, but the assistant will need to know when the user is asking
    ... to OVERLAP with the thing already there or to push it and everything else behind it back." """

    def day(self):
        t = tools_for()
        for title, start, minutes in (("Laundry", "3:00 PM", 30), ("Shower", "5:00 PM", 30), ("Glass", "5:30 PM", 30),
                                      ("Redfox dev", "7:00 PM", 120)):
            t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min"})
        return t

    def test_push_words_move_what_is_there_and_what_follows(self):
        t = self.day()
        t.user_message = "throw my call with Brad to 5pm and push everything to make room"
        out = t.execute("add_event", {"title": "Call Brad", "start": "5:00 PM", "duration": "30 min", "fixed": False})
        self.assertIn("Shower now 5:30 PM", out)  # no push_later from the model: the user's words were enough
        self.assertEqual(times(t, "Glass"), ("6:00 PM", "6:30 PM"))
        t.user_message = "throw my shower to 3pm and push everything to make room"
        out = t.execute("update_event", {"event": "Shower", "start": "3:00 PM"})
        self.assertIn("Laundry now 3:30 PM", out)

    def test_unclear_landings_are_flagged_not_refused(self):
        t = self.day()
        t.user_message = "message mom at 5"
        out = t.execute("add_event", {"title": "Message mom", "start": "5:00 PM", "duration": "30 min"})
        self.assertTrue(out.startswith("Added"), out)  # never a failure
        self.assertIn("alongside Shower", out)
        self.assertIn("push_later=true moves Shower and what follows", out)
        self.assertEqual(times(t, "Shower"), ("5:00 PM", "5:30 PM"))  # nothing moved on a guess

    def test_together_words_background_items_and_kept_answers_need_no_question(self):
        t = self.day()
        t.user_message = "podcast while I shower"
        out = t.execute("add_event", {"title": "Plan the week", "start": "5:00 PM", "duration": "30 min"})
        self.assertNotIn("push_later", out)  # said together
        t.user_message = "message brad at 7"
        out = t.execute("add_event", {"title": "Message Brad", "start": "7:00 PM", "duration": "15 min"})
        self.assertIn("alongside Redfox dev", out)
        self.assertNotIn("push_later", out)  # a dev session is a backdrop the user stacks things on
        t.user_message = "taxes at 5:30"
        self.assertIn("push_later", t.execute("add_event", {"title": "Taxes", "start": "5:30 PM", "duration": "30 min"}))
        out = t.execute("set_sharing", {"first": "Glass", "second": "anything", "together": True})
        self.assertEqual(out, "Noted: glass and anything can share time.")
        t.execute("remove_event", {"event": "Taxes"})
        self.assertNotIn("push_later", t.execute("add_event", {"title": "Taxes", "start": "5:30 PM",
                                                               "duration": "30 min"}))
        t.execute("set_sharing", {"first": "Redfox dev", "second": "Thirties dev", "together": False})
        out = t.execute("add_event", {"title": "Thirties dev", "start": "7:00 PM", "duration": "1 hour"})
        self.assertIn("push_later=true moves Redfox dev", out)  # "dev" and "dev" can't, as the user said
        from thirties_core.sync_engine import SyncEngine
        self.assertIn("sharing_rules", SyncEngine._stores(type("E", (), {"state_db": t.scheduler.state_db})()))

    def test_kinds(self):
        from thirties_core.sharing import kind_of, wants_push
        self.assertEqual(kind_of("Redfox dev"), "dev")
        self.assertEqual(kind_of("Shower"), "shower")
        self.assertEqual(kind_of("anything"), "*")
        self.assertTrue(wants_push("lunch with Sam at 1, push the rest back"))
        self.assertFalse(wants_push("push my shower to 3"))  # moving one item, not making room
        self.assertFalse(wants_push("audiobook during dinner, push everything"))


class TestDayLayout(ScheduleCase):

    def test_shared_time_is_one_card_with_work_shown_under_it(self):
        t = tools_for()
        t.execute("add_event", {"title": "Morning routine", "start": "9:00 AM", "duration": "1 hour"})
        t.execute("add_event", {"title": "Caffeinate", "start": "9:30 AM", "duration": "10 min"})
        t.execute("add_event", {"title": "Redfox dev", "start": "5:00 PM", "duration": "1 hour"})
        t.execute("add_event", {"title": "Thirties dev", "start": "5:00 PM", "duration": "1 hour"})
        plan = t.day_plan
        segs = build_segments(plan)
        routine = next(s for s in segs if any(x.title == "Morning routine" for x in s.spans))
        self.assertEqual(len(routine.blocks), 2)  # one card, not three pieces around the nested item
        self.assertEqual(segment_title(plan, routine), "Morning routine + Caffeinate")
        self.assertEqual(segment_context(routine), "during Work")  # T4: work isn't hidden under it
        work = [s for s in segs if s.is_envelope and s.spans[0].title == "Work"]
        self.assertEqual(len(work), 2)  # work runs on either side, each saying what it's part of
        self.assertTrue(segment_context(work[0]).startswith("Part of Work"))
        dev = next(s for s in segs if any(x.title == "Redfox dev" for x in s.spans))
        self.assertEqual(segment_title(plan, dev), "Redfox dev + Thirties dev")
        self.assertIsNone(segment_context(dev))

    def test_expanded_rows_show_each_item_and_its_status(self):
        t = tools_for(now="20:06")
        t.execute("add_event", {"title": "Clip cat nails", "start": "9:00 PM", "duration": "1 hour"})
        t.execute("add_event", {"title": "Water plants", "start": "9:00 PM", "duration": "1 hour"})
        t.execute("log_outcome", {"event": "Water plants", "status": "done", "started": "9:00 PM",
                                  "finished": "10:00 PM"})
        plan = t.day_plan
        seg = next(s for s in build_segments(plan) if len(s.spans) == 2)
        rows = card_rows(plan, seg)
        self.assertEqual([(r.title, r.subtitle, r.status) for r in rows],
                         [("Clip cat nails", "9:00 PM – 10:00 PM", "Planned"),
                          ("Water plants", "9:00 PM – 10:00 PM", "Done")])  # soak 3: not "A + B", "Planned" twice
        self.assertEqual(status_label(seg.spans), "1 of 2 done")
        single = next(s for s in build_segments(plan) if s.is_envelope and s.spans[0].title == "Sleep")
        self.assertEqual({r.status for r in card_rows(plan, single)}, {None})  # sleep has no badge

    def test_moot_ongoing_and_someone_elses_events_read_sensibly(self):
        from thirties_core.models import EventSpan, SpanCategory
        t = tools_for(now="20:06")
        t.execute("add_event", {"title": "Lunch", "start": "12:00 PM", "duration": "30 min"})
        t.execute("add_event", {"title": "Torquio dev", "start": "6:00 PM", "duration": "1 hour"})
        span(t, "Lunch").status = SpanStatus.UNKNOWN  # gone moot with nothing said
        t.execute("log_outcome", {"event": "Torquio dev", "status": "done"})
        plan = t.day_plan
        self.assertEqual(status_label([span(t, "Lunch")]), "Not reported")  # it used to raise
        dev = [span(t, "Torquio dev")]
        self.assertEqual(status_label(dev), "Done")
        self.assertEqual(status_label(dev, lambda s: s.title.endswith("dev")), "Worked on")  # a session, it goes on
        plan.spans.append(EventSpan("cal-r", "Rehearsal", at("19:00"), at("21:00"), SpanCategory.CALENDAR,
                                    source="calendar", shown_only=True))
        segs = build_segments(plan)
        noted = [s for s in segs if "Rehearsal" in (segment_context(s, plan) or "")]
        self.assertEqual(len(noted), 1)  # once, where it starts: never a card of its own or on every thirty
        self.assertEqual(segment_context(noted[0], plan), "Rehearsal 7:00 PM – 9:00 PM (not yours)")
        self.assertFalse(any("Rehearsal" in (segment_title(plan, s) or "") for s in segs))


class TestEarlyTicks(ScheduleCase):

    def test_ticked_before_their_time_they_keep_to_blocks_back_to_back(self):
        t = tools_for(now="20:06")
        t.execute("add_event", {"title": "Clip cat nails", "start": "9:00 PM", "duration": "30 min"})
        t.execute("add_event", {"title": "Water plants", "start": "9:00 PM", "duration": "30 min"})
        out = t.execute("log_outcome", {"event": "Clip cat nails", "status": "done"})
        self.assertIn("7:30 PM – 8:00 PM, estimated", out)  # not 7:36 – 8:06 across two thirties (10-24)
        out = t.execute("log_outcome", {"event": "Water plants", "status": "done"})
        self.assertIn("7:00 PM – 7:30 PM, estimated", out)  # back to back, not the same half hour
        nails = span(t, "Clip cat nails")
        self.assertEqual((nails.planned_start, nails.planned_end), (at("21:00"), at("21:30")))  # the plan is kept
        segs = [s for s in build_segments(t.day_plan) if any(x.title in ("Clip cat nails", "Water plants")
                                                               for x in s.spans)]
        self.assertEqual([(segment_title(t.day_plan, s), len(s.blocks)) for s in segs],
                         [("Water plants", 1), ("Clip cat nails", 1)])  # two clean cards

    def test_short_and_late_ticks(self):
        t = tools_for(now="20:02")
        t.execute("add_event", {"title": "Feed cats", "start": "9:00 PM", "duration": "5 min"})
        start, end = early_tick_times(t.day_plan, span(t, "Feed cats"), at("20:02"))
        self.assertEqual((start, end), (at("19:55"), at("20:00")))  # inside one thirty
        t.execute("add_event", {"title": "Laundry", "start": "9:00 PM", "duration": "1 hour"})
        start, end = early_tick_times(t.day_plan, span(t, "Laundry"), at("20:20"))
        self.assertEqual((start, end), (at("19:30"), at("20:30")))  # nearest block line, its own length
        set_debug_time(at("21:40"))
        out = t.execute("log_outcome", {"event": "Laundry", "status": "done"})
        self.assertIn("9:00 PM – 9:40 PM", out)  # ticked during it: it ended at the tick, as before


class TestCalendarOutcomes(ScheduleCase):

    def test_a_calendar_event_can_be_ticked_without_touching_the_calendar(self):
        t = tools_for([event("standup", "09:00", "09:30", "Standup")], now="09:40")
        out = t.execute("log_outcome", {"event": "Standup", "status": "done"})
        self.assertIn("Marked Standup done (9:00 AM – 9:30 AM, planned)", out)
        self.assertIn("calendar itself is unchanged", out)
        plan, _ = t.scheduler.build_day_plan(DAY)  # rebuilt from the calendar: the outcome is laid back over it
        standup = next(s for s in plan.spans if s.title == "Standup")
        self.assertEqual(standup.status, SpanStatus.DONE)
        self.assertEqual((standup.start_dt, standup.end_dt), (at("09:00"), at("09:30")))
        out = t.execute("log_outcome", {"event": "Standup", "status": "done", "started": "9:05 AM",
                                        "finished": "9:50 AM"})
        self.assertIn("9:05 AM – 9:50 AM, reported", out)

    def test_calendar_outcomes_sync(self):
        t = tools_for([event("standup", "09:00", "09:30", "Standup")], now="09:40")
        t.execute("log_outcome", {"event": "Standup", "status": "skipped"})
        from thirties_core.calendar_outcomes import CalendarOutcomes
        records = CalendarOutcomes(t.scheduler.state_db).all_records()
        other = make_scheduler([event("standup", "09:00", "09:30", "Standup")])
        self.assertTrue(CalendarOutcomes(other.state_db).merge_record(records[0]))
        self.assertFalse(CalendarOutcomes(other.state_db).merge_record(records[0]))
        plan, _ = other.build_day_plan(DAY)
        self.assertEqual(next(s for s in plan.spans if s.title == "Standup").status, SpanStatus.SKIPPED)
        from thirties_core.sync_engine import SyncEngine
        self.assertIn("calendar_outcomes", SyncEngine._stores(type("E", (), {"state_db": other.state_db})()))


class TestPushBack(ScheduleCase):

    def day(self, now="15:32"):
        t = tools_for([event("call", "19:00", "19:30", "Call with Sam")], now=now)
        t.execute("remove_event", {"event": "Work"})
        for title, start, minutes in (("Breakfast", "10:00 AM", 30), ("Shower", "2:00 PM", 30), ("Laundry", "3:00 PM", 30),
                                      ("Composing", "3:00 PM", 60), ("Glass", "5:00 PM", 30), ("Snack", "6:30 PM", 30),
                                      ("Litter", "9:00 PM", 60)):
            t.execute("add_event", {"title": title, "start": start, "duration": f"{minutes} min"})
        t.execute("log_outcome", {"event": "Shower", "status": "done", "started": "2:00 PM", "finished": "2:30 PM"})
        return t

    def test_push_everything_back_includes_what_is_past_and_unmarked(self):
        t = self.day()
        out = t.execute("push_back", {"by": "2 hours"})
        self.assertEqual(times(t, "Laundry"), ("5:00 PM", "5:30 PM"))   # 10-26: "where's the stuff for 3 o'clock?"
        self.assertEqual(times(t, "Composing"), ("5:00 PM", "6:00 PM"))  # still alongside Laundry, the one now
        self.assertEqual(times(t, "Glass"), ("7:30 PM", "8:00 PM"))      # 7:00 is the fixed call: right after it
        self.assertEqual(times(t, "Snack"), ("9:00 PM", "9:30 PM"))      # and what follows keeps its spacing
        self.assertEqual(times(t, "Shower"), ("2:00 PM", "2:30 PM"))     # done stays
        self.assertEqual(times(t, "Breakfast"), ("10:00 AM", "10:30 AM"))  # long past: behind, not pushed
        self.assertEqual(times(t, "Call with Sam"), ("7:00 PM", "7:30 PM"))
        self.assertEqual(times(t, "Litter"), ("9:00 PM", "10:00 PM"))  # would run into bedtime: stays, and is named
        self.assertIn("Not marked, so moved along as still to do: Laundry.", out)
        self.assertIn("Would run into bedtime, so it stays where it was: Litter.", out)
        t.execute("undo_redraw", {})
        self.assertEqual(times(t, "Laundry"), ("3:00 PM", "3:30 PM"))

    def test_push_back_takes_whole_blocks(self):
        # 10-31: at 9:02, "push everything back" an hour moved Caffeinate (8:06) and left Feed kitties (8:00) behind.
        t = tools_for([], now="15:02")
        t.execute("remove_event", {"event": "Work"})
        for title, start in (("Feed kitties", "2:00 PM"), ("Caffeinate", "2:06 PM"), ("Lunch", "1:30 PM")):
            t.execute("add_event", {"title": title, "start": start, "duration": "6 min", "again": True})
        t.execute("push_back", {"by": "1 hour"})
        self.assertEqual(times(t, "Feed kitties")[0], "3:00 PM")
        self.assertEqual(times(t, "Caffeinate")[0], "3:06 PM")
        self.assertEqual(times(t, "Lunch")[0], "1:30 PM")  # the block before: behind, not pushed

    def test_push_back_from_a_time_and_bad_amounts(self):
        t = self.day()
        t.execute("push_back", {"by": "1 hour", "since": "6:00 PM"})
        self.assertEqual(times(t, "Snack"), ("7:30 PM", "8:00 PM"))
        self.assertEqual(times(t, "Glass"), ("5:00 PM", "5:30 PM"))
        self.assertTrue(t.execute("push_back", {"by": "-1 hour"}).startswith(NOT_DONE))
        self.assertTrue(t.execute("push_back", {}).startswith(NOT_DONE))


class TestPlacement(ScheduleCase):

    def cramped_afternoon(self):
        t = tools_for(now="12:10")
        t.execute("remove_event", {"event": "Work"})
        for title, start in (("Errands", "12:30 PM"), ("Calls", "1:30 PM"), ("Taxes", "2:30 PM"), ("Laundry", "4:00 PM"),
                             ("Dinner", "6:00 PM")):
            t.execute("add_event", {"title": title, "start": start, "duration": "1 hour"})
        return t

    def test_options_say_what_is_around_them_and_how_full_the_day_is(self):
        t = self.cramped_afternoon()
        out = t.execute("slot_options", {"duration": "1 hour"})
        self.assertIn("nothing is placed yet", out)
        self.assertIn("right after Dinner", out)
        self.assertIn("in the middle, with", out)
        self.assertIn("right before bedtime", out)
        self.assertIn("evening: 2 of 8 blocks planned", out)
        self.assertRegex(out, r"How full: .*afternoon \d+ of \d+ blocks planned")
        self.assertEqual(span(t, "Dinner").start_dt, at("18:00"))  # read-only

    def test_later_means_a_real_gap_not_the_next_free_block(self):
        t = self.cramped_afternoon()
        slots = candidate_slots(t, t.day_plan, 60, at("12:10"), later=True)
        self.assertTrue(slots)
        self.assertTrue(all(s.start >= at("13:30") for s in slots))  # an hour or more out
        self.assertTrue(all(s.stretch[1] - s.stretch[0] >= timedelta(minutes=90) for s in slots))  # room to breathe
        self.assertNotIn(at("15:30"), [s.start for s in slots])  # the 3:30 squeeze between Taxes and Laundry
        now_slots = candidate_slots(t, t.day_plan, 30, at("12:10"))
        self.assertEqual(now_slots[0].start, at("12:00"))
        self.assertIn("now", now_slots[0].where)

    def test_fullness_and_parts_of_the_day(self):
        t = self.cramped_afternoon()
        full = fullness(t.day_plan, at("12:10"))
        busy, total = full["afternoon"]
        self.assertEqual(total, 10)
        self.assertGreaterEqual(busy, 7)
        self.assertEqual(part_of_day(at("19:00")), "evening")
        self.assertEqual(part_of_day(at("02:00", 1)), "night")


class TestNow(ScheduleCase):

    def test_the_time_now_is_said_as_it_is(self):
        keep = real_times(now=at("08:41"))
        self.assertEqual(to_thirties("It's 8:41 now, so breakfast fits.", keep),
                         "It's 8:41 now, so breakfast fits.")  # 10-26: "said it was 8:30 right now ... it was 8:41"
        self.assertEqual(to_thirties("Since it's 8:38, breakfast at 9:05.", keep),
                         "Since it's 8:38, breakfast in the 9:00 block.")  # a moment ago is still now; 9:05 isn't

    def test_replies_keep_the_real_time(self):
        from tests.test_conversation import reply
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        set_debug_time(at("08:41"))
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        engine = MockInferenceEngine()
        manager = ConversationManager(plan, [], pending, sched, inference_engine=engine, active_notes=[])
        engine.responses = [reply("It's 8:41, so you have time before work.")]
        self.assertEqual(manager.send_user_message("what time is it?"), "It's 8:41, so you have time before work.")
        self.assertIn("Current time: 8:41 AM.", manager.messages[0]["content"])


class TestSlmPushBack(SlmTestCase):

    def test_push_everything_back_by_an_amount(self):
        self.say("walk at 4:30 for an hour", {"type": "add", "title": "Walk", "start": "4:30", "duration": "1 hour"})
        out = self.say("push everything back an hour")
        self.assertTrue(out.startswith("Pushed back 1h:"), out)
        walk = next(s for s in self.m.day_plan.spans if s.title == "Walk")
        self.assertEqual(walk.start_dt, at("18:30"))  # the one happening now moves too, past the fixed dentist


if __name__ == "__main__":
    unittest.main()
