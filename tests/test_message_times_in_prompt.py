"""The model reads when each earlier message was sent, rather than guessing: the prompt lists every message the
user saw with its time. Drafts the user never saw and the app's own checks are left out.

Invented messages for someone at a furnace; DAY is Thu Oct 15."""

import unittest

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core.astronomy import set_debug_time


class TestTheModelReadsWhenMessagesWereSent(ServiceCase):

    def say(self, role, text, hhmm, **extra):
        self.manager.brain.messages.append({"role": role, "content": text, "at": at(hhmm).isoformat(), **extra})

    def section(self):
        prompt = self.manager.brain.build_system_prompt()
        self.assertIn("WHEN EACH EARLIER MESSAGE WAS SENT", prompt)
        return prompt.split("WHEN EACH EARLIER MESSAGE WAS SENT", 1)[1].split("SCHEDULE\n", 1)[0]

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:50"))
        self.manager = self.svc.manager()

    def test_each_earlier_message_has_its_time(self):
        self.say("assistant", "👋 Just checking in: did you get to Marver the paperweight?", "09:40")
        self.say("user", "The vase is in the lehr now", "10:20")
        self.say("assistant", "An unseen draft", "10:20", hidden=True)
        self.say("user", self.manager.brain.CHECK_PREFIX + " a check of the app's own", "10:20")
        lines = [line for line in self.section().splitlines() if line.startswith("- ")]
        self.assertEqual(lines, ["- 9:40 AM, you: 👋 Just checking in: did you get to Marver the paperweight?",
                                 "- 10:20 AM, the user: The vase is in the lehr now"])

    def test_a_message_with_no_time_is_never_given_one(self):
        self.manager.brain.messages.append({"role": "assistant", "content": "Annealing starts soon."})
        prompt = self.manager.brain.build_system_prompt()
        self.assertNotIn("Annealing starts soon", prompt.split("SCHEDULE\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
