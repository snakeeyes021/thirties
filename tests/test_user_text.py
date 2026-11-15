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
                  "thirties_core/day_lists.py", "thirties_core/conversation.py", "thirties_core/routine_profile.py"]
UI = ["src/*.ui"]

HISTORY = (r"no longer|used to|anymore|any more|previously|built in|old meaning|"
           r"now (?:only|built|does|works|uses|means)|\d+\.\d+\.\d+")
INTERNAL = r"horizons?|rollovers?|roll(?:s|ed|ing)? over|pool|back-?off|intents?|brains?|frontier|slm|spans?"
BANNED = re.compile(rf"\b(?:{HISTORY}|{INTERNAL})\b", re.IGNORECASE)


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


if __name__ == "__main__":
    unittest.main()
