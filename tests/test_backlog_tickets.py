"""Tickets an agent wrote into the user's lists don't fill the schedule. An invented backlog
("Recipe app dev") and one agent-style ticket dropped into an ordinary list."""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE
from thirties_core.backlog_tickets import is_ticket, reads_like_ticket

TICKET = ("**Status: drafted** wire the recipe importer to `parse_feed()`; next step in "
          "[the plan](https://example.org/plan)")


def ticket(tid, title, note="n_dev", list_title="Recipe app dev"):
    return TaskItem(id=tid, source_notebook="2. Dev", title=title, is_checkbox_item=True,
                    parent_note_title=list_title, parent_note_id=note)


class TicketCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:00"))
        self.api.notes["n_dev"] = {"id": "n_dev", "title": "Recipe app dev",
                                   "body": "- [ ] Fix the importer\n- [ ] Add a search page\n"}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_dev", "backlog", "Recipe app dev")
        self.services.tasks += [ticket("t_importer", "Fix the importer"), ticket("t_search", "Add a search page"),
                                item("t_agent", TICKET)]
        for tid in ("t_importer", "t_agent"):
            catalog.set_urgency(tid, "now")
        self.services.tasks[:] = catalog.apply(self.services.tasks)


class TestTickets(TicketCase):

    def test_what_reads_like_a_ticket(self):
        self.assertTrue(reads_like_ticket(TICKET))
        self.assertFalse(reads_like_ticket("Read the article at https://example.org/a"))  # a link alone: the user's own
        self.assertFalse(reads_like_ticket("**Call the bank**"))
        self.assertTrue(is_ticket(next(t for t in self.services.tasks if t.id == "t_importer")))  # backlog role

    def test_the_days_options_never_offer_a_ticket(self):
        titles = [c["title"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)]
        self.assertNotIn("Fix the importer", titles)
        self.assertFalse(any("Status: drafted" in t for t in titles))

    def test_a_ticket_the_user_didnt_name_is_held_back(self):
        self.t.user_message = "plan my afternoon"
        out = self.t.execute("add_event", {"title": "Fix the importer", "task_id": "t_importer", "category": "task",
                                           "start": "2:00 PM", "duration": "1 hour"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Time on Recipe app dev", out)
        self.assertNotIn("Fix the importer", [s.title for s in self.t.day_plan.spans])

    def test_one_dropped_into_an_ordinary_list_is_held_back_too(self):
        self.t.user_message = "put the errands on today"
        out = self.t.execute("add_to_today", {"items": [TICKET]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.body("n_today"), "")

    def test_a_ticket_the_user_names_goes_on(self):
        self.t.user_message = "I want to fix the importer at 2"
        out = self.t.execute("add_event", {"title": "Fix the importer", "task_id": "t_importer", "category": "task",
                                           "start": "2:00 PM", "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_time_on_the_project_goes_on(self):
        self.t.user_message = "plan my afternoon"
        out = self.t.execute("add_event", {"title": "Time on Recipe app dev", "start": "2:00 PM",
                                           "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_the_apps_own_buttons_are_never_held(self):
        self.t.user_message = None
        out = self.t.execute("add_event", {"title": "Fix the importer", "task_id": "t_importer", "category": "task",
                                           "start": "2:00 PM", "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
