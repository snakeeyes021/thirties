"""Keep as it is, under "Put off several times" in the Before today tab: the offer clears at once, even for an
item that also has misses counted and a row in Keep, and that row stays as it was. Put off again, it's offered again.

Invented items: someone keeping a herbarium. DAY is Thu Oct 15."""

import unittest

from tests import test_backoff
from tests.fixtures import at
from tests.test_gui import GTK, _iterate_until  # noqa: F401  (sets up the package for the views)
from tests.test_today import item
from thirties_core.astronomy import set_debug_time
from thirties_core.backoff import MissStreaks
from thirties_core.break_down import open_offers
from thirties_core.lists import ListCatalog

TITLE = "Bind the herbarium"


class HerbariumCase(test_backoff.BackoffDays):
    """BackoffDays' set-up and helpers; its own tests run in test_backoff, not again here."""

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] += f"- [ ] {TITLE}\n"
        self.services.tasks.append(item("t_herb", TITLE))
        self.services.tasks[:] = ListCatalog(self.sched.state_db).apply(self.services.tasks)
        self.on(-1, TITLE, "t_herb")  # left unmarked yesterday: a miss, and a row in Keep
        self.morning(0)
        self.put_off(3)
        self.t.execute("rollover_proposal", {})

    def put_off(self, n):
        for _ in range(n):
            self.sched.state_db.record_task_deferred("t_herb")
        self.sched.attach_history_to_tasks(self.services.tasks)

    def keep_row(self):
        return next((r for r in self.t.assistant.before.rows().get("keep") or [] if r["title"] == TITLE), None)

    def offered(self):
        return [o["title"] for o in open_offers(self.sched.state_db)]


for _name in [n for n in dir(test_backoff.BackoffDays) if n.startswith("test_")]:
    setattr(HerbariumCase, _name, None)


class TestKeepAsItIs(HerbariumCase):

    def test_it_clears_the_offer_and_leaves_its_keep_row_alone(self):
        self.assertIsNotNone(MissStreaks(self.sched.state_db).get(TITLE, "t_herb"))  # in both sections
        row = self.keep_row()
        self.assertIsNotNone(row)
        self.assertEqual(self.offered(), [TITLE])
        out = self.t.execute("still_want", {"item": TITLE, "choice": "keep"})  # what the button sends
        self.assertEqual(out, f"{TITLE}: kept as it is.")
        self.assertEqual(self.offered(), [])
        self.assertEqual(self.keep_row(), row)  # its proposed time, still there to accept

    def test_put_off_again_it_is_offered_again(self):
        self.t.execute("still_want", {"item": TITLE, "choice": "keep"})
        set_debug_time(at("09:00", 1))
        self.assertNotIn("put off", self.t.execute("rollover_proposal", {}))
        self.put_off(1)
        set_debug_time(at("09:00", 2))
        self.assertIn(f"{TITLE} has been put off 4 times.", self.t.execute("rollover_proposal", {}))
        self.assertEqual(self.offered(), [TITLE])


@unittest.skipUnless(GTK, "GTK 4 / libadwaita or a display is not available")
class TestKeepAsItIsInTheTab(HerbariumCase):

    def test_the_button_clears_the_row_and_the_keep_row_stays(self):
        from thirties.views.before_today import BeforeTodayPage
        page = BeforeTodayPage()
        page.rows_provider = self.t.assistant.before.rows
        page.run_tool = self.t.execute
        page.offers_provider = lambda: open_offers(self.sched.state_db)
        page.refresh()
        self.assertIn("offers", page.groups)
        when = page._when(self.keep_row())
        page.buttons[("offer", TITLE, "keep")].emit("clicked")
        self.assertNotIn("offers", page.groups)
        self.assertNotIn(("offer", TITLE, "keep"), page.buttons)
        self.assertIn("keep", page.groups)
        self.assertEqual(page._when(self.keep_row()), when)


if __name__ == "__main__":
    unittest.main()
