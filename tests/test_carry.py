"""The carry-over rule's pieces (carry.py) and how long list lines are said in the morning."""

import unittest
from datetime import date

from tests.fixtures import make_scheduler
from thirties_core.assistant_tools import _short
from thirties_core.carry import ASK, CARRY, DROP, CarryLedger

DAY = date(2026, 11, 6)


class TestLedger(unittest.TestCase):

    def test_carried_once_asked_once_then_dropped(self):
        ledger = CarryLedger(make_scheduler().state_db)
        self.assertEqual(ledger.sort(["Thank yous"], DAY)[CARRY], ["Thank yous"])
        self.assertEqual(ledger.sort(["Thank yous"], DAY)[CARRY], ["Thank yous"])  # the same morning, same answer
        self.assertEqual(ledger.sort(["Thank yous"], date(2026, 11, 7))[ASK], ["Thank yous"])
        self.assertEqual(ledger.sort(["Thank yous"], date(2026, 11, 7))[ASK], ["Thank yous"])
        self.assertEqual(ledger.sort(["Thank yous"], date(2026, 11, 8))[DROP], ["Thank yous"])

    def test_kept_starts_again_and_what_closed_is_forgotten(self):
        ledger = CarryLedger(make_scheduler().state_db)
        ledger.sort(["A", "B"], DAY, lambda t: date(2026, 10, 26))  # on the list since 10-26: asked right away
        self.assertEqual(ledger.sort(["A", "B"], DAY)[ASK], ["A", "B"])
        ledger.kept(["A"], DAY)
        self.assertEqual(ledger.sort(["A"], date(2026, 11, 7))[CARRY], ["A"])  # B was done: gone from the ledger
        self.assertIsNone(ledger.since("B"))


class TestShort(unittest.TestCase):

    def test_a_paragraph_becomes_its_headline(self):
        line = ("**NAS: change the `pm-agent` password** (it leaked into an agent log on 11-04; then give the PM the "
                "new one) · the two fixes below are done")
        self.assertEqual(_short(line), "NAS: change the pm-agent password")
        self.assertEqual(_short("Buy stamps"), "Buy stamps")
        self.assertTrue(len(_short("word " * 40)) <= 81)
