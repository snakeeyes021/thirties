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

    def test_agenda_lines_show_end_times_only_where_not_implied(self) -> None:
        items = [(at("16:00"), at("17:00"), "Feed cat", ""),
                 (at("17:00"), at("17:30"), "Wash car", ""),
                 (at("18:00"), at("19:00"), "Dinner", ""),
                 (at("18:00"), at("19:00"), "Audiobook", ""),
                 (at("19:00"), at("21:00"), "Team dinner", "unconfirmed")]
        self.assertEqual(agenda_lines(items), [
            "- 4:00 PM – Feed cat",
            "- 5:00 PM – Wash car (until 5:30 PM)",
            "- 6:00 PM – Dinner",
            "- 6:00 PM – Audiobook",
            "- 7:00 PM – Team dinner (unconfirmed, until 9:00 PM)",
        ])
        nested = [(at("08:30"), at("16:00"), "Work", ""), (at("10:00"), at("11:00"), "Meeting", "")]
        self.assertEqual(agenda_lines(nested, numbered=True),
                         ["1. 8:30 AM – Work (until 4:00 PM)", "2. 10:00 AM – Meeting (until 11:00 AM)"])


if __name__ == "__main__":
    unittest.main()
