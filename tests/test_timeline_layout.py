"""The Timeline page's layout, without a display (thirties_core/timeline_layout.py): the waking day as slots, the
sky and work rails, cards in lanes with pips, free stretches, the now marker, the week, and free-time units."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.horizons import HorizonPlanner, parse_period
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem
from thirties_core.plan_tools import PlanTools
from thirties_core.settings import SETTINGS
from thirties_core.timeline_layout import (amount_text, day_layout, delete_call, free_text, move_call, normalise_units,
                                           resize_call, resize_extent, restore_call, snap_boundary, snap_row,
                                           stretch_options,
                                           unplaced_in_week, week_days, week_layout, what_to_plan)


def span(sched, sid, title, start, end, category=SpanCategory.ACTIVITY, day_offset=0, **kw):
    s = EventSpan(sid, title, at(start, day_offset), at(end, day_offset), category, **kw)
    sched.state_db.upsert_span(s)
    return s


class DayCase(unittest.TestCase):
    """DAY (Thu Oct 15): sunrise ~7:17 AM, sunset ~6:30 PM; default Work 8:30 AM - 4 PM, Sleep 11 PM - 7 AM."""

    def setUp(self):
        self.sched = make_scheduler([event("lunch", "12:00", "12:30", "Lunch with Sam")])
        self.spans = {s.id: s for s in (span(self.sched, "dinner", "Dinner", "18:00", "19:00"),
                                        span(self.sched, "audio", "Audiobook", "18:00", "19:00"),
                                        span(self.sched, "movie", "Movie", "20:00", "21:30"))}

    def mark(self, sid, status, **kw):
        s = self.spans[sid]
        s.status = status
        for k, v in kw.items():
            setattr(s, k, v)
        self.sched.state_db.upsert_span(s)

    def plan(self):
        return self.sched.build_day_plan(DAY)[0]


class TestTheDay(DayCase):

    def test_the_waking_day_is_a_slot_per_thirty_from_waking_to_bedtime(self):
        d = day_layout(self.plan(), at("19:10"))
        self.assertEqual(len(d.slots), 32)  # 7 AM to 11 PM
        self.assertEqual((d.start, d.end), (at("07:00"), at("23:00")))
        self.assertTrue(all(not s.sleep for s in d.slots))

    def test_the_sky_is_to_the_minute(self):
        plan = self.plan()
        d = day_layout(plan, at("19:10"))
        self.assertEqual([b.daylight for b in d.sky], [False, True, False])
        sunrise_min = (plan.sunrise - at("07:00")).total_seconds() / 60
        self.assertAlmostEqual(d.sky[0].bottom, sunrise_min / 30)  # not rounded to a thirty
        self.assertAlmostEqual(d.sky[1].bottom, (plan.sunset - at("07:00")).total_seconds() / 1800)
        self.assertEqual(d.sky[-1].bottom, 32)

    def test_work_is_one_card_under_the_items(self):
        d = day_layout(self.plan(), at("19:10"))
        self.assertEqual([(c.span.title, c.row, c.rows, c.look, c.tickable) for c in d.work],
                         [("Work", 3, 15, "work", False)])  # 8:30 AM - 4 PM, a monolith
        self.assertNotIn("Work", [c.span.title for c in d.cards])  # not in the lanes: items sit on top of it
        self.assertTrue(d.slots[3].work and not d.slots[2].work)

    def test_cards_sit_on_their_slots_with_a_pip_per_thirty_and_share_time_in_lanes(self):
        d = day_layout(self.plan(), at("19:10"))
        cards = {c.span.title: c for c in d.cards}
        self.assertEqual((cards["Movie"].row, cards["Movie"].rows, cards["Movie"].pips), (26, 3, 3))
        self.assertEqual(cards["Lunch with Sam"].look, "calendar")
        self.assertEqual(cards["Dinner"].look, "daily")
        self.assertEqual({cards["Dinner"].lane, cards["Audiobook"].lane}, {0, 1})
        self.assertEqual((cards["Dinner"].lanes, cards["Audiobook"].lanes, cards["Movie"].lanes), (2, 2, 1))

    def test_a_short_item_still_takes_its_slot_and_counts_one_pip(self):
        span(self.sched, "pills", "Pills", "20:40", "20:45")
        d = day_layout(self.plan(), at("19:10"))
        pills = next(c for c in d.cards if c.span.title == "Pills")
        self.assertEqual((pills.row, pills.rows, pills.pips, pills.lanes), (27, 1, 1, 2))  # beside the movie

    def test_free_time_is_nothing_planned_not_work_not_sleep_from_now_on(self):
        d = day_layout(self.plan(), at("19:10"))
        self.assertEqual([(f.start, f.end, f.rows) for f in d.free],
                         [(at("19:00"), at("20:00"), 2), (at("21:30"), at("23:00"), 3)])  # the current thirty counts
        self.assertEqual(d.free_blocks, 5)
        morning = day_layout(self.plan(), at("06:00"))  # before the day: all of it
        self.assertEqual([(f.start, f.end) for f in morning.free][:2],
                         [(at("07:00"), at("08:30")), (at("16:00"), at("18:00"))])  # never inside Work
        self.assertFalse(any(f.start < at("16:00") and f.end > at("08:30") for f in morning.free))
        self.assertEqual(day_layout(self.plan(), at("12:00", 2)).free, [])  # a past day has none

    def test_a_skipped_item_frees_its_time_but_is_still_shown(self):
        self.mark("movie", SpanStatus.SKIPPED)
        d = day_layout(self.plan(), at("19:10"))
        self.assertEqual([(f.start, f.end) for f in d.free], [(at("19:00"), at("23:00"))])
        card = next(c for c in d.cards if c.span.title == "Movie")
        self.assertEqual((card.state, card.status, card.tickable), ("skipped", "Skipped", False))

    def test_now_is_on_the_rail_with_the_sun_or_moon(self):
        d = day_layout(self.plan(), at("19:15"))
        self.assertAlmostEqual(d.now_row, 24.5)
        self.assertFalse(d.now_daylight)  # after sunset: the moon
        self.assertTrue(day_layout(self.plan(), at("12:00")).now_daylight)
        self.assertIsNone(day_layout(self.plan(), at("12:00", 1)).now_row)  # not this day

    def test_ticking_now_means_it_finished_at_the_tick(self):
        d = day_layout(self.plan(), at("18:20"))
        dinner = next(c for c in d.cards if c.span.title == "Dinner")
        self.assertTrue(dinner.current and dinner.tickable and dinner.finishes_now)
        lunch = next(c for c in d.cards if c.span.title == "Lunch with Sam")
        self.assertTrue(lunch.tickable)  # the user's own calendar event can be ticked (soak 1)
        self.assertFalse(lunch.finishes_now)

    def test_moot_worked_on_and_someone_elses_events_read_as_on_the_day_page(self):
        research = span(self.sched, "dev", "Torquio dev", "16:00", "17:00", status=SpanStatus.DONE)
        self.assertEqual(research.title, "Torquio dev")
        self.mark("dinner", SpanStatus.UNKNOWN)
        plan = self.plan()
        plan.spans.append(EventSpan("cal-r", "Rehearsal", at("19:00"), at("21:00"), SpanCategory.CALENDAR,
                                    source="calendar", shown_only=True))
        plan.spans.append(EventSpan("cal-i", "Team drinks", at("20:00"), at("21:00"), SpanCategory.CALENDAR,
                                    source="calendar", tentative=True))
        d = day_layout(plan, at("19:10"))
        cards = {c.span.title: c for c in d.cards}
        self.assertEqual((cards["Torquio dev"].look, cards["Torquio dev"].status), ("ongoing", "Worked on"))
        self.assertEqual((cards["Dinner"].state, cards["Dinner"].status), ("moot", "Not reported"))
        self.assertNotIn("Rehearsal", cards)  # never a card of its own
        self.assertEqual([(n.row, n.text) for n in d.notes], [(24, "Rehearsal (not yours)")])
        self.assertEqual(cards["Movie"].notes, ["Team drinks (invite, not answered), 8:00 PM – 9:00 PM"])
        self.assertEqual(d.free[0].start, at("19:00"))  # someone else's event doesn't make the user busy
        custom = day_layout(plan, at("19:10"), ongoing=lambda s: s.title == "Dinner")
        self.assertEqual({c.span.title: c.look for c in custom.cards}["Torquio dev"], "once")

    def test_something_planned_past_bedtime_widens_the_day(self):
        self.sched.state_db.upsert_span(EventSpan("late", "Late show", at("23:30"), at("00:30", 1),
                                                  SpanCategory.ACTIVITY))
        d = day_layout(self.plan(), at("19:10"))
        self.assertEqual(d.end, at("00:30", 1))
        self.assertTrue(d.slots[-1].sleep)
        self.assertFalse(d.slots[-1].free)

    def test_the_tooltip_has_the_real_times_and_what_really_happened(self):
        self.mark("movie", SpanStatus.DONE, actual_start=at("20:10"), actual_end=at("21:40"))
        card = next(c for c in day_layout(self.plan(), at("22:00")).cards if c.span.title == "Movie")
        self.assertEqual(card.times, "8:00 PM – 9:30 PM (really 8:10 PM – 9:40 PM)")
        self.assertEqual((card.state, card.tickable), ("done", False))

    def test_no_sleep_shows_the_whole_canvas(self):
        sched = make_scheduler()
        sched.config.general.work_days = []
        plan = sched.build_day_plan(DAY)[0]
        plan.spans = [s for s in plan.spans if s.category != SpanCategory.SLEEP]
        self.assertEqual(len(day_layout(plan, at("12:00")).slots), 48)


class TestChangingThePlanByHand(DayCase):
    """Drag to move, resize from an edge, remove: each gesture is one tool call (the assistant's own tools)."""

    def setUp(self):
        super().setUp()
        self.d = day_layout(self.plan(), at("19:10"))
        self.cards = {c.span.title: c for c in self.d.cards}

    def test_thirties_snap(self):
        self.assertEqual(snap_row(26.9, 32), 26)
        self.assertEqual(snap_row(-3, 32), 0)
        self.assertEqual(snap_row(40, 32), 31)
        self.assertEqual(snap_boundary(26.4, 32), 26)
        self.assertEqual(snap_boundary(26.6, 32), 27)
        self.assertEqual(snap_boundary(99, 32), 32)

    def test_a_drag_moves_it_to_the_thirty_it_lands_on_keeping_its_length(self):
        movie = self.cards["Movie"]  # rows 26-28, 8 PM
        self.assertEqual(move_call(self.d, movie, 0, 28.3), ("update_event", {"event": "movie", "start": "9:00 PM"}))
        # grabbed by its second thirty: the card keeps its place under the pointer
        self.assertEqual(move_call(self.d, movie, 1, 28.3), ("update_event", {"event": "movie", "start": "8:30 PM"}))
        self.assertIsNone(move_call(self.d, movie, 0, 26.7))  # same thirty: nothing to do
        self.assertEqual(move_call(self.d, movie, 0, 22.5, push=True),  # onto dinner, Shift held: push what follows
                         ("update_event", {"event": "movie", "start": "6:00 PM", "push_later": True}))
        self.assertEqual(move_call(self.d, movie, 0, 22.5)[1], {"event": "movie", "start": "6:00 PM"})  # shares it

    def test_a_resize_changes_one_edge_on_thirties(self):
        movie = self.cards["Movie"]
        self.assertEqual(resize_extent(self.d, movie, "bottom", 30.2), (26, 4))
        self.assertEqual(resize_call(self.d, movie, "bottom", 30.2), ("update_event", {"event": "movie",
                                                                                         "end": "10:00 PM"}))
        self.assertEqual(resize_call(self.d, movie, "top", 25.1),
                         ("update_event", {"event": "movie", "start": "7:30 PM", "end": "9:30 PM"}))
        self.assertEqual(resize_extent(self.d, movie, "top", 31), (28, 1))  # never less than one thirty
        self.assertEqual(resize_extent(self.d, movie, "bottom", 2), (26, 1))
        self.assertIsNone(resize_call(self.d, movie, "bottom", 29.1))  # unchanged

    def test_work_calendar_events_and_free_time_stay_put(self):
        lunch = self.cards["Lunch with Sam"]
        self.assertIsNone(move_call(self.d, lunch, 0, 14))  # a calendar event is fixed
        self.assertIsNone(resize_call(self.d, lunch, "bottom", 14))
        self.assertIsNone(delete_call(lunch.span))
        work = self.d.work[0]
        self.assertIsNone(move_call(self.d, work, 0, 5))
        self.assertIsNone(delete_call(work.span))

    def test_remove_and_undo(self):
        movie = self.cards["Movie"].span
        self.assertEqual(delete_call(movie), ("remove_event", {"event": "movie"}))
        self.assertEqual(restore_call(movie), ("add_event", {"title": "Movie", "start": "8:00 PM", "end": "9:30 PM",
                                                             "alongside": True}))
        tools = PlanTools(self.plan(), [], self.sched)
        tools.execute(*delete_call(movie))
        out = tools.execute(*restore_call(movie))
        self.assertNotIn("NOT_DONE", out)
        back = [s for s in tools.day_plan.spans if s.title == "Movie"]
        self.assertEqual([(s.start_dt, s.end_dt) for s in back], [(at("20:00"), at("21:30"))])


class TestPlanningAFreeStretch(DayCase):

    def test_the_options_are_slot_options_inside_the_stretch(self):
        plan = self.plan()
        d = day_layout(plan, at("06:00"))
        evening = next(f for f in d.free if f.start == at("21:30"))
        tools = PlanTools(plan, [], self.sched)
        slots = stretch_options(tools, plan, evening, 30, at("06:00"))
        self.assertTrue(slots)
        self.assertTrue(all(evening.start <= s.start and s.end <= evening.end for s in slots))
        self.assertIn("right after Movie", [s.where for s in slots])

    def test_what_to_plan_comes_from_the_lists(self):
        tasks = [TaskItem(id="t1", source_notebook="Tasks", title="Call the bank", is_checkbox_item=True,
                          parent_note_id="n1", parent_note_title="Errands", list_role="list", due="2026-10-15")]
        plan = self.plan()
        tools = PlanTools(plan, [], self.sched, services=Services.for_db(self.sched.state_db, tasks))
        self.assertEqual([c["task_id"] for c in what_to_plan(tools, DAY)], ["t1"])
        self.assertEqual(what_to_plan(object(), DAY), [])  # no backlog: nothing to offer, no error


class TestTheWeek(DayCase):

    def test_the_week_starts_on_the_users_day(self):
        self.assertEqual(week_days(DAY)[0], date(2026, 10, 12))
        self.assertEqual(week_days(DAY, 6)[0], date(2026, 10, 11))
        self.assertEqual(len(week_days(DAY)), 7)

    def test_seven_shrunken_days_on_one_axis_with_free_time(self):
        plans = [self.sched.build_day_plan(d)[0] for d in week_days(DAY)]
        w = week_layout(plans, at("19:10"), DAY)
        self.assertEqual([c.day for c in w.columns], week_days(DAY))
        thu = w.columns[3]
        self.assertTrue(thu.is_today and not thu.is_past)
        self.assertTrue(w.columns[0].is_past)
        self.assertEqual(w.columns[0].free_blocks, 0)  # past days have none left
        self.assertEqual(thu.free_blocks, 5)
        self.assertEqual(w.columns[4].free_blocks, day_layout(plans[4], at("19:10")).free_blocks)
        self.assertEqual(w.total_free, sum(c.free_blocks for c in w.columns))
        self.assertIsNotNone(thu.now)
        self.assertIsNone(w.columns[4].now)
        self.assertTrue(all(0 <= b.top < b.bottom <= 1 for c in w.columns for b in c.bars))
        movie = next(b for b in thu.bars if b.span.title == "Movie")
        dinner = next(b for b in thu.bars if b.span.title == "Dinner")
        self.assertLess(dinner.top, thu.now)
        self.assertLess(thu.now, movie.top)
        self.assertEqual(len(thu.work), 1)
        self.assertEqual(w.columns[5].work, [])  # Saturday: no work
        lunch = next(b for b in thu.bars if b.span.title == "Lunch with Sam")
        self.assertTrue(lunch.in_work)  # drawn inside Work's container
        self.assertFalse(dinner.in_work or movie.in_work)  # after work: the full width

    def test_items_pinned_to_the_week_but_no_day_are_chips(self):
        tasks = [TaskItem(id=t, source_notebook="Tasks", title=title, is_checkbox_item=True, parent_note_id="n1",
                          parent_note_title="Home", list_role="list")
                 for t, title in (("door", "Fix the door"), ("paint", "Paint the hall"), ("tax", "File taxes"))]
        tools = PlanTools(self.plan(), [], self.sched, services=Services.for_db(self.sched.state_db, tasks))
        hp = HorizonPlanner(tools)
        hp.pin(tasks[0], parse_period("this week", DAY))
        hp.pin(tasks[1], parse_period("Saturday", DAY))  # has a day: not a chip
        hp.pin(tasks[2], parse_period("next week", DAY))  # another week
        self.assertEqual(unplaced_in_week(hp, DAY), [{"item_id": "door", "title": "Fix the door", "list": "Home"}])


class TestUnits(unittest.TestCase):

    def test_blocks_or_hours_always_with_units(self):
        self.assertEqual(free_text(7), "7 blocks free")
        self.assertEqual(free_text(1), "1 block free")
        self.assertEqual(free_text(0), "0 blocks free")
        self.assertEqual(free_text(7, "hours"), "3½ h free")
        self.assertEqual(free_text(1, "hours"), "½ h free")
        self.assertEqual(free_text(8, "hours"), "4 h free")
        self.assertEqual(amount_text(89), "89 blocks")

    def test_the_setting_is_shared_and_defaults_to_blocks(self):
        self.assertEqual(SETTINGS["timeline.free_units"].scope, "shared")
        self.assertEqual(normalise_units(None), "blocks")
        self.assertEqual(normalise_units("hours"), "hours")
        self.assertEqual(normalise_units("furlongs"), "blocks")


if __name__ == "__main__":
    unittest.main()
