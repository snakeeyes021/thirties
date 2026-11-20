"""A place the user gives by another item ("right after the repair visit") is kept as a tie, and it sticks.

Seen in simulated days: an errand the user wanted right after an appointment went to a clock time, then another,
then under "If there's time", then back, and was finally taken off today while still on the plan. A reply asking
"want me to move it?" was sent back to be rewritten as "make the change", and the rewrite made a change nobody asked
for. Now (relations.py):

- a move or new item the user places by another item keeps that tie, and later placings keep it;
- when the item it follows ends at another time, the tied item moves with it;
- a tied item isn't pencilled in, and taking a leftover line off "If there's time" leaves the planned item;
- a follow-up named like an item already on today is that item, moved, not a second copy;
- a reply that asks before a change the user didn't ask for reaches them as it is, and the change isn't made.

On invented items: someone who works a late shift at a pottery studio, here on a day off with a canoe trip.
"""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, event, make_scheduler
from tests.test_conversation import ConversationTestCase, reply
from tests.test_today import TITLES, item
from thirties_core import if_time
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE, PlanTools

REPAIR = "cal:repair"


class TieCase(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([event("repair", "12:00", "14:00", "Kiln repair visit")])
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n_errands": {"id": "n_errands", "body": "- [ ] Pick up the glaze order\n"}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES.get(nid) or n.get("title", ""), "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_today", "today", "Today To-Do")
        catalog.set_role("n_errands", "list", "Errands/Admin")
        self.services = Services.for_db(self.sched.state_db, catalog.apply([item("t_glaze", "Pick up the glaze order")]),
                                        joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)
        self.t.execute("remove_event", {"event": "Work"})  # a day off from the studio

    def say(self, message, tool, args):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def spans(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def span(self, title):
        found = self.spans(title)
        return found[0] if found else None

    def tie_it(self):
        out = self.t.execute("add_event", {"title": "Pick up the glaze order", "start": "4:00 PM", "duration": "1 hour",
                                           "task_id": "t_glaze", "category": "task"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        out = self.say("put the glaze pickup right after the kiln repair visit", "update_event",
                       {"event": "Pick up the glaze order", "start": "2:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        return out


class TestTheTieIsKept(TieCase):

    def test_kept_from_the_users_words(self):
        out = self.tie_it()
        self.assertIn("right after Kiln repair visit", out)
        self.assertIn(REPAIR, self.t._facts().get(self.span("Pick up the glaze order").id).after)

    def test_a_later_replan_keeps_it(self):
        self.tie_it()
        self.say("redo the afternoon, it's a mess", "update_event", {"event": "Pick up the glaze order", "start": "1:00 PM"})
        self.assertGreaterEqual(self.span("Pick up the glaze order").start_dt, at("14:00"))

    def test_before_another_item(self):
        self.t.execute("add_event", {"title": "Load the canoe", "start": "5:00 PM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Rinse the dry bags", "start": "6:00 PM", "duration": "30 min"})
        out = self.say("the dry bags need rinsing before I load the canoe", "update_event",
                       {"event": "Rinse the dry bags", "start": "4:30 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        canoe = self.span("Load the canoe")
        self.assertIn(canoe.id, self.t._facts().get(self.span("Rinse the dry bags").id).before)

    def test_no_tie_without_the_users_words(self):
        self.t.execute("add_event", {"title": "Pick up the glaze order", "start": "4:00 PM", "duration": "1 hour"})
        self.say("glaze pickup at 2 please", "update_event", {"event": "Pick up the glaze order", "start": "2:00 PM"})
        self.assertEqual(self.t._facts().get(self.span("Pick up the glaze order").id).after, [])


class TestItMovesWithWhatItFollows(TieCase):

    def test_the_visit_running_later(self):
        self.tie_it()
        out = self.say("the kiln repair visit runs to 3pm now", "update_event", {"event": REPAIR, "end": "3:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Pick up the glaze order").start_dt, at("15:00"))


class TestItStaysOnThePlan(TieCase):

    def test_not_pencilled_in(self):
        self.tie_it()
        out = self.say("glaze pickup goes right after the repair visit, not at a set time", "pencil_in",
                       {"items": ["Pick up the glaze order"]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Pick up the glaze order").start_dt, at("14:00"))
        self.assertNotIn(if_time.HEADING, self.api.notes["n_today"]["body"])

    def test_a_leftover_if_theres_time_line_leaves_the_planned_item(self):
        out = self.say("pick up the glaze order if there's time", "add_to_today", {"items": ["Pick up the glaze order"]})
        self.assertIn(if_time.HEADING, self.api.notes["n_today"]["body"])
        self.t.execute("add_event", {"title": "Pick up the glaze order", "start": "2:00 PM", "duration": "1 hour",
                                     "task_id": "t_glaze", "category": "task"})
        out = self.say("the glaze run is the reason for today, it's NOT a maybe", "if_there_is_time",
                       {"action": "drop", "item": "Pick up the glaze order"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("later this week", out)
        self.assertEqual(self.span("Pick up the glaze order").start_dt, at("14:00"))
        from thirties_core.carry import OpenPool
        self.assertFalse(any("glaze" in str(r).lower() for r in OpenPool(self.sched.state_db).all().items()))

    def test_a_follow_up_named_like_a_planned_item_is_that_item(self):
        self.t.execute("add_event", {"title": "Pick up the glaze order", "start": "5:00 PM", "duration": "1 hour",
                                     "task_id": "t_glaze", "category": "task"})
        out = self.say("glaze pickup right after the repair visit", "follow_up",
                       {"event": REPAIR, "title": "Pick up the glaze order", "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.spans("Pick up the glaze order")), 1)
        self.assertEqual(self.span("Pick up the glaze order").start_dt, at("14:00"))
        self.assertEqual(self.span("Pick up the glaze order").task_id, "t_glaze")

    def test_a_follow_up_for_a_pencilled_item_leaves_the_heading(self):
        self.say("pick up the glaze order if there's time", "add_to_today", {"items": ["Pick up the glaze order"]})
        out = self.say("glaze pickup right after the repair visit", "follow_up",
                       {"event": REPAIR, "title": "Pick up the glaze order", "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Pick up the glaze order").task_id, "t_glaze")
        self.assertEqual(if_time.items(self.api.notes["n_today"]), [])
        self.assertIn("Pick up the glaze order", self.api.notes["n_today"]["body"])  # still on today's list
        self.assertEqual(self.api.notes["n_errands"]["body"], "- [ ] Pick up the glaze order\n")  # home list untouched


class TestNoChangeMadeToDropAQuestion(ConversationTestCase):
    """A reply that asks before a change reaches the user as it is: no rewrite, and no change nobody asked for."""

    def test_the_question_stays_and_the_unasked_change_isnt_made(self):
        set_debug_time(at("09:10"))
        self.manager.execute_tool("doing_now", {"item": "Fire the test kiln", "duration": "2 hours"})
        set_debug_time(at("09:20"))
        self.engine.set_next_response(reply('CALL update_event {"event": "Fire the test kiln", "duration": "3 hours"}'))
        self.engine.set_next_response(reply("The test kiln ends around 12:00 PM, after the lake shuttle leaves. "
                                            "Would you like me to move the firing to this afternoon?"))
        out = self.manager.send_user_message("the test kiln needs about three hours. tell me when it ends")
        asked = [str(c[-1]["content"]) for c in self.engine.call_history]
        self.assertFalse(any("waits on a yes" in a for a in asked), asked)
        kiln = next(s for s in self.manager.day_plan.spans if s.title == "Fire the test kiln")
        self.assertEqual(kiln.start_dt, at("09:00"))  # under way: its start stays
        self.assertIn("ends around 12:00 PM", out)
        self.assertIn("Would you like me to move the firing", out)

if __name__ == "__main__":
    unittest.main()
