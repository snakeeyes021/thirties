"""The profile and the memory notes keep what the user asked to keep (profile_guard.py): their words ask for it
("remember…", "from now on…", "fyi…") or say how things always are for them ("I always…", "my goal is…"). A
preference guessed from one passing remark is refused, so nothing is saved and the model can leave it out. Invented
messages; DAY is Thu Oct 15."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.profile_guard import asked_to_keep

PASSING = ("great, off to grab a cinnamon bun now",
           "ugh the bus was late again, moving the dentist to 3",
           "thanks, the soup was lovely",
           "that slot won't work, the courier hasn't shown up yet",
           "push the call to 4 pls")
LASTING = ("remember I do the recycling on Tuesdays",
           "from now on keep mornings free for writing",
           "I always swim before work",
           "I never take calls after 8pm",
           "fyi the Someday list is just ideas",
           "my goal is to read 20 books this year",
           "I want to finish the quilt by spring",
           "anything under Maybe is stuff I won't get to")


class TestTheirWords(unittest.TestCase):

    def test_a_passing_remark_keeps_nothing(self):
        for said in PASSING:
            self.assertFalse(asked_to_keep(said), said)

    def test_asked_or_said_for_good(self):
        for said in LASTING:
            self.assertTrue(asked_to_keep(said), said)

    def test_nothing_said_is_the_app_itself(self):
        self.assertTrue(asked_to_keep(None))  # a direct call by the app's own pages, no conversation


class TestTheTools(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        self.services = Services.for_db(sched.state_db)
        self.t = PlanTools(plan, pending, sched, services=self.services)

    def test_an_unasked_save_is_refused_and_nothing_is_kept(self):
        self.t.user_message = "great, off to grab a cinnamon bun now"
        out = self.t.execute("update_profile", {"section": "Preferences", "text": "Likes cinnamon buns"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("cinnamon", self.services.profile.sections().get("Preferences", ""))
        out = self.t.execute("remember", {"note": "Gets a cinnamon bun every morning"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.services.memory.notes(), [])

    def test_an_asked_save_goes_through(self):
        self.t.user_message = "remember I do the recycling on Tuesdays"
        out = self.t.execute("remember", {"note": "Recycling goes out on Tuesdays"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.t.user_message = "I always swim before work"
        out = self.t.execute("update_profile", {"section": "Habits & routines", "text": "Swims before work"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Swims before work", self.services.profile.sections()["Habits & routines"])


if __name__ == "__main__":
    unittest.main()
