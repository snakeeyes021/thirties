"""The sensitive-data filter at the exit to any cloud model (privacy.py). All values here are invented; the
card and IBAN numbers are the published test numbers."""

import unittest
from unittest.mock import patch

from thirties_core.privacy import PRIVATE_BODY, Shield, iban_ok, is_private_note, luhn_ok


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.s = Shield()

    def test_labelled_password_is_replaced_and_restored(self):
        out = self.s.redact("wifi password: Plum-Kite-42 for the guest network")
        self.assertNotIn("Plum-Kite-42", out)
        self.assertIn("password: [password]", out)
        self.assertEqual(self.s.restore("Sure: [password]."), "Sure: Plum-Kite-42.")

    def test_password_said_in_a_sentence(self):
        out = self.s.redact("my password is zebra77! remember it")
        self.assertNotIn("zebra77", out)
        self.assertIn("[password]", out)

    def test_ordinary_sentence_about_a_password_is_left_alone(self):
        text = "the password is on the fridge"
        self.assertEqual(self.s.redact(text), text)

    def test_pin_and_security_code(self):
        out = self.s.redact("PIN: 4821, CVV: 913")
        self.assertNotIn("4821", out)
        self.assertNotIn("913", out)

    def test_backup_codes_all_go(self):
        out = self.s.redact("backup codes: 8812-1290 7713-0042, 5521-9083 then file them")
        for code in ("8812-1290", "7713-0042", "5521-9083"):
            self.assertNotIn(code, out)
        self.assertIn("then file them", out)

    def test_key_shaped_strings(self):
        key = "AIza" + "Q" * 35
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        out = self.s.redact(f"key {key} and {jwt} and sk-proj-abcdefghijklmnopqrstuvwx")
        self.assertNotIn(key, out)
        self.assertNotIn(jwt, out)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwx", out)

    def test_private_key_block(self):
        block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
        self.assertNotIn("b3BlbnNzaC1rZXk", self.s.redact(f"here:\n{block}\nend"))

    def test_links_with_a_password_or_token(self):
        out = self.s.redact("https://sam:hunter22@files.example.com/x and https://example.com/a?token=abc123&page=2")
        self.assertNotIn("hunter22", out)
        self.assertNotIn("abc123", out)
        self.assertIn("files.example.com", out)
        self.assertIn("page=2", out)


class GarbledPlaceholdersTest(unittest.TestCase):
    """A placeholder the model wrote a little differently was never put back, and could be written into the
    user's lists in place of the real value."""

    def test_a_placeholder_written_a_little_differently_is_still_put_back(self):
        s = Shield()
        s.redact("Shed lock password: Gr33nG@te and card 4111 1111 1111 1111")
        self.assertEqual(s.restore("[Password] (new lock)"), "Gr33nG@te (new lock)")
        self.assertEqual(s.restore("pay with [card..1111]"), "pay with 4111 1111 1111 1111")
        self.assertEqual(s.restore("pay with [CARD ..1111"), "pay with 4111 1111 1111 1111")
        self.assertEqual(s.restore("all [passwords] reset"), "all [passwords] reset")  # another word

    def test_a_change_still_holding_a_placeholder_is_never_written_to_the_lists(self):
        from tests.test_joplin_safety import ERRANDS, VersionedJoplin, writer_for
        from thirties_core.joplin_writeback import current_turn
        from thirties_core.privacy import leftover_placeholder
        self.assertEqual(leftover_placeholder({"title": "Reset the [Password ] on the router"}), "[Password ]")
        self.assertIsNone(leftover_placeholder({"title": "Plan the [Kestrel] review", "lines": ["[ID: 1]"]}))
        api = VersionedJoplin({"n1": {"id": "n1", "title": "Errands", "body": ERRANDS}})
        writer, _ = writer_for(api)
        token = current_turn.set("rename the router task")
        try:
            state, msg = writer.submit({"op": "add_task", "note_id": "n1", "title": "Reset the [password] on it"})
        finally:
            current_turn.reset(token)
        self.assertEqual(state, "failed")
        self.assertIn("ask the user", msg)
        self.assertEqual(api.notes["n1"]["body"], ERRANDS)  # nothing written


class NumbersTest(unittest.TestCase):
    def setUp(self):
        self.s = Shield()

    def test_card_number_passing_luhn(self):
        out = self.s.redact("card 4111 1111 1111 1111 expires soon")
        self.assertNotIn("4111 1111 1111 1111", out)
        self.assertIn("[card ..1111]", out)
        self.assertEqual(self.s.restore("[card ..1111]"), "4111 1111 1111 1111")

    def test_long_number_failing_luhn_stays(self):
        self.assertIn("4111 1111 1111 1112", self.s.redact("order 4111 1111 1111 1112"))

    def test_ids_and_dates_are_not_cards(self):
        text = "[ID: 3fa85f6457174562] on 2026-11-11 at 08:30, call 555-0100, order #48213"
        self.assertEqual(self.s.redact(text), text)

    def test_iban(self):
        self.assertTrue(iban_ok("GB82 WEST 1234 5698 7654 32"))
        out = self.s.redact("pay to GB82 WEST 1234 5698 7654 32 today")
        self.assertNotIn("WEST 1234", out)
        self.assertIn("[IBAN ..5432]", out)

    def test_bank_account_and_routing(self):
        out = self.s.redact("account number: 000123456789, routing: 011000015")
        self.assertNotIn("000123456789", out)
        self.assertNotIn("011000015", out)
        self.assertIn("[account ..6789]", out)

    def test_ssn(self):
        out = self.s.redact("SSN 123-45-6789 for the form")
        self.assertNotIn("123-45-6789", out)
        self.assertIn("[SSN]", out)

    def test_labelled_passport_and_licence(self):
        out = self.s.redact("passport no: X1234567, driver's license #: D123-4567-8901")
        self.assertNotIn("X1234567", out)
        self.assertNotIn("D123-4567-8901", out)

    def test_luhn(self):
        self.assertTrue(luhn_ok("4111111111111111"))
        self.assertFalse(luhn_ok("4111111111111112"))


class MedicalTest(unittest.TestCase):
    def setUp(self):
        self.s = Shield()

    def test_medication_names_become_their_category(self):
        out = self.s.redact("Refill sertraline at the pharmacy; ask about lisinopril")
        self.assertNotIn("sertraline", out)
        self.assertNotIn("lisinopril", out)
        self.assertIn("Refill [medication] at the pharmacy", out)

    def test_diagnoses_become_their_category(self):
        out = self.s.redact("Book the ADHD follow-up; diabetes check in March")
        self.assertNotIn("ADHD", out)
        self.assertNotIn("diabetes", out)
        self.assertIn("[condition]", out)

    def test_round_trip_through_a_reply_and_a_tool_call(self):
        sent = self.s.redact_messages([{"role": "user", "content": "remind me to take the metformin at 8"}])
        self.assertNotIn("metformin", sent[0]["content"])
        reply = self.s.restore_reply({"role": "assistant", "content": "Done: [medication] at 8.",
                                      "tool_calls": [{"name": "add_item",
                                                      "arguments": {"title": "Take [medication]", "start": "8:00 PM"}}]})
        self.assertEqual(reply["content"], "Done: metformin at 8.")
        self.assertEqual(reply["tool_calls"][0]["arguments"]["title"], "Take metformin")

    def test_ordinary_words_are_left_alone(self):
        text = ("Meal prep, April taxes, hearing aids, pin the reading list, Secret Santa gift, "
                "physical therapy stretches, done by noon")
        self.assertEqual(self.s.redact(text), text)


class PrivateNotesTest(unittest.TestCase):
    NOTES = [
        {"id": "a1", "source_notebook": "Life > Health", "title": "Appointments", "tags": [],
         "body": "Dr. Okafor's office is on the third floor, suite 310\n"
                 "- [ ] **Book the dentist** (cleaning overdue since the spring, use the new insurance card)\n"
                 "- [ ] Buy vitamins\n"},
        {"id": "b2", "source_notebook": "Projects", "title": "Garden", "tags": ["private"],
         "body": "The spare key sits under the blue flowerpot by the door"},
        {"id": "c3", "source_notebook": "Projects", "title": "Shed", "tags": [],
         "body": "Paint the shed before the first frost arrives"},
    ]

    def test_which_notes_are_private(self):
        self.assertEqual([is_private_note(n) for n in self.NOTES], [True, True, False])
        self.assertTrue(is_private_note({"source_notebook": "Money", "title": "Finances 2026"}))

    def test_private_bodies_never_go_and_items_go_as_titles(self):
        self.assertEqual(self.s.set_private_notes(self.NOTES), 2)
        text = ("Contents:\n    Dr. Okafor's office is on the third floor, suite 310\n"
                "- [ID: 1] '**Book the dentist** (cleaning overdue since the spring, use the new insurance card)'\n"
                "Contents:\n    The spare key sits under the blue flowerpot by the door\n"
                "Contents:\n    Paint the shed before the first frost arrives")
        out = self.s.redact(text)
        self.assertNotIn("third floor", out)
        self.assertNotIn("flowerpot", out)
        self.assertNotIn("insurance card", out)
        self.assertIn("'Book the dentist'", out)
        self.assertIn(PRIVATE_BODY, out)
        self.assertIn("Paint the shed before the first frost arrives", out)  # not private

    def test_what_was_private_stays_private_after_it_leaves_its_note(self):
        """Filed as done into a note that isn't private, a long private line was sent in full from then on."""
        line = "**Book the dentist** (cleaning overdue since the spring, use the new insurance card)"
        self.s.set_private_notes(self.NOTES)
        moved = [dict(self.NOTES[0], body="Buy vitamins\n"), self.NOTES[2],
                 {"id": "d4", "source_notebook": "Thirties", "title": "Done 2026-10", "tags": [],
                  "body": f"- [x] 2026-10-14: {line}\n"}]
        self.s.set_private_notes(moved)  # the next read: the line is in a Done note now
        out = self.s.redact(f"Earlier you said: {line}")
        self.assertNotIn("insurance card", out)
        self.assertIn("Book the dentist", out)

    def test_it_stays_private_across_a_restart(self):
        from tests.fixtures import make_scheduler
        from thirties_core.backlog import remember_private
        sched = make_scheduler()
        remember_private(sched, self.NOTES, self.s)
        later = Shield()  # the app started again; the note was unmarked meanwhile
        remember_private(sched, [dict(self.NOTES[1], tags=[]), self.NOTES[2]], later)
        self.assertNotIn("flowerpot", later.redact("The spare key sits under the blue flowerpot by the door"))

    def setUp(self):
        self.s = Shield()


class PrivateNotesFromJoplinTest(unittest.TestCase):
    """Reading the lists tells the filter which notes are private, and the prompt leaves their bodies out."""

    def test_joplin_tags_are_read_and_mark_a_note_private(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        from tests.test_joplin_engine import create_mock_joplin_db
        from thirties_core.config import ThirtiesConfig
        from thirties_core.joplin_engine import JoplinEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "joplin.sqlite"
            create_mock_joplin_db(path)
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE tags (id TEXT PRIMARY KEY, title TEXT)")
                conn.execute("CREATE TABLE note_tags (id TEXT PRIMARY KEY, note_id TEXT, tag_id TEXT)")
                conn.execute("INSERT INTO tags VALUES ('t1', 'private')")
                conn.execute("INSERT INTO note_tags VALUES ('nt1', 'note_2', 't1')")
            cfg = ThirtiesConfig()
            cfg.joplin.db_path = str(path)
            notes = {n["id"]: n for n in JoplinEngine(cfg).fetch_active_notes(path)}
        self.assertEqual(notes["note_2"]["tags"], ["private"])
        self.assertTrue(is_private_note(notes["note_2"]))
        self.assertFalse(is_private_note(notes["note_1"]))

    def test_a_profile_without_tags_still_reads(self):
        import tempfile
        from pathlib import Path
        from tests.test_joplin_engine import create_mock_joplin_db
        from thirties_core.config import ThirtiesConfig
        from thirties_core.joplin_engine import JoplinEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "joplin.sqlite"
            create_mock_joplin_db(path)
            cfg = ThirtiesConfig()
            cfg.joplin.db_path = str(path)
            notes = JoplinEngine(cfg).fetch_active_notes(path)
        self.assertTrue(notes)
        self.assertTrue(all(n["tags"] == [] for n in notes))

    def test_loading_the_backlog_registers_private_notes(self):
        from thirties_core import privacy
        from thirties_core.backlog import load_backlog
        from thirties_core.config import ThirtiesConfig

        class Source:
            def fetch_tasks(self):
                return []

            def fetch_active_notes(self):
                return [{"id": "n1", "source_notebook": "Finance", "title": "Taxes", "tags": [],
                         "body": "The accountant wants the forms by the end of the month"}]

        shield = Shield()
        with patch.object(privacy, "SHIELD", shield):
            load_backlog(ThirtiesConfig(), Source(), media=[])
        self.assertEqual(shield.redact("The accountant wants the forms by the end of the month"), PRIVATE_BODY)

    def test_the_prompt_leaves_private_bodies_out(self):
        from tests.fixtures import build_day
        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.plan_tools import PlanTools
        scheduler, plan, events = build_day()
        notes = [{"id": "n1", "source_notebook": "Life > Health", "title": "Clinic notes", "is_todo": False,
                  "todo_completed": 0, "tags": [], "body": "Ask about the results from the last visit"},
                 {"id": "n2", "source_notebook": "Life", "title": "Garden ideas", "is_todo": False,
                  "todo_completed": 0, "tags": [], "body": "Plant tulips along the fence"}]
        text = FrontierBrain(PlanTools(plan, events, scheduler), None, tasks=[], active_notes=notes).build_backlog_summary()
        self.assertNotIn("results from the last visit", text)
        self.assertIn("Clinic notes", text)
        self.assertIn("Plant tulips along the fence", text)


class InstructionsPassTest(unittest.TestCase):
    def test_the_assistants_own_instructions_go_out_unchanged(self):
        """The system prompt and tool list must never trip the filter (it would garble the instructions)."""
        from tests.fixtures import build_day
        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.plan_tools import PlanTools
        scheduler, plan, events = build_day()
        prompt = FrontierBrain(PlanTools(plan, events, scheduler), None, tasks=[], active_notes=[]).build_system_prompt()
        counts = {}
        out = Shield().redact(prompt, counts)
        self.assertEqual(counts, {})
        self.assertEqual(out, prompt)


class ExitTest(unittest.TestCase):
    """Both cloud transports send only filtered text, and act on the restored reply."""

    def test_agy_sends_filtered_prompt_and_restores_the_calls(self):
        import json
        import subprocess
        from thirties_core import privacy
        from thirties_core.config import ThirtiesConfig
        from thirties_core.inference import AntigravityInferenceEngine
        sent = {}

        def fake_run(cmd, input=None, **kwargs):
            sent["stdin"] = input
            body = json.dumps({"type": "result", "status": "SUCCESS",
                               "response": 'CALL add_item {"title": "Pick up [medication]"}\nAdded [medication].'})
            return subprocess.CompletedProcess(cmd, 0, stdout=body + "\n", stderr="")

        shield = Shield()
        with patch.object(privacy, "SHIELD", shield), \
                patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("thirties_core.inference.subprocess.run", side_effect=fake_run):
            reply = AntigravityInferenceEngine(ThirtiesConfig()).chat(
                [{"role": "system", "content": "Plan the day."},
                 {"role": "user", "content": "add pick up escitalopram, card 4111 1111 1111 1111"}])
        self.assertNotIn("escitalopram", sent["stdin"])
        self.assertNotIn("4111 1111 1111 1111", sent["stdin"])
        self.assertEqual(reply["content"], "Added escitalopram.")
        self.assertEqual(reply["tool_calls"][0]["arguments"]["title"], "Pick up escitalopram")

    def test_gemini_api_sends_filtered_payload(self):
        import io
        import json
        from thirties_core import privacy
        from thirties_core.config import ThirtiesConfig
        from thirties_core.inference import GeminiInferenceEngine
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "test-key"
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["data"] = req.data.decode()
            return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "Noted: [password]"}]}}]}).encode())

        with patch.object(privacy, "SHIELD", Shield()), \
                patch("thirties_core.inference.urllib.request.urlopen", side_effect=fake_urlopen):
            reply = GeminiInferenceEngine(cfg).chat([{"role": "user", "content": "router password: Tangerine-9"}])
        self.assertNotIn("Tangerine-9", seen["data"])
        self.assertEqual(reply["content"], "Noted: Tangerine-9")


if __name__ == "__main__":
    unittest.main()
