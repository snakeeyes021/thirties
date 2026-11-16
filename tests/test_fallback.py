"""When the usual model's usage limit is reached (fallback.py): a simpler assistant answers, the user is told once,
nothing calls the usual model before its reset time, and the usual one is back after it. Fakes and a fake clock
only: no model is ever called."""

import json
import re
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler, temp_dir
from tests.test_conversation import reply
from tests.test_user_text import BANNED, INTERNAL_IDS
from thirties_core import fallback, turns
from thirties_core.astronomy import get_current_time, set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.fallback import BACK, EXHAUSTED, SIMPLER, UNAVAILABLE, FallbackEngine, reset_after, when_text
from thirties_core.inference import MockInferenceEngine

QUOTA = "Error contacting model via Antigravity CLI: error: Individual quota reached. Resets in 3h20m."
QUOTA_NO_TIME = "Error contacting model via Antigravity CLI: RESOURCE_EXHAUSTED (429)"


class Fake:
    """An engine that answers from a script (a list, then the last entry forever) and counts its calls."""

    def __init__(self, *script, model="fake-model"):
        self.script = list(script)
        self.calls = 0
        self.model = self.model_name = model

    def is_available(self):
        return True

    def chat(self, messages, tools=None):
        self.calls += 1
        self.last = list(messages)
        out = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return reply(out) if not out.startswith("Error") else {"role": "assistant", "content": out, "tool_calls": []}


class DeviceBrain:
    """Stands in for the on-device brain: no model, no GPU."""

    def __init__(self):
        self.messages = [{"role": "system", "content": "device"}]
        self.said = []

    def reset(self):
        self.messages = [self.messages[0]]

    def respond(self, text):
        self.said.append(text)
        self.messages.append({"role": "user", "content": text})
        self.messages.append({"role": "assistant", "content": "Noted on this computer."})
        return "Noted on this computer."


class FallbackCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        waits = turns.RETRY_WAITS
        turns.RETRY_WAITS = (0.0, 0.0)
        self.addCleanup(setattr, turns, "RETRY_WAITS", waits)
        mirror = turns.MIRROR_IN_BACKGROUND
        turns.MIRROR_IN_BACKGROUND = False
        self.addCleanup(setattr, turns, "MIRROR_IN_BACKGROUND", mirror)
        self.sched = make_scheduler()
        self.sched.config.assistant.fallback_calls_per_5h = 3
        self.primary = Fake("Sure, noted.")
        self.second = Fake("Jotted that down for you.")
        self.device = False
        self.engine = FallbackEngine(self.primary, self.sched.config, self.sched.state_db,
                                     lambda: get_current_time(self.sched.tz), fallback=self.second,
                                     device_ready=lambda: self.device)
        self.phone = []
        mirror_fn = lambda texts, answer, sent: self.phone.append(answer)
        turns.add_mirror(mirror_fn)
        self.addCleanup(turns.remove_mirror, mirror_fn)

    def manager(self) -> ConversationManager:
        plan, pending = self.sched.build_day_plan(DAY)
        return ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine, active_notes=[])

    def state(self):
        return fallback.FallbackState(self.sched.state_db).load()


class TestReadingTheError(unittest.TestCase):

    def test_reset_times_from_the_error(self):
        self.assertEqual(reset_after(QUOTA), timedelta(hours=3, minutes=20))
        self.assertEqual(reset_after("Individual quota reached. Resets in 5h45m."), timedelta(hours=5, minutes=45))
        self.assertEqual(reset_after("quota reached, resets in 1d6h"), timedelta(days=1, hours=6))
        self.assertEqual(reset_after('HTTP 429: {"retryDelay": "37s"}'), timedelta(seconds=37))
        self.assertEqual(reset_after("429 Too Many Requests. Please retry in 12.5s."), timedelta(seconds=12.5))
        self.assertIsNone(reset_after(QUOTA_NO_TIME))

    def test_until_when_is_a_thirty_with_the_day_when_not_today(self):
        now = at("10:00")
        self.assertEqual(when_text(at("13:20"), now), "about 1:30 PM")
        self.assertEqual(when_text(at("13:30"), now), "about 1:30 PM")
        self.assertEqual(when_text(at("09:10", 1), now), "tomorrow at about 9:30 AM")
        self.assertEqual(when_text(at("08:00", 3), now), "Sunday at about 8:00 AM")

    def test_only_the_apps_gemini_engine_gets_the_fallback(self):
        sched = make_scheduler()
        mock = MockInferenceEngine()
        self.assertIs(fallback.with_fallback(mock, sched.config, sched.state_db, sched.tz), mock)
        from thirties_core.inference import GeminiSourcesEngine
        sources = GeminiSourcesEngine.__new__(GeminiSourcesEngine)
        wrapped = fallback.with_fallback(sources, sched.config, sched.state_db, sched.tz)
        self.assertIsInstance(wrapped, FallbackEngine)
        sched.config.assistant.quota_fallback = False
        self.assertIs(fallback.with_fallback(sources, sched.config, sched.state_db, sched.tz), sources)


class TestSwitching(FallbackCase):

    def test_a_usage_limit_mid_day_switches_to_the_second_model_and_says_so_once(self):
        m = self.manager()
        self.assertEqual(m.send_user_message("the plants are watered"), "Sure, noted.")
        self.primary.script = [QUOTA]
        set_debug_time(at("10:10"))
        out = m.send_user_message("and the bins are out")
        self.assertEqual(out, SIMPLER.format(when="about 1:30 PM") + "\n\nJotted that down for you.")
        self.assertEqual((self.primary.calls, self.second.calls), (2, 1))
        set_debug_time(at("11:00"))
        self.assertEqual(m.send_user_message("the post came"), "Jotted that down for you.")  # not said again
        self.assertEqual(self.primary.calls, 2)  # nothing calls the usual model before its reset time
        said = [h["content"] for h in m.history() if h["role"] == "assistant"]
        self.assertEqual(sum(SIMPLER.split(",")[0] in s for s in said), 1)

    def test_the_app_and_the_phone_get_the_same_one_stored_message(self):
        self.primary.script = [QUOTA]
        out = self.manager().send_user_message("the plants are watered")
        self.assertEqual(self.phone, [out])  # the desktop's turn, mirrored to the phone as stored
        stored = self.manager().history()  # e.g. the background service's manager, which runs Telegram
        self.assertEqual([h["content"] for h in stored if h["role"] == "assistant"], [out])
        self.assertEqual(out.count("simpler assistant"), 1)

    def test_the_second_model_is_capped_then_chat_waits(self):
        self.primary.script = [QUOTA]
        m = self.manager()
        for said in ("one", "two", "three"):
            self.assertIn("Jotted", m.send_user_message(said))
        self.assertEqual(self.second.calls, 3)  # the cap: 3 calls in any 5 hours
        out = m.send_user_message("four")
        self.assertTrue(out.startswith(UNAVAILABLE.format(when="about 1:30 PM")), out)
        self.assertEqual((self.primary.calls, self.second.calls), (1, 3))
        self.assertEqual([u["content"] for u in m.unanswered()], ["four"])  # kept for when it's back

    def test_the_second_models_own_usage_limit_stops_it_at_once(self):
        self.primary.script = [QUOTA]
        self.second.script = ["Error contacting model via Antigravity CLI: Individual quota reached."]
        m = self.manager()
        self.assertTrue(m.send_user_message("one").startswith("The assistant is unavailable until"))
        m.send_user_message("two")
        self.assertEqual(self.second.calls, 1)
        self.assertEqual(self.state()["fallback_until"], "max")

    def test_the_cap_counts_only_the_last_five_hours(self):
        state = fallback.FallbackState(self.sched.state_db)
        for hhmm in ("10:00", "11:00", "14:30"):
            state.count_fallback(at(hhmm))
        self.assertFalse(state.fallback_ready(at("14:45"), 3))
        self.assertTrue(state.fallback_ready(at("15:01"), 3))  # 10:00's has left the window
        self.assertEqual(len(state.calls_in_window(at("15:01"))), 2)


class TestTheDeviceModel(FallbackCase):

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.fallback_calls_per_5h = 0  # the second model has nothing left
        self.brain = DeviceBrain()
        from thirties_core import conversation
        real = conversation.make_brain

        def make(config, tools, engine, tasks=None, active_notes=None, choice=None):
            return self.brain if choice == "slm" else real(config, tools, engine, tasks=tasks,
                                                           active_notes=active_notes, choice=choice)
        patcher = patch("thirties_core.conversation.make_brain", side_effect=make)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_device_model_stands_in_only_when_offered_and_installed(self):
        self.primary.script = [QUOTA]
        self.device = True
        m = self.manager()
        out = m.send_user_message("the bins are out")  # lost to the limit mid-turn: the device model takes it
        self.assertEqual(out, SIMPLER.format(when="about 1:30 PM") + "\n\nNoted on this computer.")
        self.assertEqual(self.brain.said, ["the bins are out"])
        self.assertEqual(m.unanswered(), [])
        self.assertEqual(m.send_user_message("the post came"), "Noted on this computer.")
        self.assertEqual(self.primary.calls, 1)
        from thirties_core.settings import Settings
        self.assertIsNone(Settings(self.sched.state_db).get("assistant.brain"))  # never saved as the user's choice

    def test_without_it_chat_waits(self):
        self.primary.script = [QUOTA]
        out = self.manager().send_user_message("the bins are out")
        self.assertTrue(out.startswith(UNAVAILABLE.format(when="about 1:30 PM")), out)
        self.assertEqual(self.brain.said, [])

    def test_the_usual_brain_comes_back_after_the_reset(self):
        self.primary.script = [QUOTA, "Back to normal."]
        self.device = True
        m = self.manager()
        m.send_user_message("the bins are out")
        self.assertEqual(m.brain_name, "slm")
        set_debug_time(at("13:31"))
        self.assertEqual(m.send_user_message("the post came"), BACK + "\n\nBack to normal.")
        self.assertEqual(m.brain_name, "frontier")


class TestNoModel(FallbackCase):

    def setUp(self):
        super().setUp()
        self.primary.script = [QUOTA]
        self.second.script = ["Error contacting model: usage limit reached"]
        self.m = self.manager()
        self.m.execute_tool("add_event", {"title": "Repot the fern", "start": "10:30 AM", "end": "11:00 AM"})
        self.m.execute_tool("add_event", {"title": "Tune the banjo", "start": "11:30 AM", "end": "12:00 PM"})
        self.first = self.m.send_user_message("how's my morning")
        self.calls = (self.primary.calls, self.second.calls)

    def test_chat_says_until_when_with_whats_next_and_keeps_the_message(self):
        self.assertTrue(self.first.startswith(UNAVAILABLE.format(when="about 1:30 PM")), self.first)
        self.assertIn("Repot the fern", self.first)  # what's next, from Python
        self.assertEqual([u["content"] for u in self.m.unanswered()], ["how's my morning"])
        self.assertEqual(self.m.unanswered()[0]["retry_after"][:16], at("13:20").isoformat()[:16])  # shown as 1:30

    def test_ticks_the_day_page_and_whats_next_keep_working_without_a_model(self):
        from thirties_core import whats_next
        from thirties_core.messages import render_schedule
        fern = next(s for s in self.m.day_plan.spans if s.title == "Repot the fern")
        out = self.m.execute_tool("log_outcome", {"event": fern.id, "status": "done"})
        self.assertNotIn("Error", out)
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(next(s for s in plan.spans if s.title == "Repot the fern").status, "done")
        self.assertIn("Tune the banjo", render_schedule(self.m.day_plan, at("10:00"), whole_day=True))
        self.assertIn("Tune the banjo", whats_next.summary(self.m.tools, at("10:00"), "rest"))
        self.assertEqual((self.primary.calls, self.second.calls), self.calls)  # none of it called a model

    def test_more_messages_wait_together_and_are_answered_after_the_reset(self):
        set_debug_time(at("11:00"))
        self.assertTrue(self.m.send_user_message("and the evening?").startswith("The assistant is unavailable"))
        self.assertEqual([u["content"] for u in self.m.unanswered()], ["how's my morning", "and the evening?"])
        self.assertIsNone(self.m.answer_unanswered(at("13:00")))
        self.assertEqual((self.primary.calls, self.second.calls), self.calls)
        self.primary.script = ["Here's your day."]
        set_debug_time(at("13:31"))
        answer = self.m.answer_unanswered(at("13:31"))
        self.assertEqual(answer, "Here's your day.")  # the user was never told of a simpler one: nothing to undo
        self.assertEqual(self.primary.calls, self.calls[0] + 1)
        self.assertEqual(self.m.unanswered(), [])
        users = [m["content"] for m in self.primary.last if m.get("role") == "user"]
        self.assertIn("how's my morning", users)  # both kept messages, in one turn
        self.assertIn("and the evening?", users)


class TestRecovery(FallbackCase):

    def test_the_usual_model_is_tried_again_only_after_the_reset_time_by_a_real_turn(self):
        self.primary.script = [QUOTA, "Usual reply."]
        m = self.manager()
        m.send_user_message("one")
        set_debug_time(at("13:19"))
        self.assertIn("Jotted", m.send_user_message("two"))
        self.assertEqual(self.primary.calls, 1)
        set_debug_time(at("13:21"))
        self.assertEqual(m.send_user_message("three"), BACK + "\n\nUsual reply.")
        self.assertEqual(m.send_user_message("four"), "Usual reply.")  # said once
        self.assertEqual(self.primary.calls, 3)
        self.assertEqual(self.state().get("try_again_at"), None)

    def test_back_is_said_only_when_the_user_was_told(self):
        fallback.FallbackState(self.sched.state_db).chosen_out(at("09:00"), QUOTA)  # ran out while nobody talked
        self.primary.script = ["Usual reply."]
        set_debug_time(at("12:21"))
        self.assertEqual(self.manager().send_user_message("hi"), "Usual reply.")
        self.assertFalse(self.state().get("back"))
        self.assertIsNone(self.state().get("try_again_at"))

    def test_no_tight_loop_with_no_reset_time_waits_grow_15_30_60(self):
        """A fake clock ticks every minute for six hours with the background retrying a kept message: the usual
        model is called only at each guessed retry time, never in between."""
        self.primary.script = [QUOTA_NO_TIME]
        self.second.script = ["Error contacting model: usage limit reached"]
        m = self.manager()
        m.send_user_message("the bins are out")
        tried = []
        last = self.primary.calls
        for minute in range(1, 6 * 60):
            now = at("10:00") + timedelta(minutes=minute)
            set_debug_time(now.isoformat())
            m.answer_unanswered(now)
            if self.primary.calls != last:
                tried.append(minute)
                last = self.primary.calls
        self.assertEqual(tried[:5], [15, 45, 105, 165, 225])  # 15, then 30, then 60 minutes apart
        self.assertEqual(self.primary.calls, 1 + len(tried))
        self.assertEqual(self.second.calls, 1)  # it said its limit once, and was left alone

    def test_a_reset_time_in_the_error_is_waited_for(self):
        self.primary.script = [QUOTA]
        self.second.script = ["Error contacting model: usage limit reached"]
        m = self.manager()
        m.send_user_message("the bins are out")
        for minute in range(1, 200):
            now = at("10:00") + timedelta(minutes=minute)
            set_debug_time(now.isoformat())
            m.answer_unanswered(now)
        self.assertEqual(self.primary.calls, 1)  # 3h20m: nothing before 1:20 PM
        now = at("13:21")
        set_debug_time(now.isoformat())
        self.primary.script = ["Done, the bins are noted."]
        self.assertEqual(m.answer_unanswered(now), "Done, the bins are noted.")
        self.assertEqual(self.primary.calls, 2)

    def test_a_failed_retry_after_the_reset_waits_again_and_stays_quiet(self):
        self.primary.script = [QUOTA]
        self.second.script = ["Error contacting model: usage limit reached"]
        m = self.manager()
        m.send_user_message("the bins are out")
        before = len(m.history())
        set_debug_time(at("13:21"))
        self.assertIsNone(m.answer_unanswered(at("13:21")))
        self.assertEqual(len(m.history()), before)  # nothing new said
        self.assertEqual(self.primary.calls, 2)
        self.assertEqual(m.unanswered()[0]["retry_after"][:16], (at("13:21") + timedelta(hours=3, minutes=20))
                         .isoformat()[:16])


class TestListSorting(FallbackCase):
    """Sorting the lists waits while the usual model is out: no calls to it, none to the second model, and it
    starts again once a real turn has had the usual answer."""

    def setUp(self):
        super().setUp()
        self.sorted = []

        def chat(engine, messages, tools=None):
            self.sorted.append(messages)
            return {"role": "assistant", "content": '{"role": "ignore"}', "tool_calls": []}
        for patcher in (patch("thirties_core.inference.GeminiSourcesEngine.is_available", return_value=True),
                        patch("thirties_core.inference.GeminiSourcesEngine.chat", chat)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sorter(self):
        from thirties_core.brains.list_sorter import list_sorter_for
        return list_sorter_for(self.sched.config, self.sched.state_db, self.engine)

    def test_the_sorter_waits_until_a_turn_has_the_usual_answer_again(self):
        from thirties_core.brains.list_sorter import FrontierListSorter
        self.assertIsInstance(self.sorter(), FrontierListSorter)
        self.primary.script = [QUOTA, "Usual reply."]
        m = self.manager()
        m.send_user_message("one")  # out from 10:00 until 1:20 PM
        self.assertIsNone(self.sorter())
        set_debug_time(at("13:25"))
        self.assertIsNone(self.sorter())  # past the reset, but nothing has answered yet: sorting never probes
        self.assertEqual(m.send_user_message("two"), BACK + "\n\nUsual reply.")
        self.assertIsInstance(self.sorter(), FrontierListSorter)

    def test_the_background_run_makes_no_calls_while_out_and_resumes_after(self):
        from thirties_core.background import BackgroundService
        joplin = MagicMock()
        joplin.fetch_tasks.return_value = []
        joplin.fetch_active_notes.return_value = [
            {"id": "n_kites", "title": "Kite repairs", "source_notebook": "Lists", "body": "- [ ] Patch the red one\n"}]
        svc = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=joplin,
                                inference_engine=self.engine, sync_engine=MagicMock())
        svc.today = lambda: DAY
        svc.sort_lists_enabled = True

        class Now:  # the sort runs in its own thread: here, right away
            def __init__(self, target=None, **_):
                self.target = target

            def start(self):
                self.target()
        self.primary.script = [QUOTA, "Usual reply."]
        m = self.manager()
        m.send_user_message("one")
        calls = (self.primary.calls, self.second.calls)
        with patch("thirties_core.background.threading.Thread", Now):
            for minute in (0, 60, 200):  # through the reset time (1:20 PM), with nothing answered yet
                set_debug_time((at("10:05") + timedelta(minutes=minute)).isoformat())
                svc._last_lists = 0.0
                svc.sort_lists()
            self.assertEqual(self.sorted, [])
            self.assertEqual((self.primary.calls, self.second.calls), calls)
            set_debug_time(at("13:30"))
            m.send_user_message("two")  # the usual model answers a real turn
            svc._last_lists = 0.0
            svc.sort_lists()
        self.assertTrue(self.sorted)  # sorting again
        self.assertEqual(self.second.calls, calls[1])  # never on the second model


class TestTheLedger(unittest.TestCase):

    def setUp(self):
        ledger = patch("thirties_core.inference.AGY_LEDGER", Path(temp_dir("thirties_tmp_")) / "agy_calls.log")
        self.ledger = ledger.start()
        self.addCleanup(ledger.stop)
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)

    def test_the_second_models_calls_are_logged_like_every_agy_call(self):
        from thirties_core.inference import AGY_CALLER_ENV
        sched = make_scheduler()
        primary = Fake(QUOTA)
        engine = FallbackEngine(primary, sched.config, sched.state_db, lambda: get_current_time(sched.tz),
                                device_ready=lambda: False)
        ok = MagicMock(returncode=0, stderr="",
                       stdout='{"event":"result","result":{"status":"SUCCESS","response":"Noted."}}\n')
        quota = MagicMock(returncode=3, stdout="", stderr="error: Individual quota reached. Resets in 6d2h.")
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", side_effect=[ok, quota]) as run, \
                patch.dict("os.environ", {AGY_CALLER_ENV: "test:fallback"}):
            self.assertEqual(engine.chat([{"role": "user", "content": "a"}])["content"], "Noted.")
            self.assertEqual(engine.chat([{"role": "user", "content": "b"}])["content"], EXHAUSTED)
            self.assertEqual(engine.chat([{"role": "user", "content": "c"}])["content"], EXHAUSTED)
        self.assertEqual(run.call_count, 2)  # stopped at its first usage-limit error
        self.assertIn("gemini-4-argon-low", run.call_args_list[0][0][0])
        rows = [line.split("\t")[1:] for line in self.ledger.read_text().splitlines()]
        self.assertEqual(rows, [["gemini-4-argon-low", "ok", "test:fallback"],
                                ["gemini-4-argon-low", "quota", "test:fallback"]])
        self.assertEqual(primary.calls, 1)


class TestWhatTheUserReads(unittest.TestCase):

    def test_the_texts_are_plain_and_in_the_users_terms(self):
        now = at("10:00")
        for text in (SIMPLER.format(when=when_text(at("13:20"), now)), BACK,
                     UNAVAILABLE.format(when=when_text(at("09:00", 1), now))):
            self.assertFalse(BANNED.search(text) or INTERNAL_IDS.search(text), text)
            self.assertFalse(re.search(r"^- ", text, re.MULTILINE), text)
            self.assertNotRegex(text.lower(), r"quota|model|gemini|argon|gemma|fallback|error")


if __name__ == "__main__":
    unittest.main()
