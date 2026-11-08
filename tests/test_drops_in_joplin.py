"""Dropped items in Joplin (11-06: a drop has to be ticked off and filed in the proper archive list in Joplin): an
applied drop is ticked and filed in the month's Dropped note beside the Done notes; undrop
puts it back where it was, open; done goes the normal done way; a proposal alone never touches Joplin. On a fake
copy of the notes, never the user's."""

import json
from datetime import timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
from thirties_core.carry import OpenPool

ERRANDS = "## Errands\n- [ ] Call the bank\n- [ ] Buy stamps\n- [ ] Return the parcel\n\n## Someday\n- [ ] Learn the harp\n"


class TestDropsInJoplin(TodayTestCase):

    def setUp(self):
        super().setUp()
        from tests.test_today import TITLES, item
        from thirties_core.lists import ListCatalog
        self.api.notes["n_errands"]["body"] = ERRANDS
        self.services.tasks = ListCatalog(self.sched.state_db).apply(
            [item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps"), item("t_parcel", "Return the parcel")])
        self.pool = OpenPool(self.sched.state_db)
        self.pool.add([{"title": "Call the bank", "task_id": "t_bank", "horizon": "week", "when": "this week",
                        "why": "the branch closed for good", "pending_drop": True, "proposed_on": DAY.isoformat()}],
                      DAY - timedelta(days=1))
        self.before = {"errands": self.body("n_errands")}

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if n.get("title", "").startswith("Dropped ")), None)

    def test_a_proposal_alone_touches_nothing_then_accepting_files_it(self):
        self.assertEqual(self.body("n_errands"), ERRANDS)  # proposed: Joplin as it was
        self.assertIsNone(self.dropped_note())
        out = self.t.execute("leftover_action", {"item": "Call the bank", "action": "accept"})
        self.assertIn("Dropped Call the bank", out)
        after = self.body("n_errands")
        print("\n--- Errands/Admin before:\n" + ERRANDS + "--- after the drop:\n" + after)
        self.assertNotIn("Call the bank", after)
        note = self.dropped_note()
        print("--- " + note["title"] + ":\n" + note["body"])
        self.assertEqual(note["title"], "Dropped " + DAY.strftime("%Y-%m"))
        self.assertIn(f"- [x] {DAY.isoformat()}: Call the bank (Errands/Admin) · dropped: the branch closed for good",
                      note["body"])

    def test_undrop_puts_it_back_where_it_was_open(self):
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "drop"})
        out = self.t.execute("leftover_action", {"item": "Call the bank", "action": "undrop"})
        self.assertIn("Call the bank: back", out)
        after = self.body("n_errands")
        print("\n--- Errands/Admin after drop then undrop:\n" + after)
        self.assertEqual(after, ERRANDS)  # its place and section, open again
        self.assertNotIn("Call the bank", self.dropped_note()["body"])

    def test_undrop_after_its_neighbour_moved_goes_to_its_section(self):
        self.api.notes["n_errands"]["body"] = "## Errands\n- [ ] Buy stamps\n- [ ] Call the bank\n\n## Someday\n- [ ] x\n"
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "drop"})
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [ ] Buy stamps\n", "")  # gone by hand
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "undrop"})
        self.assertTrue(self.body("n_errands").startswith("## Errands\n- [ ] Call the bank\n"), self.body("n_errands"))

    def test_an_app_drop_after_its_day_is_filed_and_archived(self):
        from thirties_core.carry import settle
        self.pool.set("Call the bank", proposed_on=(DAY - timedelta(days=1)).isoformat())  # yesterday's proposal
        settle(self.t.assistant, DAY)
        self.assertNotIn("Call the bank", self.body("n_errands"))
        self.assertIn("Call the bank (Errands/Admin) · dropped", self.dropped_note()["body"])
        archive = dict(self.t.assistant.before.rows()["archive"])
        self.assertEqual([r["title"] for r in archive[DAY.isoformat()]], ["Call the bank"])
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "undrop"})
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))
        self.assertFalse(self.t.assistant.before.rows()["archive"])  # out of the archive, settled at its horizon
        self.assertIn("call the bank", OpenPool(self.sched.state_db).all(DAY))

    def test_done_goes_the_normal_done_way(self):
        self.pool.set("Call the bank", pending_drop=False)
        out = self.t.execute("leftover_action", {"item": "Call the bank", "action": "done"})
        self.assertIn("done", out)
        print("\n--- Errands/Admin after done:\n" + self.body("n_errands"))
        self.assertIn("- [x] Call the bank", self.body("n_errands"))  # ticked; the sweep files it in Done later
        self.assertNotIn("call the bank", OpenPool(self.sched.state_db).all())

    def test_someday_follows_the_lists_own_someday_section(self):
        self.pool.set("Call the bank", pending_drop=False)
        self.t.execute("place_leftover", {"item": "Call the bank", "horizon": "someday"})
        self.assertIn("## Someday\n- [ ] Learn the harp\n- [ ] Call the bank", self.body("n_errands"))

    def test_ticked_in_joplin_by_hand_is_done_here_too(self):
        self.pool.set("Call the bank", pending_drop=False)
        self.api.notes["n_errands"]["body"] = ERRANDS.replace("- [ ] Call the bank", "- [x] Call the bank")  # by hand
        self.t.assistant.before.reconcile()
        self.assertNotIn("call the bank", OpenPool(self.sched.state_db).all())

    def test_deleted_in_joplin_by_hand_is_gone_here_too(self):
        self.pool.set("Call the bank", pending_drop=False)
        self.api.notes["n_errands"]["body"] = ERRANDS.replace("- [ ] Call the bank\n", "")
        self.t.assistant.before.reconcile()
        self.assertNotIn("Call the bank", [r["title"] for r in self.t.assistant.before.rows()["keep"]])


class TestEveryActionSettlesTheRow(TestDropsInJoplin):
    """11-06: Accept on several rows left them where they were. Every action settles the item and
    takes it out of the group it was in, from the tab and from the chat alike."""

    def setUp(self):
        super().setUp()
        self.pool.add([{"title": "Buy stamps", "task_id": "t_stamps", "horizon": "week", "when": "this week",
                        "why": "a week old"}], DAY - timedelta(days=1))

    def groups(self):
        rows = self.t.assistant.before.rows()
        return ({r["title"] for r in rows["keep"]}, {r["title"] for r in rows["drop"]},
                {r["title"] for _, rs in rows["archive"] for r in rs})

    def act(self, item, action):
        return self.t.execute("leftover_action", {"item": item, "action": action})

    def test_accept_on_a_keep_row(self):
        self.assertIn("Buy stamps", self.groups()[0])
        self.act("Buy stamps", "accept")
        self.assertNotIn("Buy stamps", self.groups()[0])
        self.assertIn("buy stamps", OpenPool(self.sched.state_db).all(DAY))  # still waits at its horizon

    def test_done_on_a_keep_row(self):
        self.act("Buy stamps", "done")
        self.assertFalse(any("Buy stamps" in g for g in self.groups()))

    def test_drop_on_a_keep_row_goes_to_the_archive(self):
        self.act("Buy stamps", "drop")
        keep, drop, archive = self.groups()
        self.assertNotIn("Buy stamps", keep)
        self.assertIn("Buy stamps", archive)

    def test_accept_on_a_drop_row_goes_to_the_archive(self):
        self.assertIn("Call the bank", self.groups()[1])
        self.act("Call the bank", "accept")
        keep, drop, archive = self.groups()
        self.assertNotIn("Call the bank", drop)
        self.assertIn("Call the bank", archive)

    def test_keep_on_a_drop_row_places_it(self):
        self.act("Call the bank", "keep")
        keep, drop, archive = self.groups()
        self.assertNotIn("Call the bank", drop | keep | archive)
        self.assertEqual(OpenPool(self.sched.state_db).all(DAY)["call the bank"]["when"], "this week")

    def test_undrop_leaves_the_archive(self):
        self.act("Call the bank", "accept")
        self.act("Call the bank", "undrop")
        self.assertNotIn("Call the bank", self.groups()[2])

    def test_a_yes_in_the_chat_settles_them_too(self):
        import json
        self.sched.state_db.set_sync_meta(f"rollover:{DAY.isoformat()}", json.dumps({
            "plan": [], "today": {"keep": [], "add": []}, "state": "offered",
            "leftovers": {"keep": [{"title": "Buy stamps"}], "drop": [{"title": "Call the bank"}]}}))
        self.t.execute("apply_rollover", {})
        keep, drop, archive = self.groups()
        self.assertEqual((keep, drop), (set(), set()))
        self.assertIn("Call the bank", archive)


class TestADropRowShowsWhereKeepWouldPutIt(TestDropsInJoplin):
    """11-06: the user sees what would happen and what will happen. A proposed drop shows the
    horizon Keep would give it, and Keep does exactly that (unless the user names another, in the chat)."""

    def setUp(self):
        super().setUp()
        from thirties_core.carry import resolve_when
        self.next_week = resolve_when("next week", DAY)
        self.pool.set("Call the bank", **self.next_week)

    def test_the_row_shows_it_and_keep_uses_it(self):
        drop = self.t.assistant.before.rows()["drop"]
        self.assertEqual([(r["title"], r["when"]) for r in drop], [("Call the bank", "next week")])
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "keep"})
        rec = OpenPool(self.sched.state_db).all()["call the bank"]
        self.assertEqual((rec["when"], rec["start"], rec["end"]),
                         (self.next_week["when"], self.next_week["start"], self.next_week["end"]))
        rows = self.t.assistant.before.rows()
        self.assertNotIn("Call the bank", {r["title"] for g in ("keep", "drop") for r in rows[g]})  # settled

    def test_in_the_chat_a_named_horizon_wins(self):
        self.t.execute("leftover_action", {"item": "Call the bank", "action": "keep", "when": "tomorrow"})
        rec = OpenPool(self.sched.state_db).all()["call the bank"]
        self.assertEqual(rec["start"], (DAY + timedelta(days=1)).isoformat())
