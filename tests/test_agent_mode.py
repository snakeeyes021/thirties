"""Agent mode (thirties_core/agent_mode/): the developer switch that hands each turn to a full Gemini run.

No model and no agy is ever run here: the agy call is a stand-in runner, and the quota readings are given.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, FakeJoplinAPI, at, event, make_scheduler, temp_dir
from thirties_core import agent_mode
from thirties_core.agent_mode import guard, route
from thirties_core.agent_mode.bridge import ToolBridge
from thirties_core.agent_mode.session import (AGENT_DIR_ENV, AGY_DIR, DEFAULT_EFFORT, DEFAULT_MODEL, WORKSPACE_NAME,
                                              AgentSession, TurnResult, agent_dir, build_command, model_name,
                                              mcp_schema_dir, prepare_workspace, read_output)
from thirties_core.inference import split_thinking_level
from thirties_core.agent_mode.tools import AgentTools, Journal
from thirties_core.agent_mode import tools_server
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.settings import Settings

REPO = Path(__file__).resolve().parent.parent

KILN = ("# Glaze bench\n"
        "- [ ] Mix the celadon batch\n"
        "    - [ ] Sieve it twice\n"
        "- [x] Label the shelf bins\n"
        "## Firing\n"
        "- [ ] Load the bisque kiln\n")
COOP = "- [ ] Return the spoke wrench\n- [ ] Patch the demo tube\n"
OPENING = "- [x] Unlock the tool wall\n- [x] Switch on the dust extractor\n"


def agy_stream(response: str, conversation_id: str = "conv-7f3", status: str = "SUCCESS") -> str:
    """What agy prints in stream-json mode, as its docs describe it."""
    return "\n".join(json.dumps(e) for e in (
        {"event": "init", "conversation_id": conversation_id, "init": {"cwd": "/x", "tools": []}},
        {"event": "step_update", "step_update": {"conversation_id": conversation_id, "step_index": 0,
                                                 "state": "DONE", "step_type": "agent_response"}},
        {"event": "result", "result": {"conversation_id": conversation_id, "status": status,
                                       "response": response, "num_turns": 1}},
    )) + "\n"


class AgentModeCase(unittest.TestCase):
    """A person's lists in a stand-in Joplin, their day in a temp state database, and a temp workspace for Gemini."""

    def setUp(self) -> None:
        set_debug_time(at("11:10"))
        self.addCleanup(set_debug_time, None)
        self.ws = Path(temp_dir("thirties_agent_ws_")) / "workspace"
        env = patch.dict(os.environ, {AGENT_DIR_ENV: str(self.ws)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(agent_mode.AGENT_MODE_ENV, None)
        ledger = patch("thirties_core.inference.AGY_LEDGER", Path(temp_dir("thirties_agent_ledger_")) / "calls.log")
        self.ledger = ledger.start()
        self.addCleanup(ledger.stop)
        self.sched = make_scheduler([event("ev1", "14:35", "15:25", "Kiln vendor call")])
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({
            "n_kiln": {"id": "n_kiln", "title": "Kiln Studio", "body": KILN},
            "n_coop": {"id": "n_coop", "title": "Bike Co-op", "body": COOP},
            "n_open": {"id": "n_open", "title": "Workshop Opening", "body": OPENING},
        })
        self.joplin = MagicMock()
        self.joplin.api_client = self.api
        self.joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": n["title"], "source_notebook": "Projects", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        ListCatalog(self.sched.state_db).set_role("n_open", "routine", "Workshop Opening")
        self.engine = MockInferenceEngine()

    def manager(self) -> ConversationManager:
        plan, pending = self.sched.build_day_plan(DAY)
        return ConversationManager(plan, [], pending, self.sched, joplin_engine=self.joplin,
                                   inference_engine=self.engine, active_notes=[])

    def tools(self) -> AgentTools:
        from thirties_core.joplin_writeback import JoplinWriter
        return AgentTools(self.sched, self.joplin, lambda: JoplinWriter(self.sched.config, self.sched.state_db,
                                                                        self.api),
                          Journal(agent_dir() / "journal.jsonl"), day=lambda: DAY)

    def call(self, tools: AgentTools, name: str, **args) -> dict:
        return json.loads(tools.call(name, args))


# --- the switch ---

class TestTheSwitch(AgentModeCase):

    def test_off_by_default(self):
        self.assertFalse(agent_mode.is_on(self.sched.config, self.sched.state_db))
        self.assertFalse(self.sched.config.assistant.agent_mode)

    def test_on_by_env_config_or_this_devices_setting(self):
        with patch.dict(os.environ, {agent_mode.AGENT_MODE_ENV: "1"}):
            self.assertTrue(agent_mode.is_on(self.sched.config, self.sched.state_db))
        self.sched.config.assistant.agent_mode = True
        self.assertTrue(agent_mode.is_on(self.sched.config, self.sched.state_db))
        Settings(self.sched.state_db).set(agent_mode.SETTING, False)  # the device's switch wins over config
        self.assertFalse(agent_mode.is_on(self.sched.config, self.sched.state_db))
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        self.sched.config.assistant.agent_mode = False
        self.assertTrue(agent_mode.is_on(self.sched.config, self.sched.state_db))

    def test_the_switch_is_offered_only_behind_the_developer_switch(self):
        self.assertFalse(agent_mode.offered(self.sched.config, self.sched.state_db))
        self.sched.config.assistant.offer_gemma = True
        self.assertTrue(agent_mode.offered(self.sched.config, self.sched.state_db))
        self.sched.config.assistant.offer_gemma = False
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)  # on: shown, so it can be turned off
        self.assertTrue(agent_mode.offered(self.sched.config, self.sched.state_db))


# --- routing ---

class TestRouting(AgentModeCase):

    def setUp(self) -> None:
        super().setUp()
        self.runs = []
        go = patch.object(guard, "check", return_value=guard.Verdict(True))
        go.start()
        self.addCleanup(go.stop)

    def fake_run(self, reply="The bisque kiln is on your list for the 13:00 block."):
        def run(session, text, day):
            self.runs.append((text, day))
            return TurnResult(True, reply=reply, conversation_id="conv-1")
        return patch.object(AgentSession, "run", run)

    def test_off_the_usual_assistant_answers(self):
        self.engine.set_next_response({"role": "assistant", "content": "Usual reply.", "tool_calls": []})
        with self.fake_run(), patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            out = self.manager().send_user_message("what's on the glaze bench?")
        self.assertEqual(out, "Usual reply.")
        self.assertEqual(self.runs, [])

    def test_on_the_agent_answers_and_the_usual_assistant_is_never_asked(self):
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        self.engine.chat = MagicMock(side_effect=AssertionError("the usual assistant was asked"))
        m = self.manager()
        with self.fake_run(), patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            out = m.send_user_message("what's on the glaze bench?")
        self.assertEqual(out, "The bisque kiln is on your list for the 13:00 block.")
        self.assertEqual(self.runs, [("what's on the glaze bench?", DAY)])
        shown = m.history()
        self.assertEqual([h["role"] for h in shown], ["user", "assistant"])
        self.assertEqual(shown[1]["content"], out)  # as Gemini wrote it: no checks on the reply
        self.assertEqual(self.manager().history()[-1]["content"], out)  # stored for the other clients

    def test_telegram_follows_the_same_route(self):
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        self.engine.chat = MagicMock(side_effect=AssertionError("the usual assistant was asked"))
        with self.fake_run("Noted."), patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            out = self.manager().send_user_message("patch tube first", via="telegram")
        self.assertEqual(out, "Noted.")
        self.assertEqual(len(self.runs), 1)

    def test_turning_it_off_returns_to_the_usual_assistant(self):
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        m = self.manager()
        with self.fake_run("Agent here."), patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            self.assertEqual(m.send_user_message("first"), "Agent here.")
            Settings(self.sched.state_db).set(agent_mode.SETTING, False)
            self.engine.set_next_response({"role": "assistant", "content": "Usual again.", "tool_calls": []})
            self.assertEqual(m.send_user_message("second"), "Usual again.")
        self.assertEqual(len(self.runs), 1)

    def test_a_failed_agent_run_hands_the_turn_over_and_says_so_once(self):
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        failing = patch.object(AgentSession, "run", lambda s, t, d: TurnResult(False, error="agy failed"))
        m = self.manager()
        with failing, patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            self.engine.set_next_response({"role": "assistant", "content": "Usual reply.", "tool_calls": []})
            first = m.send_user_message("one")
            self.engine.set_next_response({"role": "assistant", "content": "Usual reply two.", "tool_calls": []})
            second = m.send_user_message("two")
        self.assertTrue(first.startswith(route.FAILED_LINE))
        self.assertEqual(second, "Usual reply two.")
        self.assertEqual(m.history()[-1]["content"], "Usual reply two.")
        self.assertTrue(any(h["content"].startswith(route.FAILED_LINE) for h in m.history()))

    def test_its_own_lines_name_no_clock_time(self):
        import re
        for line in (route.QUOTA_LINE, route.FAILED_LINE, route.CHANGED_LINE, route.EMPTY_REPLY):
            self.assertIsNone(re.search(r"\d:\d\d", line), line)

    def test_agent_undo_is_a_slash_command(self):
        tools = self.tools()
        self.call(tools, "tick_line", note="Bike Co-op", line=2)
        out = self.manager().send_user_message(agent_mode.UNDO_COMMAND)
        self.assertIn("Undone", out)
        self.assertEqual(self.api.notes["n_coop"]["body"], COOP)


# --- the guard ---

class TestTheGuard(AgentModeCase):

    def test_below_35_percent_hands_over(self):
        self.assertFalse(guard.check(five_hour=lambda: 0.34).go)
        self.assertTrue(guard.check(five_hour=lambda: 0.35).go)
        self.assertTrue(guard.check(five_hour=lambda: 0.8).go)

    def test_no_reading_lets_the_turn_run(self):
        self.assertTrue(guard.check(five_hour=lambda: None).go)
        self.assertTrue(guard.check().go)

    def test_a_reading_is_reused_for_a_few_minutes(self):
        guard._last.clear()
        asked = []
        with patch.object(guard, "five_hour_from_agy", lambda cmd, cwd: asked.append(1) or 0.6):
            read = guard.reading_from(["agy"], Path("."))
            self.assertEqual((read(), read()), (0.6, 0.6))
        self.assertEqual(len(asked), 1)
        guard._last.clear()

    def test_the_reading_from_agy(self):
        quota = {"command": {"data": {"groups": [
            {"name": "Gemini 4 Argon", "buckets": [{"window": "5h", "remaining_fraction": 0.9}]},
            {"name": "Gemini Models", "buckets": [{"window": "weekly", "remaining_fraction": 0.6},
                                                  {"window": "5h", "remaining_fraction": 0.22}]}]}}}
        self.assertAlmostEqual(guard.parse_agy_quota(json.dumps(quota)), 0.22)
        self.assertIsNone(guard.parse_agy_quota("not json"))

    def test_low_quota_turn_goes_to_the_usual_assistant_with_one_line(self):
        Settings(self.sched.state_db).set(agent_mode.SETTING, True)
        ran = []
        reading = patch.object(guard, "reading_from", return_value=lambda: 0.21)
        m = self.manager()
        with reading, patch.object(AgentSession, "run", lambda *a: ran.append(a)), \
                patch.object(AgentSession, "agy_binary", return_value="/nonexistent/agy"):
            self.engine.set_next_response({"role": "assistant", "content": "Usual reply.", "tool_calls": []})
            out = m.send_user_message("anything new on the co-op list?")
        self.assertEqual(ran, [])
        self.assertEqual(out, f"{route.QUOTA_LINE}\n\nUsual reply.")
        self.assertEqual(m.history()[-1]["content"], out)


# --- the workspace and the agy call ---

class TestWorkspace(AgentModeCase):

    def test_created_0700_outside_the_repository(self):
        ws = agent_dir()
        self.assertEqual(stat.S_IMODE(ws.stat().st_mode), 0o700)
        self.assertNotIn(str(REPO), str(ws.resolve()))
        os.chmod(ws, 0o755)  # loosened by hand: tightened again
        self.assertEqual(stat.S_IMODE(agent_dir().stat().st_mode), 0o700)

    def test_default_place_is_the_apps_data_folder(self):
        with patch.dict(os.environ, {AGENT_DIR_ENV: ""}), patch("pathlib.Path.home",
                                                                 return_value=Path(temp_dir("thirties_home_"))):
            ws = agent_dir()
        self.assertEqual(ws.parts[-4:], (".local", "share", "thirties", WORKSPACE_NAME))

    def test_brief_tool_server_and_permissions_are_written(self):
        ws = agent_dir()
        settings = prepare_workspace(ws)
        brief = (ws / "AGENTS.md").read_text()
        self.assertIn("half-hour blocks", brief)
        self.assertIn("state.md", brief)
        mcp = json.loads((ws / AGY_DIR / "mcp_config.json").read_text())["mcpServers"]["thirties"]
        self.assertEqual(mcp["args"][-1], str(ws / "tools.sock"))
        self.assertTrue((ws / "bin" / "tools_server.py").is_file())
        allow = json.loads(settings.read_text())["permissions"]["allow"]
        self.assertEqual(allow[0], "mcp(thirties/*)")
        self.assertEqual(allow[1], f"read_file({mcp_schema_dir()}/**)")
        self.assertIn(f"read_file({ws}/**)", allow)
        self.assertIn(f"write_file({ws}/**)", allow)
        self.assertIn(f"write_file({os.path.realpath(ws)}/**)", allow)
        self.assertFalse([a for a in allow if a.startswith("write_file(") and "/mcp/" in a], allow)
        self.assertTrue((ws / "state.md").is_file())  # there from the start: a first read finds it
        for f in (ws / "AGENTS.md", ws / AGY_DIR / "mcp_config.json", settings):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_the_agy_command(self):
        ws = agent_dir()
        model = model_name(DEFAULT_MODEL, DEFAULT_EFFORT)
        self.assertEqual(split_thinking_level(model), (DEFAULT_MODEL, "low"))
        self.assertEqual(model_name(model, "high"), model)  # a level already named stays
        cmd = build_command("/opt/agy", ws, ws / "agy-settings.json", model, "conv-9")
        self.assertEqual(cmd[0], "/opt/agy")
        self.assertNotIn("--mode", cmd)  # auto mode isn't available on every account; the allow rules do the work
        for flag, value in (("--model", model), ("--add-dir", str(ws)),
                            ("--settings", str(ws / "agy-settings.json")), ("--conversation", "conv-9"),
                            ("--output-format", "stream-json"), ("--input-format", "stream-json")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--conversation", build_command("/opt/agy", ws, ws / "s.json", "m", None))

    def test_a_turn_resumes_the_days_conversation_and_logs_the_call(self):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout=agy_stream("Sure.", "conv-a1"), stderr="")
        session = AgentSession(runner=runner)
        with patch.object(AgentSession, "agy_binary", return_value="/opt/agy"):
            first = session.run("load the kiln after the vendor call", DAY)
            second = session.run("and the tube patch?", DAY)
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(first.reply, "Sure.")
        self.assertNotIn("--conversation", calls[0][0])
        self.assertEqual(calls[1][0][calls[1][0].index("--conversation") + 1], "conv-a1")
        self.assertIn("personal assistant", json.loads(calls[0][1]["input"])["message"]["content"])  # its brief
        self.assertEqual(json.loads(calls[1][1]["input"])["message"]["content"], "and the tube patch?")
        self.assertEqual(calls[0][1]["cwd"], agent_dir())
        rows = self.ledger.read_text().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].split("\t")[1:], [model_name(DEFAULT_MODEL), "ok", agent_mode.CALLER])

    def test_a_turn_with_no_reply_is_asked_once_more_in_the_same_conversation(self):
        outs = [subprocess.CompletedProcess([], 0, stdout=agy_stream("", "conv-c3"), stderr=""),
                subprocess.CompletedProcess([], 0, stdout=agy_stream("The bench list has two lines.", "conv-c3"),
                                            stderr="")]
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return outs.pop(0)
        session = AgentSession(runner=runner)
        with patch.object(AgentSession, "agy_binary", return_value="/opt/agy"):
            out = session.run("what's on the bench list?", DAY)
        self.assertEqual(out.reply, "The bench list has two lines.")
        self.assertEqual(calls[1][0][calls[1][0].index("--conversation") + 1], "conv-c3")
        self.assertIn("without a reply", json.loads(calls[1][1]["input"])["message"]["content"])

    def test_the_days_history_note_is_there_before_a_turn(self):
        session = AgentSession(runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=agy_stream("Hi.", "conv-d4"), stderr=""))
        with patch.object(AgentSession, "agy_binary", return_value="/opt/agy"):
            session.run("hi", DAY)
        self.assertTrue((agent_dir() / "history" / f"{DAY.isoformat()}.md").is_file())
        self.assertTrue((agent_dir() / "state.md").is_file())

    def test_a_lost_conversation_starts_a_new_one(self):
        outs = [subprocess.CompletedProcess([], 1, stdout="", stderr="conversation not found"),
                subprocess.CompletedProcess([], 0, stdout=agy_stream("Fresh.", "conv-b2"), stderr="")]
        session = AgentSession(runner=lambda cmd, **kw: outs.pop(0))
        session.conversations.set(DAY, "conv-gone")
        with patch.object(AgentSession, "agy_binary", return_value="/opt/agy"):
            out = session.run("hello", DAY)
        self.assertTrue(out.ok)
        self.assertEqual(session.conversations.get(DAY), "conv-b2")

    def test_reading_the_output(self):
        result, conv = read_output(agy_stream("Done that.", "conv-z"))
        self.assertEqual((result["response"], conv), ("Done that.", "conv-z"))
        self.assertEqual(read_output("garbage"), (None, ""))


# --- the tools ---

class TestListTools(AgentModeCase):

    def test_read_lists_as_written(self):
        out = self.call(self.tools(), "read_lists")
        kiln = next(n for n in out["notes"] if n["id"] == "n_kiln")
        self.assertEqual(kiln["title"], "Kiln Studio")
        self.assertEqual(kiln["lines"][:4], ["1: # Glaze bench", "2: - [ ] Mix the celadon batch",
                                             "3:     - [ ] Sieve it twice", "4: - [x] Label the shelf bins"])

    def test_tick_untick_edit_add_remove(self):
        t = self.tools()
        self.call(t, "tick_line", note="n_kiln", line=3, expect="Sieve it twice")
        self.assertIn("    - [x] Sieve it twice\n", self.api.notes["n_kiln"]["body"])
        self.call(t, "untick_line", note="Kiln Studio", line=4)
        self.assertIn("- [ ] Label the shelf bins\n", self.api.notes["n_kiln"]["body"])
        self.call(t, "edit_line", note="Kiln Studio", line=6, text="Load the glaze kiln")
        self.assertIn("- [ ] Load the glaze kiln\n", self.api.notes["n_kiln"]["body"])
        self.call(t, "add_line", note="Kiln Studio", text="Wedge the stoneware", after_line=2, nested=True)
        lines = self.api.notes["n_kiln"]["body"].split("\n")
        self.assertEqual(lines[2], "    - [ ] Wedge the stoneware")
        self.call(t, "add_line", note="Bike Co-op", text="Order rim tape")
        self.assertEqual(self.api.notes["n_coop"]["body"], COOP + "- [ ] Order rim tape\n")
        self.call(t, "remove_line", note="Bike Co-op", line=1, expect="Return the spoke wrench")
        self.assertEqual(self.api.notes["n_coop"]["body"], "- [ ] Patch the demo tube\n- [ ] Order rim tape\n")
        self.assertEqual(t.writes, 6)

    def test_a_line_that_changed_is_refused(self):
        out = self.call(self.tools(), "tick_line", note="Bike Co-op", line=1, expect="Patch the demo tube")
        self.assertIn("now reads", out["error"])
        self.assertEqual(self.api.notes["n_coop"]["body"], COOP)

    def test_no_line_no_note_no_checkbox(self):
        t = self.tools()
        self.assertIn("no line 9", self.call(t, "tick_line", note="Bike Co-op", line=9)["error"])
        self.assertIn("No list called", self.call(t, "tick_line", note="Garden", line=1)["error"])
        self.assertIn("no checkbox", self.call(t, "tick_line", note="Kiln Studio", line=1)["error"])
        self.assertIn("no tool named", self.call(t, "delete_note", note="Kiln Studio")["error"])

    def test_a_whole_note_is_never_cleared(self):
        t = self.tools()
        self.call(t, "remove_line", note="Bike Co-op", line=1)
        out = self.call(t, "remove_line", note="Bike Co-op", line=1)
        self.assertIn("empty", out["error"])
        self.assertEqual(self.api.notes["n_coop"]["body"], "- [ ] Patch the demo tube\n")

    def test_routine_notes_are_never_written(self):
        out = self.call(self.tools(), "untick_line", note="Workshop Opening", line=1)
        self.assertIn("routine", out["error"])
        self.assertEqual(self.api.notes["n_open"]["body"], OPENING)
        self.assertEqual(self.api.updates, [])

    def test_nothing_written_when_joplin_is_closed_or_writing_is_off(self):
        self.api.up = False
        self.assertIn("isn't reachable", self.call(self.tools(), "tick_line", note="n_coop", line=1)["error"])
        self.api.up = True
        self.sched.config.joplin.write_back = False
        self.assertIn("isn't allowed", self.call(self.tools(), "tick_line", note="n_coop", line=1)["error"])
        self.assertEqual(self.api.updates, [])

    def test_every_write_is_journaled_and_logged(self):
        t = self.tools()
        self.call(t, "tick_line", note="Bike Co-op", line=2)
        rows = t.journal.entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["verb"], rows[0]["note_id"]), ("note", "tick_line", "n_coop"))
        self.assertEqual(rows[0]["before"], COOP)
        self.assertIn("- [x] Patch the demo tube", rows[0]["after"])
        from thirties_core.joplin_writeback import WriteLog
        logged = WriteLog(self.sched.state_db).since()
        self.assertEqual(logged[-1]["op"]["op"], "agent_edit")
        self.assertEqual(logged[-1]["removed"], ["- [ ] Patch the demo tube"])

    def test_undo_takes_back_only_the_last_write(self):
        t = self.tools()
        self.call(t, "tick_line", note="Bike Co-op", line=1)
        self.call(t, "edit_line", note="Kiln Studio", line=2, text="Mix the tenmoku batch")
        ok, said = t.undo_last()
        self.assertTrue(ok, said)
        self.assertEqual(self.api.notes["n_kiln"]["body"], KILN)
        self.assertIn("- [x] Return the spoke wrench", self.api.notes["n_coop"]["body"])
        ok, _ = t.undo_last()
        self.assertTrue(ok)
        self.assertEqual(self.api.notes["n_coop"]["body"], COOP)
        ok, said = t.undo_last()
        self.assertFalse(ok)
        self.assertIn("no change", said)

    def test_undo_refuses_a_note_changed_since(self):
        t = self.tools()
        self.call(t, "tick_line", note="Bike Co-op", line=1)
        self.api.notes["n_coop"]["body"] += "- [ ] Typed in by hand\n"
        ok, said = t.undo_last()
        self.assertFalse(ok)
        self.assertIn("changed since", said)
        self.assertIn("Typed in by hand", self.api.notes["n_coop"]["body"])


class TestPlanTools(AgentModeCase):

    def test_read_calendar_and_now(self):
        t = self.tools()
        cal = self.call(t, "read_calendar")
        self.assertEqual([(e["title"], e["start"], e["end"]) for e in cal["events"]],
                         [("Kiln vendor call", "14:35", "15:25")])
        now = self.call(t, "now")
        self.assertEqual((now["now"][:10], now["now"][11:16]), (DAY.isoformat(), "11:10"))
        self.assertEqual(now["block"], "11:00-11:30")

    def test_set_plan_goes_into_the_apps_day_plan_and_undo_restores_it(self):
        db = self.sched.state_db
        old = EventSpan("old1", "Sort the sandpaper", at("16:20"), at("16:50"), SpanCategory.TASK)
        done = EventSpan("done1", "Oil the bandsaw", at("09:40"), at("10:05"), SpanCategory.ACTIVITY,
                         status=SpanStatus.DONE)
        db.upsert_span(old)
        db.upsert_span(done)
        t = self.tools()
        out = self.call(t, "set_plan", items=[
            {"title": "Load the bisque kiln", "start": "10:20", "length": 40, "task_ref": "t_kiln"},
            {"title": "Patch the demo tube", "start": "1:50 PM", "length": "25m", "note": "spare patches in drawer"}])
        self.assertEqual(out["replaced"], 1)
        plan, _ = self.sched.build_day_plan(DAY)
        mine = {s.title: s for s in plan.spans if s.source == "user"}
        self.assertNotIn("Sort the sandpaper", mine)
        self.assertIn("Oil the bandsaw", mine)  # what's done stays
        kiln = mine["Load the bisque kiln"]
        self.assertEqual((kiln.start_dt, kiln.end_dt - kiln.start_dt), (at("10:20"), timedelta(minutes=40)))
        self.assertEqual((kiln.category, kiln.task_id), (SpanCategory.TASK, "t_kiln"))
        self.assertEqual(mine["Patch the demo tube"].start_dt, at("13:50"))
        self.assertIsNone(kiln.given_start)  # a time Gemini picked: shown by its block, never to the minute
        self.assertTrue(any(s.title == "Kiln vendor call" for s in plan.spans))  # the calendar stays
        read = self.call(t, "read_plan")
        self.assertIn("Load the bisque kiln", [i["title"] for i in read["items"]])
        ok, _ = t.undo_last()
        self.assertTrue(ok)
        plan, _ = self.sched.build_day_plan(DAY)
        titles = {s.title for s in plan.spans if s.source == "user"}
        self.assertEqual(titles, {"Sort the sandpaper", "Oil the bandsaw"})

    def test_set_plan_refuses_an_unreadable_item_and_changes_nothing(self):
        t = self.tools()
        out = self.call(t, "set_plan", items=[{"title": "Wedge clay", "start": "whenever", "length": 30}])
        self.assertIn("Item 1", out["error"])
        self.assertEqual(t.journal.entries(), [])


# --- the bridge and the MCP server ---

class TestBridgeAndServer(AgentModeCase):

    def test_a_tool_call_goes_through_the_socket(self):
        t = self.tools()
        sock = agent_dir() / "tools.sock"
        with ToolBridge(sock, t.call):
            self.assertEqual(stat.S_IMODE(sock.stat().st_mode), 0o600)
            text, failed = tools_server.relay(str(sock), "tick_line", {"note": "Bike Co-op", "line": 1})
            self.assertFalse(failed)
            self.assertIn("- [x] Return the spoke wrench", self.api.notes["n_coop"]["body"])
            text, failed = tools_server.relay(str(sock), "tick_line", {"note": "Bike Co-op", "line": 99})
            self.assertTrue(failed)
        self.assertFalse(sock.exists())
        text, failed = tools_server.relay(str(sock), "now", {})
        self.assertTrue(failed)
        self.assertIn("isn't answering", text)

    def test_mcp_messages(self):
        init = tools_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18"}}, "/nonexistent")
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(tools_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "/x"))
        names = [t["name"] for t in tools_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                                       "/x")["result"]["tools"]]
        self.assertEqual(names, ["read_lists", "add_line", "tick_line", "untick_line", "edit_line", "remove_line",
                                 "read_calendar", "read_plan", "set_plan", "now"])
        from thirties_core.agent_mode.tools import READ_TOOLS, WRITE_TOOLS
        self.assertEqual(set(names), set(READ_TOOLS + WRITE_TOOLS))
        unknown = tools_server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, "/x")
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_the_server_runs_standalone_over_stdio(self):
        """As agy starts it: a separate python -I process, the standard library only, talking to the app."""
        t = self.tools()
        ws = agent_dir()
        prepare_workspace(ws)
        sock = ws / "tools.sock"
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "read_lists", "arguments": {"note": "Bike Co-op"}}}]
        with ToolBridge(sock, t.call):
            res = subprocess.run(["python3", "-I", str(ws / "bin" / "tools_server.py"), str(sock)],
                                 input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True,
                                 text=True, timeout=60, cwd=ws)
        out = [json.loads(l) for l in res.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2])
        lines = json.loads(out[1]["result"]["content"][0]["text"])["notes"][0]["lines"]
        self.assertEqual(lines[0], "1: - [ ] Return the spoke wrench")


if __name__ == "__main__":
    unittest.main()


# --- the switch in Developer options ---

from tests import test_gui as gui  # noqa: E402  (GTK, a private session only)


@unittest.skipUnless(gui.GTK, "GTK 4 / libadwaita or a display is not available")
class TestDeveloperOptionsSwitch(unittest.TestCase):

    def setUp(self) -> None:
        from thirties_core.app_core import AppCore
        from thirties_core.config import ThirtiesConfig
        env = patch.dict(os.environ, {"XDG_DATA_HOME": temp_dir("thirties_gui_data_")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(agent_mode.AGENT_MODE_ENV, None)
        os.environ.pop("THIRTIES_OFFER_GEMMA", None)
        self.core = AppCore(use_keyring=False, config=ThirtiesConfig())

    def switches(self):
        ctx = gui.settings_rows.SettingsContext(self.core)
        group = gui.settings_rows.gemini_group(ctx)
        return {r.get_title(): r for r in gui._rows_of_type(group, gui.Adw.SwitchRow)}

    def test_hidden_without_the_developer_switch(self):
        self.assertNotIn(gui.settings_rows.AGENT_MODE_TITLE, self.switches())

    def test_shown_with_it_off_and_it_turns_agent_mode_on_and_off(self):
        with patch.dict(os.environ, {"THIRTIES_OFFER_GEMMA": "1"}):  # the developer switch
            row = self.switches()[gui.settings_rows.AGENT_MODE_TITLE]
        self.assertFalse(row.get_active())
        db = self.core.scheduler.state_db
        row.set_active(True)
        self.assertTrue(agent_mode.is_on(self.core.config, db))
        row.set_active(False)
        self.assertFalse(agent_mode.is_on(self.core.config, db))
