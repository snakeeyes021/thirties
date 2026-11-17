"""A change that didn't happen is said in the reply itself, plainly, never as a note added under it.

A refused change used to leave the reply's false claim in place with a note under it ("(That move didn't
happen: ...)", "(Bake bread is still on the plan.)"). Now the reply is sent back once with the plain sentence to say,
written fresh (the user never saw the draft); if it still claims the change, that sentence is replaced in place with
the plain one, and the rest of the reply stands. Only what didn't change is named. On invented items only.
"""

from tests.fixtures import event
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.plan_tools import NOT_DONE

NOTE_MARKS = ("(That move", "(Nothing has moved", "(On the plan", "didn't happen", "(Dentist", "(Bake bread")


class PlainCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(events=[event("cal-dentist", "14:00", "14:45", "Dentist check-up")])
        out = self.manager.tools.execute("add_event", {"title": "Wash the van", "start": "5:00 PM",
                                                       "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def assertNoNote(self, out):
        for mark in NOTE_MARKS:
            self.assertNotIn(mark, out)

    def checks_sent(self):
        return [str(m.get("content")) for m in self.manager.brain.messages
                if str(m.get("content") or "").startswith("[System check")]


class TestARefusedMove(PlainCase):

    def test_the_false_sentence_becomes_the_plain_one(self):
        self.engine.responses = [
            reply('CALL update_event {"event": "Dentist check-up", "start": "3:00 PM"}'),
            reply("Moved your Dentist check-up to 3:00 PM. Anything else?"),
            reply("Moved your Dentist check-up to 3:00 PM. Anything else?")]
        out = self.manager.send_user_message("can the dentist go later")
        self.assertIn("Dentist check-up is still at 2:00 PM; I couldn't move it.", out)
        self.assertNotIn("Moved your Dentist", out)
        self.assertIn("Anything else?", out)  # the rest of the reply stands
        self.assertNoNote(out)

    def test_sent_back_once_with_the_plain_sentence_and_written_fresh(self):
        true = "Dentist check-up is still at 2:00 PM; I couldn't move it, since it's on your calendar."
        self.engine.responses = [
            reply('CALL update_event {"event": "Dentist check-up", "start": "3:00 PM"}'),
            reply("Moved your Dentist check-up to 3:00 PM."),
            reply(true)]
        out = self.manager.send_user_message("can the dentist go later")
        self.assertEqual(out, true)
        check = " ".join(self.checks_sent())
        self.assertIn("Dentist check-up is still at 2:00 PM; I couldn't move it.", check)
        self.assertIn("never saw", check)


class TestTwoChangesOneRefused(PlainCase):

    def calls(self):
        return reply('CALL update_event {"event": "Wash the van", "start": "6:00 PM"}\n'
                     'CALL update_event {"event": "Dentist check-up", "start": "3:00 PM"}')

    def test_only_the_refused_one_is_named(self):
        claim = "I moved Wash the van to 6:00 PM. Dentist check-up is now at 3:00 PM."
        self.engine.responses = [self.calls(), reply(claim), reply(claim)]
        out = self.manager.send_user_message("van at 6 and can the dentist go later")
        self.assertIn("I moved Wash the van to 6:00 PM.", out)
        self.assertIn("Dentist check-up is still at 2:00 PM; I couldn't move it.", out)
        self.assertNotIn("Wash the van is still", out)
        self.assertNotIn("now at 3:00", out)
        self.assertNoNote(out)

    def test_in_one_sentence_the_true_part_stays(self):
        claim = "I moved Wash the van to 6:00 PM and the Dentist check-up to 3:00 PM."
        self.engine.responses = [self.calls(), reply(claim), reply(claim)]
        out = self.manager.send_user_message("van at 6 and can the dentist go later")
        self.assertIn("I moved Wash the van to 6:00 PM", out)
        self.assertIn("Dentist check-up is still at 2:00 PM; I couldn't move it.", out)
        self.assertNotIn("check-up to 3:00", out)
        self.assertNoNote(out)


class TestARefusedRemoval(PlainCase):

    def test_said_in_place_of_the_claim(self):
        self.engine.responses = [
            reply('CALL remove_event {"event": "Dentist check-up"}'),
            reply("Removed the Dentist check-up."), reply("Removed the Dentist check-up.")]
        out = self.manager.send_user_message("take the dentist off")
        self.assertIn("Dentist check-up is still on the plan at 2:00 PM; I couldn't take it off.", out)
        self.assertNotIn("Removed the Dentist", out)
        self.assertNoNote(out)


class WithBookkeeping:
    def setUp(self):
        super().setUp()
        self.manager.scheduler.config.assistant.turn_bookkeeping = True


class TestARefusedMoveWithBookkeeping(WithBookkeeping, TestARefusedMove):
    pass


class TestTwoChangesOneRefusedWithBookkeeping(WithBookkeeping, TestTwoChangesOneRefused):
    pass


class TestARefusedRemovalWithBookkeeping(WithBookkeeping, TestARefusedRemoval):
    pass


if __name__ == "__main__":
    import unittest
    unittest.main()
