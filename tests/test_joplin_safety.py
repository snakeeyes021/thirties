"""Invariants of the Joplin write path.

- A write never overwrites an edit made in between (typed in the open note, or synced from a phone): it
  re-checks the note's version just before writing and starts again from a fresh read if it changed.
- A retried write never duplicates: a write that timed out but was saved, or whose outcome is unknown, is
  recognised as done.
- Every queued change has a stable id and a validity window; a day's housekeeping never runs on another day,
  and a removal takes the copy it means.

All notes and items are invented.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.fixtures import FakeJoplinAPI, make_scheduler
from thirties_core.joplin_writeback import JoplinWriter, WriteLog, remove_item


class VersionedJoplin(FakeJoplinAPI):
    """The fake Data API with Joplin's ``updated_time``, and ways to misbehave:

    - ``edit_before_put``: (note_id, new body) typed by the user right after Thirties first reads that note, before
      it writes (Joplin itself has no version check: a plain PUT would overwrite it);
    - ``lose_replies``: that many writes are saved but the reply is lost (a timeout);
    - ``crash_after_put``: Joplin saves the next write, then goes away before anything else is read.
    """

    def __init__(self, notes=None, up=True):
        super().__init__(notes, up)
        for n in self.notes.values():
            n.setdefault("updated_time", 1000)
        self.edit_before_put = None
        self.lose_replies = 0
        self.crash_after_put = False
        self.puts = 0

    def _touch(self, note_id):
        self.notes[note_id]["updated_time"] += 1

    def user_edit(self, note_id, body):
        self.notes[note_id]["body"] = body
        self._touch(note_id)

    def get_note(self, note_id, fields=""):
        note = super().get_note(note_id, fields)
        if note is not None and self.edit_before_put and self.edit_before_put[0] == note_id:
            _, body = self.edit_before_put  # the user's keystrokes land just after this read
            self.edit_before_put = None
            self.user_edit(note_id, body)
        return note

    def update_note(self, note_id, data):
        if not self.up or note_id not in self.notes:
            return False
        self.notes[note_id].update(data)
        self._touch(note_id)
        self.updates.append((note_id, data))
        self.puts += 1
        if self.crash_after_put:
            self.crash_after_put = False
            self.up = False
            return False
        if self.lose_replies:
            self.lose_replies -= 1
            return False
        return True


def writer_for(api):
    sched = make_scheduler()
    sched.config.joplin.write_back, sched.config.joplin.api_token = True, "tok"
    return JoplinWriter(sched.config, sched.state_db, api), sched.state_db


ERRANDS = "## Errands\n- [ ] Buy stamps\n- [ ] Return the library books\n"


class NoLostEditsTest(unittest.TestCase):
    """Thirties' write and the user's edit both survive."""

    def test_an_edit_typed_between_read_and_write_survives(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}})
        writer, _ = writer_for(api)
        # The user adds a line while Thirties is about to add its own.
        api.edit_before_put = ("n1", ERRANDS + "- [ ] Water the ferns\n")
        state, _ = writer.submit({"op": "add_task", "note_id": "n1", "title": "Call the plumber"})
        self.assertEqual(state, "done")
        body = api.notes["n1"]["body"]
        self.assertIn("- [ ] Water the ferns", body)
        self.assertIn("- [ ] Call the plumber", body)
        self.assertEqual(body.count("Call the plumber"), 1)

    def test_a_tick_doesnt_undo_an_edit_made_meanwhile(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}})
        writer, _ = writer_for(api)
        api.edit_before_put = ("n1", ERRANDS.replace("Return the library books", "Return the library books (2)"))
        writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Buy stamps"})
        body = api.notes["n1"]["body"]
        self.assertIn("- [x] Buy stamps", body)
        self.assertIn("Return the library books (2)", body)

    def test_a_move_between_notes_keeps_an_edit_made_meanwhile(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS},
                               "n2": {"id": "n2", "title": "Home", "body": "## Home\n- [ ] Fix the gate\n"}})
        writer, _ = writer_for(api)
        api.edit_before_put = ("n2", "## Home\n- [ ] Fix the gate\n- [ ] Oil the hinges\n")
        state, _ = writer.submit({"op": "move", "note_id": "n1", "to_note_id": "n2", "title": "Buy stamps",
                                  "section": "Home"})
        self.assertEqual(state, "done")
        self.assertIn("Oil the hinges", api.notes["n2"]["body"])
        self.assertEqual(api.notes["n2"]["body"].count("Buy stamps"), 1)
        self.assertNotIn("Buy stamps", api.notes["n1"]["body"])


class NoDuplicatesTest(unittest.TestCase):
    """A write that was saved is never applied twice, whatever Joplin answered."""

    def test_an_add_whose_reply_was_lost_isnt_added_again(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}})
        writer, _ = writer_for(api)
        api.lose_replies = 1
        state, _ = writer.submit({"op": "add_task", "note_id": "n1", "title": "Call the plumber"})
        writer.flush(launch=False)
        self.assertEqual(state, "done")  # the read-back shows it landed
        self.assertEqual(api.notes["n1"]["body"].count("Call the plumber"), 1)

    def test_an_add_cut_off_by_a_crash_isnt_added_again(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}})
        writer, db = writer_for(api)
        api.crash_after_put = True
        state, _ = writer.submit({"op": "add_task", "note_id": "n1", "title": "Call the plumber"})
        self.assertEqual(state, "queued")  # Joplin went away: nobody knows yet whether it landed
        api.up = True
        writer.flush(launch=False)
        writer.flush(launch=False)
        self.assertEqual(api.notes["n1"]["body"].count("Call the plumber"), 1)
        self.assertEqual(db.outbox_pending(), [])

    def test_a_tick_cut_off_by_a_crash_doesnt_tick_a_second_copy(self):
        body = "- [ ] Water the plants\n- [ ] Buy stamps\n- [ ] Water the plants\n"
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Chores", "body": body}})
        writer, _ = writer_for(api)
        api.crash_after_put = True
        writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Water the plants",
                       "occurrence": 0})
        api.up = True
        api.user_edit("n1", api.notes["n1"]["body"] + "- [ ] Sweep the porch\n")  # edited before the retry
        writer.flush(launch=False)
        lines = api.notes["n1"]["body"].splitlines()
        self.assertEqual(lines.count("- [x] Water the plants"), 1)
        self.assertEqual(lines.count("- [ ] Water the plants"), 1)

    def test_a_move_cut_off_by_a_crash_leaves_exactly_one_copy(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS},
                               "n2": {"id": "n2", "title": "Home", "body": "## Home\n- [ ] Fix the gate\n"}})
        writer, db = writer_for(api)
        api.crash_after_put = True  # the receiving note is written first, then Joplin is gone
        writer.submit({"op": "move", "note_id": "n1", "to_note_id": "n2", "title": "Buy stamps", "section": "Home"})
        api.up = True
        writer.flush(launch=False)
        both = api.notes["n1"]["body"] + api.notes["n2"]["body"]
        self.assertEqual(both.count("Buy stamps"), 1)
        self.assertIn("Buy stamps", api.notes["n2"]["body"])
        self.assertEqual(db.outbox_pending(), [])

    def test_nothing_to_change_writes_nothing(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Morning", "body": "- [ ] Stretch\n"}})
        writer, _ = writer_for(api)
        state, _ = writer.submit({"op": "untick_all", "note_id": "n1", "title": "Morning"})
        self.assertEqual(state, "done")
        self.assertEqual(api.updates, [])


class ExpiryAndIdentityTest(unittest.TestCase):
    """Queued changes carry an id and a validity window; a removal takes the copy it means."""

    def test_each_change_gets_a_stable_id_that_is_logged(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}}, up=False)
        writer, db = writer_for(api)
        writer.submit({"op": "add_task", "note_id": "n1", "title": "Call the plumber"})
        queued = json.loads(db.outbox_pending()[0]["op"])
        self.assertTrue(queued.get("_id"))
        api.up = True
        writer.flush(launch=False)
        logged = WriteLog(db).since()
        self.assertEqual(logged[-1]["op"].get("_id"), queued["_id"])

    def test_yesterdays_clear_never_runs_on_todays_ticks(self):
        api = VersionedJoplin({"t": {"id": "t", "title": "Today", "body": "- [x] Buy stamps\n"}}, up=False)
        writer, db = writer_for(api)
        writer.submit({"op": "clear_ticked", "note_id": "t", "title": "Today",
                       "valid_until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
        api.up = True
        api.user_edit("t", "- [x] Morning walk\n- [ ] Call the plumber\n")  # the new day's ticks
        writer.flush(launch=False)
        self.assertIn("- [x] Morning walk", api.notes["t"]["body"])
        self.assertEqual(db.outbox_pending(), [])  # set aside, not kept for ever

    def test_day_bound_housekeeping_expires_by_itself(self):
        api = VersionedJoplin({"t": {"id": "t", "title": "Today", "body": "- [x] Buy stamps\n"}}, up=False)
        writer, db = writer_for(api)
        writer.submit({"op": "clear_ticked", "note_id": "t", "title": "Today"})
        queued = json.loads(db.outbox_pending()[0]["op"])
        self.assertTrue(queued.get("valid_until"))

    def test_lasting_changes_dont_expire(self):
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}}, up=False)
        writer, db = writer_for(api)
        writer.submit({"op": "add_task", "note_id": "n1", "title": "Call the plumber"})
        self.assertFalse(json.loads(db.outbox_pending()[0]["op"]).get("valid_until"))

    def test_a_removal_takes_the_copy_it_means(self):
        body = "- [x] Lunch\n- [ ] Buy stamps\n- [ ] Lunch\n"
        self.assertEqual(remove_item(body, "Lunch", state="open"), "- [x] Lunch\n- [ ] Buy stamps\n")
        self.assertEqual(remove_item(body, "Lunch", state="ticked"), "- [ ] Buy stamps\n- [ ] Lunch\n")
        self.assertEqual(remove_item("- [ ] Lunch\n- [ ] Lunch\n", "Lunch", occurrence=1, state="open"),
                         "- [ ] Lunch\n")
        self.assertEqual(remove_item(body, "Lunch"), "- [ ] Buy stamps\n- [ ] Lunch\n")  # unchanged default

    def test_a_queued_open_removal_never_takes_a_ticked_copy(self):
        api = VersionedJoplin({"t": {"id": "t", "title": "Today", "body": "- [ ] Lunch\n"}}, up=False)
        writer, _ = writer_for(api)
        writer.submit({"op": "remove", "note_id": "t", "title": "Lunch", "state": "open"})
        api.up = True
        api.user_edit("t", "- [x] Lunch\n")  # eaten and ticked before Joplin was back
        writer.flush(launch=False)
        self.assertEqual(api.notes["t"]["body"], "- [x] Lunch\n")


if __name__ == "__main__":
    unittest.main()
