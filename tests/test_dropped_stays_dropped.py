"""A leftover the user drops stays dropped (before_today.py, carry.py rule 6). One that lives only on the Today note
is filed in the month's Dropped note, so its line leaves Today and undrop puts it back; one with a home list loses
only its Today copy, its home untouched. The morning pass never offers a dropped item again, even while a line of
it is still on Today, unless it was put on Today again after the drop. Data guard: Dropped is filed from Today only
on the user's own drop, never from a home list, and every other filing still needs a drop word. Invented fixtures:
a pottery studio."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
from thirties_core.carry import DROPPED, OpenPool, note_added_today, sort_leftovers
from thirties_core.done_log import file_dropped_op
from thirties_core.joplin_writeback import JoplinWriter, current_turn
from thirties_core.write_guard import refused

HOME = "- [ ] Call the bank\n- [ ] Buy stamps\n"
SHARDS, PUGMILL = "Sweep the shard bin", "Oil the pug mill"


class DroppedBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.t.assistant
        self.api.notes["n_today"]["body"] = f"- [ ] {SHARDS}\n- [ ] {PUGMILL}\n"
        self.pool = OpenPool(self.sched.state_db)
        self.pool.add([{"title": SHARDS, "task_id": "", "horizon": "week"},
                       {"title": PUGMILL, "task_id": "", "horizon": "week"}], DAY - timedelta(days=1))

    def act(self, item, action, said=None):
        self.t.user_message = said
        try:
            return self.t.execute("leftover_action", {"item": item, "action": action})
        finally:
            self.t.user_message = None

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped ")), None)


class TodayOnlyTest(DroppedBase):
    def test_a_dropped_today_only_line_is_filed_and_leaves_today(self):
        out = self.act(SHARDS, "drop", "sure, that shard bin one")
        self.assertIn("filed in this month's Dropped note", out)
        self.assertNotIn(SHARDS, self.body("n_today"))
        self.assertIn(f"- [ ] {PUGMILL}", self.body("n_today"))  # the other line is untouched
        self.assertIn(SHARDS, self.dropped_note()["body"])
        self.assertEqual(self.pool.all()[SHARDS.lower()]["horizon"], DROPPED)
        self.assertEqual(self.body("n_errands"), HOME)

    def test_undrop_puts_the_line_back_on_today(self):
        self.act(SHARDS, "drop")
        out = self.act(SHARDS, "undrop")
        self.assertIn(f"{SHARDS}: back", out)
        self.assertIn(f"- [ ] {SHARDS}", self.body("n_today"))
        self.assertNotIn(SHARDS, self.dropped_note()["body"])

    def test_the_apps_own_drop_files_nothing(self):
        rec = self.pool.all()[SHARDS.lower()]
        self.a.before._drop(rec, by="app")
        self.assertIn(f"- [ ] {SHARDS}", self.body("n_today"))
        self.assertIsNone(self.dropped_note())


class HomedTest(DroppedBase):
    def test_a_home_list_line_loses_only_its_today_copy(self):
        self.t.execute("add_to_today", {"items": ["Call the bank"]})
        self.pool.add([{"title": "Call the bank", "task_id": "t_bank", "horizon": "week"}], DAY - timedelta(days=1))
        out = self.act("Call the bank", "drop", "skip the bank one")
        self.assertIn("stays on its own list", out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assertEqual(self.body("n_errands"), HOME)
        self.assertIsNone(self.dropped_note())


class MorningTest(DroppedBase):
    def offered(self, day=DAY):
        out = sort_leftovers(self.a, day)
        return [i["title"].lower() for i in out["pool_items"]] + [t.lower() for t in out["ask"]]

    def test_a_dropped_line_still_on_today_is_never_offered(self):
        self.pool.set(PUGMILL, horizon=DROPPED, dropped_on=(DAY - timedelta(days=1)).isoformat(), dropped_by="app")
        offered = self.offered()
        self.assertNotIn(PUGMILL.lower(), offered)
        self.assertIn(SHARDS.lower(), offered)  # not dropped: offered as before

    def test_put_on_today_again_after_the_drop_it_is_offered(self):
        self.pool.set(PUGMILL, horizon=DROPPED, dropped_on=(DAY - timedelta(days=3)).isoformat(), dropped_by="user")
        note_added_today(self.sched.state_db, DAY - timedelta(days=1), [PUGMILL])
        self.assertIn(PUGMILL.lower(), self.offered())


class GuardTest(DroppedBase):
    """The write guard on the Dropped filing these drops use."""

    def test_the_users_drop_files_from_today_whatever_the_words(self):
        op = file_dropped_op("n_today", SHARDS, "Today To-Do", DAY)
        op["_drop_asked"] = True
        self.assertEqual(refused(op, "[app] sure, that shard bin one", self.sched.state_db), "")

    def test_never_from_a_home_list(self):
        op = file_dropped_op("n_errands", "Call the bank", "Errands/Admin", DAY)
        op["_drop_asked"] = True
        self.assertIn("not written", refused(op, "[app] sure, that shard bin one", self.sched.state_db))

    def test_every_other_filing_still_needs_a_drop_word(self):
        op = file_dropped_op("n_today", SHARDS, "Today To-Do", DAY)
        token = current_turn.set("[app] sure, that shard bin one")
        try:
            status, _ = JoplinWriter(self.sched.config, self.sched.state_db, self.api).submit(op)
        finally:
            current_turn.reset(token)
        self.assertEqual(status, "failed")
        self.assertIn(f"- [ ] {SHARDS}", self.body("n_today"))
        self.assertIsNone(self.dropped_note())


if __name__ == "__main__":
    unittest.main()
