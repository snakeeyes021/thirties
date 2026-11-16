"""What the user reads never explains how Thirties used to work, or changes in how it works, and never uses
Thirties' own internal words. Text is in the user's terms, in the present tense; describing changes is the release
notes' job, so they're the one exception (data/*.metainfo.xml.in is never scanned).

Scanned: every string in the app's own windows (src/: labels, rows, toasts, dialogs, menus) and in the core modules
that write to the user directly (notices, Telegram, Before today, the schedule as Python lays it out). Prompts and
tool results for the model aren't the user's to read and aren't scanned."""

import ast
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APP = ["src/*.py", "src/views/*.py"]
CORE_USER_TEXT = ["thirties_core/messages.py", "thirties_core/background.py", "thirties_core/telegram_bridge.py",
                  "thirties_core/before_today.py", "thirties_core/if_time.py", "thirties_core/contextual.py",
                  "thirties_core/sun_notices.py", "thirties_core/morning.py", "thirties_core/not_yours.py",
                  "thirties_core/day_lists.py", "thirties_core/conversation.py", "thirties_core/routine_profile.py",
                  "thirties_core/whats_next.py", "thirties_core/project_pieces.py", "thirties_core/media_sources.py",
                  "thirties_core/period_summary.py",
                  "thirties_core/engineer_notes.py", "thirties_core/media_picks.py",
                  "thirties_core/people.py", "thirties_core/progress.py", "thirties_core/sky.py",
                  "thirties_core/tickets.py", "thirties_core/task_style.py",
                  "thirties_core/break_down.py", "thirties_core/backlog_tickets.py",
                  "thirties_core/day_actions.py", "thirties_core/conversation_index.py",
                  "thirties_core/work_end_answer.py", "thirties_core/fallback.py"]
UI = ["src/*.ui"]

HISTORY = (r"no longer|used to|anymore|any more|previously|built in|old meaning|"
           r"now (?:only|built|does|works|uses|means)|\d+\.\d+\.\d+")
INTERNAL = r"horizons?|rollovers?|roll(?:s|ed|ing)? over|pool|back-?off|intents?|brains?|frontier|slm|spans?"
# Development discussion: what's planned or missing. Honest labels for real developer features ("Developer
# options", "debug mode") are fine: they name things that exist.
DISCUSSION = r"can be added later|coming soon|not implemented|not supported yet"
BANNED = re.compile(rf"\b(?:{HISTORY}|{INTERNAL}|{DISCUSSION})\b", re.IGNORECASE)
# Internal ids and class names never reach a reply (a label may say "pipeline"; an id on its own may not)
INTERNAL_IDS = re.compile(r"(?<![\w-])(?:frontier|slm)(?![\w-])|\b[A-Z][a-z]+(?:Engine|Brain)\b")
# What the model is fed and never says: a tool's result as the prompt wraps it, the checks Python sends it, a
# refusal, and a tool call written out ("CALL add_event {...}", "show_schedule {}")
TOOL_TEXT = re.compile(r"\(\s*Result of\b|for the assistant only|\[System check|\bNOT_DONE\b|\b(?-i:CALL)\s+[a-z]+_\w*|"
                       r"(?m:^\W*[a-z]+_[a-z_]+\s*(?:\(\s*)?\{)|\bid=[0-9a-f]{6,}", re.IGNORECASE)


def _is_text(value: str) -> bool:
    """Words for a person: at least two words, and not markup or a query."""
    v = value.strip()
    return " " in v and not v.startswith("<") and not re.match(r"(SELECT|INSERT|UPDATE|CREATE|DELETE)\b", v)


def _strings(path: Path):
    """(line, text) for each string literal that isn't a docstring or a log line."""
    tree = ast.parse(path.read_text())
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            skip.add(id(node.body[0].value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in ("logger", "logging"):
            skip.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, node.value


def leaks(patterns, ui=()):
    found = []
    for pattern in patterns:
        for path in sorted(ROOT.glob(pattern)):
            for line, text in _strings(path):
                plain = re.sub(r"`?/\w+`?", "", text)  # commands keep their own names
                if _is_text(plain) and BANNED.search(plain):
                    found.append(f"{path.relative_to(ROOT)}:{line}: {text[:100]!r}")
    for pattern in ui:
        for path in sorted(ROOT.glob(pattern)):
            for node in ET.parse(path).iter():
                if node.get("translatable") == "yes" and node.text and BANNED.search(node.text):
                    found.append(f"{path.relative_to(ROOT)}: {node.text!r}")
    return found


class TestUserText(unittest.TestCase):

    def test_the_app_never_explains_its_past_or_uses_internal_words(self):
        self.assertEqual(leaks(APP, UI), [])

    def test_nor_does_what_python_writes_to_the_user(self):
        self.assertEqual(leaks(CORE_USER_TEXT), [])

    def test_the_scan_catches_a_leak(self):
        self.assertTrue(BANNED.search("A drop now only stops an item rolling over."))
        self.assertTrue(BANNED.search("Now built in, remove?"))
        self.assertTrue(BANNED.search("Thirties no longer does that"))
        self.assertFalse(BANNED.search("Put any of these back on their lists?"))
        self.assertFalse(BANNED.search("What's happening now, and time until sleep"))  # "now" itself is fine
        self.assertFalse(BANNED.search("Gemini (cloud, debug mode)"))  # an honest label for a real mode
        self.assertTrue(BANNED.search("Reminders are coming soon"))
        self.assertTrue(INTERNAL_IDS.search("Inference Backend: CliEngine"))
        self.assertTrue(TOOL_TEXT.search("(Result of show_schedule, for the assistant only: You're in the 9:00 AM"))
        self.assertTrue(TOOL_TEXT.search("Here you go.\nupdate_event {\"event\": \"a1\"}"))
        self.assertTrue(TOOL_TEXT.search("CALL add_event {\"title\": \"Walk\"}"))
        self.assertFalse(TOOL_TEXT.search("Call the landlord about the result of the inspection."))
        self.assertFalse(TOOL_TEXT.search("Pressing today:\nCall the plumber\nCall the library about the fine"))
        self.assertFalse(TOOL_TEXT.search("The ferns are at 5:00 PM, and the rest stays (until 6:00 PM)."))


class TestWhatTheUserSeesAtRunTime(unittest.TestCase):
    """Text put together while the app runs: the names and reasons filled into it, and two replies as sent."""

    def test_the_values_filled_into_user_text(self):
        from thirties_core.brains import BRAIN_WORDS, BRAINS
        from thirties_core.memory_audit import BUILT_IN
        for text in list(BRAINS.values()) + [why for why, _ in BUILT_IN]:
            self.assertFalse(BANNED.search(text) or INTERNAL_IDS.search(text), text)
        self.assertFalse(any(INTERNAL_IDS.search(w) for w in BRAIN_WORDS.values()))

    def test_brain_replies_name_what_the_user_types(self):
        from tests.fixtures import DAY, at, make_scheduler
        from thirties_core.astronomy import set_debug_time
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        m = ConversationManager(plan, [], pending, sched, inference_engine=MockInferenceEngine(), active_notes=[])
        for said in ("/brain", "/brain gpt"):
            reply = m.send_user_message(said)
            plain = re.sub(r"`?/\w+`?", "", reply)
            self.assertFalse(INTERNAL_IDS.search(plain), reply)  # internal ids only
        self.assertIn("Choose one of: gemini", m.send_user_message("/brain gpt"))

    def test_a_reply_never_echoes_what_the_model_was_fed(self):
        """The prompt hands the model each tool's result as "(Result of …, for the assistant only: …)". A reply that
        copies one back (all of it, across paragraphs, or cut off), or writes out a call or a check, reaches the
        user without it, with every turn of the model repeating it and with the turn bookkeeping on or off."""
        from tests.fixtures import DAY, at, make_scheduler
        from tests.test_conversation import reply
        from thirties_core.astronomy import set_debug_time
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.models import EventSpan, SpanCategory
        set_debug_time(at("09:10"))
        self.addCleanup(set_debug_time, None)
        echoed = ("(Result of show_schedule, for the assistant only: You're in the 9:00 AM block: Work.\n\n"
                  "5:00 PM – Water the ferns (until 5:30 PM)\n\nOn today's list, no time yet:\nSort the recycling)\n\n"
                  "The ferns are at 5:00 PM.")
        cut_off = ("The ferns are at 5:00 PM.\n\n(Result of list_schedule, for the assistant only: Scheduled:\n"
                   "5:00 PM – 5:30 PM: Water the ferns")
        written_out = ("The ferns are at 5:00 PM.\nshow_schedule {}\n"
                       "[System check, not from the user] Your reply was fine.")
        for bookkeeping in (False, True):
            for said in (echoed, cut_off, written_out):
                sched = make_scheduler()
                sched.config.assistant.turn_bookkeeping = bookkeeping
                sched.state_db.upsert_span(EventSpan("ferns", "Water the ferns", at("17:00"), at("17:30"),
                                                     SpanCategory.ACTIVITY))
                plan, pending = sched.build_day_plan(DAY)
                engine = MockInferenceEngine()
                m = ConversationManager(plan, [], pending, sched, inference_engine=engine, active_notes=[])
                engine.set_next_response(reply("CALL show_schedule {}"))
                for _ in range(8):
                    engine.set_next_response({"role": "assistant", "content": said, "tool_calls": []})
                out = m.send_user_message("when are the ferns?")
                where = f"bookkeeping={bookkeeping}:\n{out}"
                self.assertFalse(TOOL_TEXT.search(out), where)
                self.assertIn("The ferns are at 5:00 PM", out, where)
                self.assertNotIn("Sort the recycling", out, where)  # the echo's own lines go with it
                self.assertNotIn("5:30 PM: Water the ferns", out, where)

    def test_telegram_status_as_sent(self):
        import tempfile
        from unittest.mock import MagicMock, patch
        from tests.fixtures import at
        from thirties_core.astronomy import set_debug_time
        from thirties_core.calendar_engine import MockCalendarEngine
        from thirties_core.config import TelegramConfig, ThirtiesConfig
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.scheduler import DeterministicScheduler, StateDatabase
        from thirties_core.telegram_bridge import TelegramClient, ThirtiesTelegramBridge
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = ThirtiesConfig(telegram=TelegramConfig(enabled=True, bot_token="FAKE", allowed_chat_ids=[7]))
        sched = DeterministicScheduler(config=config, state_db=StateDatabase(Path(tmp.name) / "s.sqlite"),
                                       calendar_engine=MockCalendarEngine([]))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        with patch.object(TelegramClient, "get_me", return_value={"username": "bot"}):
            bridge = ThirtiesTelegramBridge(config=config, scheduler=sched, joplin_engine=joplin,
                                            inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        bridge.client = MagicMock()
        bridge.handle_message(7, "/status")
        sent = bridge.client.send_message.call_args[0][1]
        self.assertIn("Gemini (cloud, for developers)", sent)
        self.assertFalse(INTERNAL_IDS.search(sent), sent)  # internal ids and class names only

    def test_what_software_centres_show(self):
        root = ET.parse(ROOT / "data/tech.redfoxlabs.Thirties.metainfo.xml.in").getroot()
        for node in root.iter():
            if node.tag in ("p", "li", "summary", "name") and node.text:
                self.assertFalse(re.search(rf"\b(?:{DISCUSSION})\b", node.text, re.IGNORECASE), node.text)


if __name__ == "__main__":
    unittest.main()
