"""Unit tests for thirties_core.clock."""

import unittest

from tests.fixtures import at
from thirties_core.clock import (
    ClockError,
    agenda_lines,
    format_clock,
    format_duration,
    parse_clock,
    parse_duration_minutes,
    parse_shift_minutes,
    resolve_clock,
    trim_meridiems,
)


class TestClock(unittest.TestCase):

    def test_parse_clock_forms(self) -> None:
        for text, expected in [("4:30 PM", "16:30"), ("4pm", "16:00"), ("4:30p.m.", "16:30"), ("16:30", "16:30"),
                               ("07:00", "07:00"), ("12:00 AM", "00:00"), ("12 pm", "12:00"),
                               ("noon", "12:00"), ("midnight", "00:00")]:
            self.assertEqual(parse_clock(text).strftime("%H:%M"), expected, text)

    def test_ambiguous_or_invalid_times_rejected(self) -> None:
        for text in ("4:30", "7", "25:00", "13 pm", "half past"):
            with self.assertRaises(ClockError, msg=text):
                parse_clock(text)

    def test_resolve_on_diurnal_day(self) -> None:
        day_start = at("07:00")
        self.assertEqual(resolve_clock("4:30 PM", day_start), at("16:30"))
        # Early-morning times belong to the end of the diurnal day (the next calendar date)
        self.assertEqual(resolve_clock("1:00 AM", day_start), at("01:00", 1))
        # End times resolve to the first occurrence after the start
        self.assertEqual(resolve_clock("6:30 AM", day_start, after=at("22:30")), at("06:30", 1))
        self.assertEqual(resolve_clock("5:00 PM", day_start, after=at("17:00")), at("17:00", 1))

    def test_durations(self) -> None:
        for text, minutes in [("2 hours", 120), ("90 min", 90), ("1h30m", 90), ("1.5 hours", 90),
                              ("1 hour and 30 minutes", 90), ("45", 45), (30, 30)]:
            self.assertEqual(parse_duration_minutes(text), minutes, text)
        for bad in ("soon", "0", "-5"):
            with self.assertRaises(ClockError, msg=bad):
                parse_duration_minutes(bad)

    def test_shifts(self) -> None:
        for text, minutes in [("+1 hour", 60), ("-30 min", -30), ("+90", 90), ("1 hour later", 60),
                              ("15 minutes earlier", -15), (-45, -45)]:
            self.assertEqual(parse_shift_minutes(text), minutes, text)
        for bad in ("1 hour", "+1 hour earlier", 0):
            with self.assertRaises(ClockError, msg=str(bad)):
                parse_shift_minutes(bad)

    def test_formatting(self) -> None:
        self.assertEqual(format_clock(at("07:05")), "7:05 AM")
        self.assertEqual(format_duration(150), "2h 30m")
        self.assertEqual(format_duration(45), "45m")
        self.assertEqual(format_duration(120), "2h")

    def test_am_pm_only_where_it_could_be_mistaken(self) -> None:
        afternoon = at("16:40")
        self.assertEqual(trim_meridiems("Dinner 6:00 PM – 7:00 PM", afternoon), "Dinner 6:00 – 7:00")
        self.assertEqual(trim_meridiems("Dinner 6:00 PM – 7:00 PM", at("09:00")), "Dinner 6:00 PM – 7:00")
        self.assertEqual(trim_meridiems("- 11:30 PM – Read\n- 1:00 AM – Sleep", afternoon),
                         "- 11:30 – Read\n- 1:00 AM – Sleep")  # crossing midnight
        self.assertEqual(trim_meridiems("Lunch at 12:30 PM", afternoon), "Lunch at 12:30 PM")  # 12 is never obvious
        self.assertEqual(trim_meridiems("Moved to tomorrow, 4:00 PM", afternoon), "Moved to tomorrow, 4:00 PM")

    def test_short_items_are_listed_by_their_thirty(self) -> None:
        items = [(at("17:00"), at("17:15"), "Message Brad", ""), (at("17:15"), at("17:30"), "Feed cats", ""),
                 (at("17:30"), at("18:30"), "Dinner", "")]
        self.assertEqual(agenda_lines(items), ["- 5:00 PM –", "  Message Brad", "  Feed cats", "- 5:30 PM – Dinner"])
        together = [(at("10:35"), at("10:50"), "Personal call", ""), (at("10:45"), at("11:00"), "Break", "")]
        self.assertEqual(agenda_lines(together), ["- 10:30 AM – Personal call + Break"])

    def test_agenda_lines_show_end_times_only_where_not_implied(self) -> None:
        items = [(at("16:00"), at("17:00"), "Feed cat", ""),
                 (at("17:00"), at("17:30"), "Wash car", ""),
                 (at("18:00"), at("19:00"), "Dinner", ""),
                 (at("18:00"), at("19:00"), "Audiobook", ""),
                 (at("19:00"), at("21:00"), "Team dinner", "unconfirmed")]
        self.assertEqual(agenda_lines(items), [
            "- 4:00 PM – Feed cat",
            "- 5:00 PM – Wash car (until 5:30 PM)",
            "- 6:00 PM – Dinner + Audiobook",          # sounding together
            "- 7:00 PM – Team dinner (unconfirmed)",  # the last item's end isn't worth the noise
        ])
        # the piano roll: note1 ends and note2 begins in the 10:30 thirty, note3 spans it: note3 joins the first line
        notes = [(at("10:00"), at("10:40"), "note1", ""), (at("10:40"), at("11:30"), "note2", ""),
                 (at("10:30"), at("11:00"), "note3", "")]
        self.assertEqual(agenda_lines(notes), ["- 10:00 AM – note1", "- 10:30 AM –", "  note1 + note3", "  note2",
                                               "- 11:00 AM – note2"])
        nested = [(at("08:30"), at("16:00"), "Work", ""), (at("10:00"), at("11:00"), "Meeting", "")]
        self.assertEqual(agenda_lines(nested, numbered=True),
                         ["1. 8:30 AM – Work (until 4:00 PM)", "2. 10:00 AM – Meeting"])



class TestToThirties(unittest.TestCase):

    def test_only_00_and_30_are_said(self):
        from thirties_core.clock import to_thirties
        # a time becomes the thirty it falls in: a break at 10:45 is in the 10:30 thirty, never "11:00"
        self.assertEqual(to_thirties("Sunset at 6:28, then dinner 7:00 PM – 8:00 PM"),
                         "Sunset in the 6:00 block, then dinner 7:00 PM – 8:00 PM")  # never a false "at 6:00"
        self.assertEqual(to_thirties("break at 10:45, back by 11:00"), "break in the 10:30 block, back by 11:00")
        self.assertEqual(to_thirties("since 1:14 and until 2:29 PM"), "since 1:00 and until 2:30 PM")
        self.assertEqual(to_thirties("at 11:50 PM"), "in the 11:30 PM block")
        self.assertEqual(to_thirties("Uber 12:15 PM – 12:45 PM"), "Uber 12:00 PM – 1:00 PM")  # the blocks it covers
        self.assertEqual(to_thirties("work 8:30 AM – 11:45 AM"), "work 8:30 AM – 12:00 PM")
        real = frozenset({(12, 15)})  # the Uber's real time: said as it is
        self.assertEqual(to_thirties("Your Uber is at 12:15, in the 12:00 block.", real),
                         "Your Uber is at 12:15, in the 12:00 block.")
        self.assertEqual(to_thirties("11:00 sharp, then the Uber at 12:15", real), "11:00 sharp, then the Uber at 12:15")
        from thirties_core.clock import real_times
        from types import SimpleNamespace as NS
        from tests.fixtures import at
        flight = NS(title="Flight UA 1482 (boards 2:25)", start_dt=at("15:00"), end_dt=at("16:00"))
        keep = real_times("boarding at 14:25", spans=[flight])  # a 24-hour time, and a time in a title
        self.assertEqual(to_thirties("⏰ Flight UA 1482 (boards 2:25) at 3:00. Boarding at 2:25.", keep),
                         "⏰ Flight UA 1482 (boards 2:25) at 3:00. Boarding at 2:25.")
        self.assertEqual(to_thirties("score 3:2, ratio 10:45:12"), "score 3:2, ratio 10:45:12")
