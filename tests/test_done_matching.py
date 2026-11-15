"""A done goes through for an item the user plainly named, even when a word of its title is shared with another
item; an item the words don't point to is still refused.

Seen in a simulated day: an item like "Pup into crate" beside "Order a crate divider", and a morning walk beside two
other walks: saying the pup was settled in the crate, or the stroll and breakfast were over, was refused each time.
"""

import unittest

from tests.fixtures import at
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.item_facts import referenced
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

ITEMS = (("Morning stroll + kibble breakfast", "7:00 AM"), ("Pup into crate", "7:45 AM"), ("Breakfast", "8:30 AM"),
         ("Midday stroll", "12:00 PM"), ("Pup's vet visit", "2:00 PM"), ("Evening stroll", "5:30 PM"),
         ("Order a crate divider", "7:30 PM"))
TITLES = [t for t, _ in ITEMS]


class TestReferenced(unittest.TestCase):
    def others(self, title):
        return [t for t in TITLES if t != title]

    def test_named_by_more_of_its_words_than_any_item_sharing_them(self):
        said = "stroll, breakfast + trick practice all finished!! pup is settled in the crate w a chew toy"
        self.assertTrue(referenced(said, "Morning stroll + kibble breakfast", self.others("Morning stroll + kibble breakfast")))
        self.assertTrue(referenced(said, "Pup into crate", self.others("Pup into crate")))
        self.assertFalse(referenced(said, "Order a crate divider", self.others("Order a crate divider")))
        self.assertFalse(referenced(said, "Midday stroll", self.others("Midday stroll")))

    def test_one_shared_word_alone_is_not_clear(self):
        self.assertFalse(referenced("crate is done", "Pup into crate", self.others("Pup into crate")))
        self.assertFalse(referenced("crate is done", "Order a crate divider", self.others("Order a crate divider")))

    def test_every_word_still_names_it(self):
        self.assertTrue(referenced("pup into crate: done", "Pup into crate", self.others("Pup into crate")))
        self.assertTrue(referenced("evening stroll done", "Evening stroll", self.others("Evening stroll")))
        self.assertFalse(referenced("evening stroll done", "Midday stroll", self.others("Midday stroll")))


class TestDoneOnTheUsersWords(RoutineCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("07:55"))
        for title, start in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                               "alongside": True})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def done(self, title, said):
        self.t.user_message = said
        self.t.prior_exchange = ("", "")
        try:
            return self.t.execute("log_outcome", {"event": title, "status": "done"})
        finally:
            self.t.user_message = None
            self.t.prior_exchange = None

    def status(self, title):
        return self.spans(self.t, title)[0].status

    def test_the_items_the_user_plainly_named_are_marked(self):
        said = "stroll, breakfast + trick practice all finished!! pup is settled in the crate w a chew toy"
        for title in ("Morning stroll + kibble breakfast", "Pup into crate"):
            out = self.done(title, said)
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertEqual(self.status(title), SpanStatus.DONE)

    def test_a_shared_word_means_the_one_whose_time_has_come(self):
        out = self.done("Pup into crate", "crate is done, stroll is done")
        self.assertFalse(out.startswith(NOT_DONE), out)
        out = self.done("Morning stroll + kibble breakfast", "crate is done, stroll is done")
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_an_item_the_words_dont_point_to_is_still_refused(self):
        for title, said in (("Order a crate divider", "crate is done"), ("Midday stroll", "stroll is done"),
                            ("Midday stroll", "evening stroll done"), ("Pup's vet visit", "laundry done"),
                            ("Order a crate divider", "pup is settled in the crate now, finished")):
            out = self.done(title, said)
            self.assertTrue(out.startswith(NOT_DONE), (title, said, out))
            self.assertEqual(self.status(title), SpanStatus.PLANNED)


if __name__ == "__main__":
    unittest.main()
