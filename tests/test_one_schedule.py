"""One schedule per reply. When Python's rest of today goes under a reply (or show_schedule's layout is in it), a
schedule the model listed itself in the same reply is left out: Python's rows are the ones the user reads, and there
is never a second "If there's time" section. And the block under way is labelled as now in the schedule views,
never by a start time that reads as already past. Scripted model replies; invented items. DAY is Thu Oct 15 (Work
8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest
from unittest.mock import patch

from tests.fixtures import DAY, at, make_scheduler, morning_sent
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

EVENING = (("ferns", "Water the ferns", "17:00", "17:15"),
           ("cactus", "Repot the cactus", "18:00", "19:00"),
           ("uke", "Tune the ukulele", "19:00", "20:00"),
           ("bread", "Bake soda bread", "20:00", "21:30"),
           ("map", "Frame the old map", "21:30", "22:00"))
LATER = ("Repot the cactus", "Tune the ukulele", "Bake soda bread", "Frame the old map")
UNTIMED = ["Oil the bike chain", "Sort the button tin"]


class OneScheduleCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("17:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        morning_sent(self.sched.state_db)
        db = self.sched.state_db
        for sid, title, start, end in EVENING:
            db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        for sid, title, start, end in (("kite", "Glue the kite spar", "15:00", "15:30"),
                                       ("skis", "Wax the skis", "15:30", "16:00")):
            db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, status=SpanStatus.DONE))
        plan, pending = self.sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        self.manager = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine,
                                           active_notes=[])

    def turn(self, *replies: str, said: str = "ok") -> str:
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.manager.send_user_message(said)

    def add(self, text: str, said: str = "sand the stool at 5:30") -> str:
        return self.turn('CALL add_event {"title": "Sand the stool", "start": "5:30 PM", "duration": "30 minutes", '
                         '"because": "sand the stool at 5:30"}', text, said=said)

    def assertOnce(self, out: str, *titles: str) -> None:
        for title in titles:
            self.assertEqual(out.count(title), 1, f"{title!r} in:\n{out}")


class TestTheModelsListingGoes(OneScheduleCase):

    def test_a_listing_of_the_rest_of_today_is_left_out_and_pythons_stays(self) -> None:
        out = self.add("Added the stool sanding at 5:30. Here's how tonight runs:\n"
                       "5:30 PM – Sand the stool\n6:00 PM – Repot the cactus\n7:00 PM – Tune the ukulele\n"
                       "8:00 PM – Bake soda bread (until 9:30 PM)\n9:30 PM – Frame the old map\n\nEnjoy the evening!")
        self.assertTrue(out.startswith("Added the stool sanding at 5:30.\n"), out)
        self.assertNotIn("Here's how tonight runs", out)
        self.assertOnce(out, "Sand the stool", *LATER, "The rest of today:")
        self.assertLess(out.index("Enjoy the evening!"), out.index("The rest of today:"))  # Python's block, last
        self.assertIn("6:00 PM – Repot the cactus", out.split("The rest of today:", 1)[1])

    def test_rows_without_dashes_a_heading_a_now_line_and_a_free_line(self) -> None:
        out = self.add("Done.\n\nNext up:\nNow: Water the ferns\n5:30 PM Sand the stool\n6:00 PM Repot the cactus\n"
                       "7:00 PM Tune the ukulele\nFree: 10:00 PM – 11:00 PM")
        self.assertNotIn("Next up", out)
        self.assertNotIn("Free: 10:00 PM", out)
        self.assertOnce(out, "Now: Water the ferns", "Sand the stool", "Repot the cactus", "Tune the ukulele")
        self.assertTrue(out.startswith("Done.\n\nNow: Water the ferns"), out)

    def test_an_intro_that_says_something_keeps_its_words(self) -> None:
        out = self.add("Sanding's in. I've lined the evening up around the bread:\n6:00 PM – Repot the cactus\n"
                       "7:00 PM – Tune the ukulele\n8:00 PM – Bake soda bread")
        self.assertTrue(out.startswith("Sanding's in. I've lined the evening up around the bread.\n\n"), out)
        self.assertOnce(out, *LATER)

    def test_a_listing_that_names_items_without_times_between_its_rows(self) -> None:
        out = self.add("Here's how it lines up:\nSand the stool (now-ish)\n6:00 PM – Repot the cactus\n"
                       "7:00 PM – Tune the ukulele\n11:00 PM – Sleep\n\nNothing else added.")
        self.assertTrue(out.startswith("Nothing else added.\n\n"), out)
        self.assertOnce(out, "Sand the stool", "Repot the cactus", "Tune the ukulele")
        self.assertNotIn("Sleep", out.split("The rest of today:", 1)[0])

    def test_rows_with_a_list_name_in_front_or_a_real_time_after(self) -> None:
        out = self.add("Sorted.\n\n6:00 PM – Windowsill: Repot the cactus\n7:00 PM – Tune the ukulele at 7:10 PM\n"
                       "8:00 PM – Bake soda bread (until 9:30 PM)")
        self.assertTrue(out.startswith("Sorted.\n\n"), out)
        self.assertNotIn("Windowsill", out)
        self.assertOnce(out, *LATER)

    def test_a_question_that_starts_with_a_time_stays(self) -> None:
        text = "6:00 PM for Repot the cactus, or later?\n7:00 PM Tune the ukulele, still good?"
        out = self.add(text)
        self.assertTrue(out.startswith(text + "\n\n"), out)

    def test_the_whole_reply_a_listing_leaves_pythons_block_alone(self) -> None:
        out = self.add("5:30 PM – Sand the stool\n6:00 PM – Repot the cactus\n7:00 PM – Tune the ukulele")
        self.assertOnce(out, "Sand the stool", "Repot the cactus", "Tune the ukulele", "The rest of today:")


class TestWhatStays(OneScheduleCase):

    def test_prose_about_items_and_times_stays(self) -> None:
        text = ("Sanding's at 5:30, so the cactus still has 6:00 PM to itself.\n"
                "7:00 PM is the ukulele, then the bread.")
        out = self.add(text)
        self.assertTrue(out.startswith(text + "\n\n"), out)

    def test_one_row_that_answers_the_change_stays(self) -> None:
        out = self.add("Added:\n5:30 PM – Sand the stool")
        self.assertTrue(out.startswith("Added:\n5:30 PM – Sand the stool\n\n"), out)

    def test_a_record_of_what_got_done_stays(self) -> None:
        out = self.add("Sanding's in. Earlier today:\n3:00 PM – Glue the kite spar\n3:30 PM – Wax the skis",
                       said="sand the stool at 5:30, and what have I done so far?")
        self.assertIn("Earlier today:\n3:00 PM – Glue the kite spar\n3:30 PM – Wax the skis", out)

    def test_a_listing_of_another_day_stays(self) -> None:
        text = "Tomorrow could look like this:\n9:00 AM – Clean the aquarium\n10:00 AM – Paint the fence"
        out = self.add("Sanding's in. " + text)
        self.assertIn(text, out)

    def test_no_block_under_the_reply_no_change(self) -> None:
        text = "Here's tonight:\n6:00 PM – Repot the cactus\n7:00 PM – Tune the ukulele"
        out = self.turn(text, said="hmm")  # nothing changed: nothing goes under it, so its listing is the only one
        self.assertEqual(out, text)


class TestIfTheresTimeOnce(OneScheduleCase):

    def test_the_models_if_theres_time_goes_when_pythons_is_under_the_reply(self) -> None:
        from thirties_core.whats_next import IF_TIME_HEADING
        with patch("thirties_core.whats_next.untimed_for", return_value=[(IF_TIME_HEADING, UNTIMED)]):
            out = self.add("Sanding's in.\n\nIf there's time (after the bread):\nOil the bike chain\n"
                           "Sort the button tin\n\nHave fun.")
        self.assertEqual(out.lower().count("if there's time"), 1, out)
        self.assertIn("If there's time:\nOil the bike chain\nSort the button tin", out)
        self.assertOnce(out, *UNTIMED)
        self.assertIn("Have fun.", out)

    def test_two_from_the_model_alone_become_one(self) -> None:
        from thirties_core.one_schedule import tidy
        text = ("Sure.\n\nIf there's time:\nOil the bike chain\n\nIf there's time (tonight):\nOil the bike chain\n"
                "Sort the button tin\n\nThat's all.")
        out = tidy(text, "", self.manager.day_plan)
        self.assertEqual(out, "Sure.\n\nIf there's time:\nOil the bike chain\n\nThat's all.")


class TestWithShowSchedule(OneScheduleCase):

    def test_the_models_own_rows_beside_the_schedule_it_was_given(self) -> None:
        out = self.turn("CALL show_schedule {}", "Here's the evening:\n6:00 PM Repot the cactus\n"
                        "7:00 PM Tune the ukulele\n8:00 PM Bake soda bread\n\nShout if you want changes.",
                        said="hmm, show me")
        self.assertOnce(out, *LATER)
        self.assertIn("Shout if you want changes.", out)
        self.assertIn("6:00 PM – Repot the cactus", out)  # Python's layout is what stays

    def test_never_two_if_theres_time_sections_with_the_schedule(self) -> None:
        with patch("thirties_core.if_time.text_for", return_value="\n\nIf there's time:\n" + "\n".join(UNTIMED)):
            out = self.turn("CALL show_schedule {}", "Here you go.\n\n6:00 PM – Repot the cactus\n\n"
                            "If there's time (after dinner):\nOil the bike chain\nSort the button tin",
                            said="show me tonight")
        self.assertEqual(out.lower().count("if there's time"), 1, out)
        self.assertOnce(out, *UNTIMED)

    def test_a_rest_of_today_the_model_wrote_beside_the_whole_day(self) -> None:
        out = self.turn('CALL show_schedule {"whole_day": true}', "All set.\n\nThe rest of today:\n"
                        "6:00 PM – Repot the cactus\n7:00 PM – Tune the ukulele\n8:00 PM – Bake soda bread",
                        said="the whole day please")
        self.assertIn("Earlier today:", out)  # what today held before now, above the standard view
        self.assertEqual(out.count("The rest of today:"), 1, out)
        self.assertOnce(out, *LATER)


class TestTheBlockUnderWay(unittest.TestCase):
    """At 8:35, the 8:30 block is under way: its row says Now, never "8:30 AM", and what's in it isn't behind."""

    def setUp(self) -> None:
        set_debug_time(at("08:35"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        morning_sent(self.sched.state_db)
        for sid, title, start, end in (("seeds", "Soak the bean seeds", "07:00", "07:30"),
                                       ("canvas", "Prime the canvas", "08:30", "09:00"),
                                       ("letters", "Answer two letters", "09:00", "09:30")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        self.plan, _ = self.sched.build_day_plan(DAY)

    def test_the_whole_day_labels_the_block_under_way_now(self) -> None:
        from thirties_core.messages import whole_day_lines
        lines = whole_day_lines(self.plan, now=at("08:35"))
        self.assertIn("7:00 AM – 7:30 AM Soak the bean seeds", lines)  # earlier rows keep their blocks
        self.assertIn("9:00 AM – 9:30 AM Answer two letters", lines)  # and later ones
        self.assertIn("Now: Work (until 4:00 PM) + Prime the canvas", lines)
        self.assertFalse(any(line.startswith("8:30 AM") for line in lines), lines)
        record = whole_day_lines(self.plan)  # a record: as it was, work on a row of its own
        self.assertIn("8:30 AM – 4:00 PM Work", record)
        self.assertIn("8:30 AM – 9:00 AM Prime the canvas", record)

    def test_the_schedule_and_whats_next_never_show_it_as_past(self) -> None:
        from thirties_core.messages import render_schedule
        from thirties_core.whats_next import render
        for text in (render_schedule(self.plan, at("08:35"), whole_day=True), render_schedule(self.plan, at("08:35")),
                     render(self.plan, at("08:35"), mode="rest")):
            rows = [line for line in text.splitlines() if "Prime the canvas" in line]
            self.assertTrue(rows, text)
            self.assertFalse(any(line.startswith("8:30 AM") for line in rows), text)
            behind = text.split("Not marked yet:", 1)[1].split("\n\n", 1)[0] if "Not marked yet:" in text else ""
            self.assertIn("Soak the bean seeds", behind)  # what's really behind is
            self.assertNotIn("Prime the canvas", behind)  # what's under way isn't

    def test_an_exact_time_inside_the_block_is_kept(self) -> None:
        from thirties_core.messages import whole_day_lines
        self.sched.state_db.upsert_span(EventSpan("call", "Call the framer", at("08:45"), at("09:00"),
                                                  SpanCategory.CALENDAR, source="calendar"))
        plan, _ = self.sched.build_day_plan(DAY)
        now_row = [line for line in whole_day_lines(plan, now=at("08:35")) if line.startswith("Now:")]
        self.assertEqual(len(now_row), 1)
        self.assertIn("Call the framer at 8:45 AM", now_row[0])  # a real time inside the block is still said


if __name__ == "__main__":
    unittest.main()
