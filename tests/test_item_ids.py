"""Item identity that follows the user's edits.

An item's id is no longer worked out from its wording: Thirties keeps a table of every checklist line it has seen
and, on each read, matches a note's lines to what it saw last time, so an id follows a line through rewording,
moves and reordering; a finished copy and a new copy of the same wording are different items; and what Thirties
changes itself keeps its id without anything being re-keyed before Joplin has the change.

All notes and items are invented.
"""

import unittest
from datetime import date

from tests.fixtures import make_scheduler
from thirties_core.item_ids import ItemIds, checklist_lines
from thirties_core.joplin_engine import checklist_tasks, item_id


def note(nid, body, title="List", notebook="Home"):
    return {"id": nid, "title": title, "source_notebook": notebook, "body": body}


class Ids(unittest.TestCase):
    def setUp(self):
        self.db = make_scheduler().state_db
        self.ids = ItemIds(self.db)

    def follow(self, *notes):
        return self.ids.follow(list(notes))

    def id_of(self, mapping, nid, body, text, done=False, nth=0):
        lines = checklist_lines(body)
        hits = [i for i, (t, d) in enumerate(lines) if t == text and d == done]
        return mapping[nid][hits[nth]]


class FirstReadTest(Ids):
    def test_the_first_read_keeps_the_ids_items_already_have(self):
        """Plans, links and history made before this change point at the old ids: they must keep working."""
        body = "- [ ] Buy stamps\n- [ ] Call the bank\n- [ ] Buy stamps\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Buy stamps"), item_id("n1", "Buy stamps", 0))
        self.assertEqual(self.id_of(got, "n1", body, "Buy stamps", nth=1), item_id("n1", "Buy stamps", 1))
        self.assertEqual(self.id_of(got, "n1", body, "Call the bank"), item_id("n1", "Call the bank", 0))

    def test_ids_are_unique(self):
        body = "- [x] Laundry\n- [ ] Laundry\n- [ ] Laundry\n"
        got = self.follow(note("n1", body))
        self.assertEqual(len(set(got["n1"])), 3)

    def test_a_finished_copy_recorded_done_keeps_its_id_and_the_open_copy_gets_a_new_one(self):
        """For data from before: Monday's Laundry was done (recorded under the first open copy's id), then
        ticked; Thursday's copy slid into that id and was hidden as done."""
        self.db.record_task_completed(item_id("n1", "Laundry", 0), date(2026, 11, 9))
        body = "- [x] Laundry\n- [ ] Laundry\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Laundry", done=True), item_id("n1", "Laundry", 0))
        self.assertNotEqual(self.id_of(got, "n1", body, "Laundry"), item_id("n1", "Laundry", 0))


class FollowsEditsTest(Ids):
    BODY = "## Errands\n- [ ] Buy stamps\n- [ ] Call the bank\n- [ ] Return the library books\n"

    def setUp(self):
        super().setUp()
        first = self.follow(note("n1", self.BODY))
        self.bank = self.id_of(first, "n1", self.BODY, "Call the bank")
        self.stamps = self.id_of(first, "n1", self.BODY, "Buy stamps")

    def test_lines_added_above_dont_change_ids(self):
        body = "## Errands\n- [ ] Fix the gate\n- [ ] Buy stamps\n- [ ] Call the bank\n- [ ] Return the library books\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Call the bank"), self.bank)

    def test_a_reworded_item_keeps_its_id(self):
        body = self.BODY.replace("Call the bank", "Call the bank about the fee")
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Call the bank about the fee"), self.bank)

    def test_reordered_items_keep_their_ids(self):
        body = "## Errands\n- [ ] Return the library books\n- [ ] Call the bank\n- [ ] Buy stamps\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Call the bank"), self.bank)
        self.assertEqual(self.id_of(got, "n1", body, "Buy stamps"), self.stamps)

    def test_an_item_moved_to_another_note_keeps_its_id(self):
        other = "## Money\n- [ ] Pay the water bill\n"
        self.follow(note("n1", self.BODY), note("n2", other))
        body1 = self.BODY.replace("- [ ] Call the bank\n", "")
        body2 = other + "- [ ] Call the bank\n"
        got = self.follow(note("n1", body1), note("n2", body2))
        self.assertEqual(self.id_of(got, "n2", body2, "Call the bank"), self.bank)

    def test_ticking_keeps_the_id(self):
        body = self.BODY.replace("- [ ] Call the bank", "- [x] Call the bank")
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Call the bank", done=True), self.bank)

    def test_ticking_one_copy_never_gives_the_other_its_id(self):
        """Ticking Monday's Laundry must not give Thursday's copy Monday's id."""
        body = "- [ ] Laundry\n- [ ] Buy stamps\n- [ ] Laundry\n"
        got = self.follow(note("n3", body))
        monday, thursday = self.id_of(got, "n3", body, "Laundry"), self.id_of(got, "n3", body, "Laundry", nth=1)
        ticked = "- [x] Laundry\n- [ ] Buy stamps\n- [ ] Laundry\n"
        got = self.follow(note("n3", ticked))
        self.assertEqual(self.id_of(got, "n3", ticked, "Laundry", done=True), monday)
        self.assertEqual(self.id_of(got, "n3", ticked, "Laundry"), thursday)

    def test_an_item_added_again_after_it_was_done_is_a_new_item(self):
        """Re-adding "Buy stamps" must not give it the id already recorded as done."""
        cleared = self.BODY.replace("- [ ] Buy stamps\n", "- [x] Buy stamps\n")
        self.follow(note("n1", cleared))
        gone = self.BODY.replace("- [ ] Buy stamps\n", "")  # the finished line cleared away
        self.follow(note("n1", gone))
        again = gone + "- [ ] Buy stamps\n"
        got = self.follow(note("n1", again))
        self.assertNotEqual(self.id_of(got, "n1", again, "Buy stamps"), self.stamps)

    def test_the_tasks_read_from_joplin_carry_the_followed_ids(self):
        body = "## Errands\n- [ ] Fix the gate\n- [ ] Buy stamps\n- [ ] Call the bank about the fee\n"
        ids = self.follow(note("n1", body))["n1"]
        tasks = checklist_tasks("n1", "Errands", "Home", body, [], ids=ids)
        by_title = {t.title: t.id for t in tasks}
        self.assertEqual(by_title["Call the bank about the fee"], self.bank)
        self.assertEqual(by_title["Buy stamps"], self.stamps)

    def test_an_unchanged_note_is_not_rewritten(self):
        before = self.ids.revision("n1")
        self.follow(note("n1", self.BODY))
        self.assertEqual(self.ids.revision("n1"), before)


class ThirtiesOwnChangesTest(Ids):
    """What Thirties changes itself keeps its id, and nothing is re-keyed before Joplin has the change."""

    BODY = "- [ ] Buy stamps\n- [ ] Call the bank\n"

    def setUp(self):
        super().setUp()
        got = self.follow(note("n1", self.BODY))
        self.bank = self.id_of(got, "n1", self.BODY, "Call the bank")

    def test_a_rewording_by_thirties_keeps_the_id_however_different_the_words(self):
        self.ids.expect("n1", "Phone the credit union about the card fee", self.bank)
        body = "- [ ] Buy stamps\n- [ ] Phone the credit union about the card fee\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Phone the credit union about the card fee"), self.bank)

    def test_until_the_write_lands_the_item_keeps_its_id_where_it_is(self):
        self.ids.expect("n1", "Phone the credit union", self.bank)
        got = self.follow(note("n1", self.BODY))  # Joplin hasn't got the change yet
        self.assertEqual(self.id_of(got, "n1", self.BODY, "Call the bank"), self.bank)
        self.assertEqual(self.ids.find("n1", "Call the bank"), self.bank)

    def test_a_move_by_thirties_keeps_the_id(self):
        self.follow(note("n1", self.BODY), note("n2", "- [ ] Pay the water bill\n"))
        self.ids.expect("n2", "Call the bank", self.bank)
        body2 = "- [ ] Pay the water bill\n- [ ] Call the bank\n"
        got = self.follow(note("n1", "- [ ] Buy stamps\n"), note("n2", body2))
        self.assertEqual(self.id_of(got, "n2", body2, "Call the bank"), self.bank)

    def test_a_line_thirties_adds_gets_the_id_it_reserved(self):
        self.follow(note("n1", "- [x] Buy stamps\n- [ ] Call the bank\n"))
        self.follow(note("n1", "- [ ] Call the bank\n"))  # the finished line cleared away
        new = self.ids.reserve("n1", "Buy stamps")
        self.assertNotEqual(new, item_id("n1", "Buy stamps", 0))  # that one was done: a new item gets a new id
        body = "- [ ] Call the bank\n- [ ] Buy stamps\n"
        got = self.follow(note("n1", body))
        self.assertEqual(self.id_of(got, "n1", body, "Buy stamps"), new)

    def test_a_new_wording_reserves_the_plain_id(self):
        self.assertEqual(self.ids.reserve("n1", "Water the ferns"), item_id("n1", "Water the ferns", 0))

    def test_find_gives_the_live_id_of_a_line(self):
        self.assertEqual(self.ids.find("n1", "Call the bank"), self.bank)
        self.assertIsNone(self.ids.find("n1", "Something else"))


if __name__ == "__main__":
    unittest.main()
