"""Real-data self-check: does anything added since a commit come from the user's own data?

Test, demo and example data is always invented (AGENTS.md, anti-pattern 6). Before asking for a review, run:

    python3 -I tests/realdata_check.py BASE        # e.g. origin/feature/0.2

It reads, read-only, what this machine's Thirties and Joplin hold (found the way the app finds them: the
config's paths, never a path written here): the user's own messages, their items and notes. It takes every two
to four word phrase added since BASE (code, tests, comments and commit messages) that isn't already in the
repository at BASE, and counts the ones that also occur in that data.

A match is "strong" when the phrase has a word the repository never used before (real names and items look
like that), else "common" (everyday wording that also turns up in anyone's notes: read the line to be sure).

It prints counts and the places in the diff only (word positions in our own line), never the matching text, so
nothing private reaches a log. Exit status 1 when anything matched.

It also counts review labels in what was added: a capital letter followed by one or two digits, or a decision
id (a capital D, a dash and digits). Those belong to private reviews; the public repository describes the
behaviour instead. Quarter names and copy-list ids (a label followed by a dash and digits) don't count. The labels
are looked for in this file too, so its own examples stay in words.

No check catches a paraphrase: fixtures are written from scratch, never adapted from examples in plans, reviews
or transcripts, which describe the user's real days.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
SELF = "tests/realdata_check.py"
sys.path.insert(0, str(REPO))

STOP = set("""a an and are as at be been but by can did do does done for from had has have he her his i if in into is
it its just me my no not now of on or our out she so than that the their them then there these they this to too up
us was we were what when where which who will with you your it's i'm don't can't won't let get got go going one two
three all any each more some very also only about after before over back off""".split())
WORD = re.compile(r"[a-z][a-z'\-]+")


def words(text: str) -> List[str]:
    return WORD.findall(text.lower())


LABEL = re.compile(r"\b[A-HJ-PR-Z][0-9]{1,2}\b(?!-[0-9])|\bD-[0-9]{2,3}\b")  # review labels (not Q1-Q4, not I/O)
BREAK = " | "  # between separate pieces of text on one line (two strings): no phrase runs across it


def phrases(text: str) -> Set[str]:
    """Two to four word runs with at least two content words (not only common words), within each piece."""
    if BREAK in text:
        return set().union(*(phrases(piece) for piece in text.split(BREAK)))
    w = words(text)
    out = set()
    for n in (2, 3, 4):
        for i in range(len(w) - n + 1):
            run = w[i:i + n]
            content = [x for x in run if x not in STOP and len(x) >= 3]
            if len(content) >= 2 and run[0] not in STOP and run[-1] not in STOP:
                out.add(" ".join(run))
    return out


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout


_CODE = re.compile(r"[()=\[\]{}]|^\s*(def|class|return|import|from|if|elif|else|for|while|with|try|except)\b")
_STRING = re.compile(r"[rbfu]*(\"\"\"|'\'\'|\"|')(.*?)\1")


def prose(line: str) -> str:
    """The words in a line of Python that are text, not code: its comment, its strings, or the whole line when
    it's a docstring line with no code in it. Names in code are never examples."""
    code, _, comment = line.partition("  #") if "  #" in line else (line, "", "")
    if code.lstrip().startswith("#"):
        return code.lstrip()[1:]
    if not _CODE.search(code) and not _STRING.search(code):
        return line  # a docstring's or a long string's continuation line
    return BREAK.join([m.group(2) for m in _STRING.finditer(code)] + [comment])  # never a phrase across two


def added_lines(base: str) -> List[Tuple[str, int, str]]:
    """(file, line, text) for every line added since ``base``, plus each commit message line."""
    out: List[Tuple[str, int, str]] = []
    path, line = "", 0
    for raw in git("diff", "--unified=0", f"{base}..HEAD").splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            text = prose(raw[1:]) if path.endswith(".py") else raw[1:]
            if text:  # this checker's own code is read for labels only (below), never for phrases
                out.append((path, line, text))
            line += 1
    for sha in git("rev-list", f"{base}..HEAD").split():
        for k, text in enumerate(git("log", "-1", "--format=%B", sha).splitlines(), 1):
            out.append((f"commit {sha[:7]} message", k, text))
    return out


def vocabulary(text: str) -> Set[str]:
    return set(text.split())


def baseline(base: str) -> str:
    """Everything already in the repository at ``base`` (published and reviewed before)."""
    texts = []
    for path in git("ls-tree", "-r", "--name-only", base).splitlines():
        if path.endswith((".py", ".md", ".json", ".toml", ".ui", ".in", ".txt")):
            try:
                texts.append(git("show", f"{base}:{path}"))
            except subprocess.CalledProcessError:
                pass
    return " " + " ".join(words(" ".join(texts))) + " "


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def user_data() -> str:
    """The user's own words and items, as one lowercase run of words."""
    from thirties_core.config import load_config
    cfg = load_config()
    texts: List[str] = []
    state = cfg.resolve_state_db_path()
    if state.is_file():
        con = _ro(state)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def column(table: str, *cols: str) -> Iterable[str]:
            if table not in tables:
                return []
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            picked = [c for c in cols if c in have]
            if not picked:
                return []
            return [str(v) for row in con.execute(f"SELECT {', '.join(picked)} FROM {table}") for v in row if v]
        for row in column("conversations", "messages"):  # the user's own messages, not the app's replies
            try:
                texts += [str(m.get("content") or "") for m in json.loads(row) if m.get("role") == "user"]
            except (ValueError, AttributeError):
                pass
        texts += column("plan_changes", "message", "title")
        for row in column("spans", "data"):
            try:
                texts.append(str(json.loads(row).get("title") or ""))
            except ValueError:
                pass
        for table, cols in (("item_facts", ("name", "title")), ("item_urgency", ("title",)),
                            ("horizon_plan", ("title", "list_title")), ("horizon_marks", ("title",)),
                            ("list_roles", ("title",)), ("routine_plans", ("title",)), ("daily_anchors", ("title",)),
                            ("today_links", ("today_title", "home_title")), ("day_list_ahead", ("title", "home_title")),
                            ("done_filed", ("title", "source_title")), ("memory_notes", ("text",)),
                            ("profile_sections", ("content",)), ("reminders", ("text",)), ("energy_log", ("note",)),
                            ("outcome_context", ("title",))):
            texts += column(table, *cols)
        con.close()
    joplin = cfg.resolve_joplin_db_path()
    if joplin is not None and joplin.is_file():
        con = _ro(joplin)
        texts += [f"{t or ''} {b or ''}" for t, b in con.execute("SELECT title, body FROM notes")]
        con.close()
    return " " + " ".join(words(" ".join(texts))) + " "


def main(argv: List[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/feature/0.2"
    added = added_lines(base)
    known = baseline(base)
    data = user_data()
    if len(data.split()) < 50:
        print("real-data self-check: no user data found on this machine (nothing checked)")
        return 2
    vocab = vocabulary(known)
    hits: Dict[Tuple[str, int], List[str]] = {}
    strong = 0
    for path, line, text in added:
        w = words(text)
        for p in (phrases(text) if path != SELF else ()):
            if f" {p} " in data and f" {p} " not in known:
                n = len(p.split())  # where in the line, as word positions: the line is ours to read, the data isn't
                at = next((i for i in range(len(w) - n + 1) if " ".join(w[i:i + n]) == p), 0)
                new_word = any(x not in vocab for x in p.split())
                strong += new_word
                hits.setdefault((path, line), []).append(f"words {at + 1}-{at + n}{' STRONG' if new_word else ''}")
    labels = [(path, line) for path, line, text in added if LABEL.search(text)]
    total = sum(len(v) for v in hits.values())
    print(f"real-data self-check: {total} hits ({strong} strong, {total - strong} common) in {len(hits)} added "
          f"lines (since {base}); review labels: {len(labels)}")
    for path, line in labels:
        print(f"  label at {path}:{line}")
    for (path, line), where in sorted(hits.items()):
        print(f"  {path}:{line}  ({', '.join(sorted(where))})")
    return 1 if total or labels else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
