"""The model-neutral runtime: parser sessions over any backend, the llama.cpp backend (with a stand-in for the
library: no model is loaded), and the model catalog."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from thirties_core.brains.slm import catalog
from thirties_core.brains.slm.runtime import ConversationRuntime, LlamaCppRuntime, ParserSession, SlmUnavailable


class ScriptedRuntime(ConversationRuntime):
    """A backend whose conversations are lists; it answers with how many user messages it has seen."""

    def __init__(self, fail_once=False):
        super().__init__()
        self.opened = 0
        self.fail_once = fail_once

    def open_conversation(self, system):
        self.opened += 1
        return []

    def send(self, conversation, user):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("kv cache full")
        conversation.append(user)
        return f"reply {len(conversation)}"

    def close_conversation(self, conversation):
        conversation.clear()


class TestParserSession(unittest.TestCase):

    def test_one_conversation_until_the_budget_is_spent(self):
        rt = ScriptedRuntime()
        session = ParserSession(rt, "You convert things", budget_chars=2000)
        self.assertEqual([session.ask("x" * 100) for _ in range(3)], ["reply 1", "reply 2", "reply 3"])
        self.assertEqual(rt.opened, 1)
        session.ask("x" * 1500)  # would overflow: a fresh conversation
        self.assertEqual(rt.opened, 2)
        self.assertEqual([c["kind"] for c in rt.calls], ["parse"] * 4)

    def test_a_failed_call_retries_in_a_fresh_conversation(self):
        rt = ScriptedRuntime(fail_once=True)
        self.assertEqual(ParserSession(rt, "You help choose").ask("hi"), "reply 1")
        self.assertEqual(rt.opened, 2)
        self.assertEqual(rt.calls[-1]["kind"], "pick")

    def test_the_budget_follows_the_runtimes_context(self):
        rt = ScriptedRuntime()
        rt.context_tokens = 4096
        self.assertEqual(ParserSession(rt, "").budget_chars, int((4096 - 700) * 3.0))


class FakeLlama:
    """Stands in for llama_cpp.Llama: records each request and answers from a script."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.replies = []
        FakeLlama.instances.append(self)

    def create_chat_completion(self, **kwargs):
        self.requests.append(kwargs)
        content = self.replies.pop(0) if self.replies else "ok"
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestLlamaCppRuntime(unittest.TestCase):

    def setUp(self):
        fake = types.ModuleType("llama_cpp")
        fake.Llama = FakeLlama
        patcher = mock.patch.dict(sys.modules, {"llama_cpp": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rt = LlamaCppRuntime(Path("/models/Phi-4-mini-instruct-Q4_K_M.gguf"), context_tokens=4096)
        self.llm = FakeLlama.instances[-1]

    def test_loads_on_the_gpu_with_the_context_window(self):
        self.assertEqual((self.llm.kwargs["n_ctx"], self.llm.kwargs["n_gpu_layers"]), (4096, -1))

    def test_json_call_is_constrained_by_the_schema(self):
        self.llm.replies.append('{"actions": [{"type": "chat"}]}')
        schema = {"type": "object"}
        self.assertEqual(self.rt.json_call("sys", "hi", schema), {"actions": [{"type": "chat"}]})
        request = self.llm.requests[-1]
        self.assertEqual(request["response_format"], {"type": "json_object", "schema": schema})
        self.assertEqual(request["temperature"], 0.0)
        self.llm.replies.append("not json")
        self.assertEqual(self.rt.json_call("sys", "hi", schema), {})

    def test_a_parser_session_keeps_its_history(self):
        session = self.rt.parser("You convert one message")
        self.llm.replies += ['{"actions":[]}', '{"actions":[{"type":"chat"}]}']
        session.ask("first")
        session.ask("second")
        messages = self.llm.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[-1]["content"], "second")
        self.assertNotIn("response_format", self.llm.requests[-1])  # free-text JSON, validated by the brain

    def test_text_call(self):
        self.llm.replies.append("  Happy to help!  ")
        self.assertEqual(self.rt.text_call("sys", "thanks"), "Happy to help!")
        self.assertEqual([c["kind"] for c in self.rt.calls], ["text"])

    def test_without_the_library_it_says_why(self):
        with mock.patch.dict(sys.modules, {"llama_cpp": None}):
            with self.assertRaises(SlmUnavailable):
                LlamaCppRuntime(Path("/models/x.gguf"))


class TestCatalog(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.home.name, "HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(catalog.ENV_VAR, None)
        self.models = Path(self.home.name) / "thirties" / "models"
        self.models.mkdir(parents=True)

    def test_gemma_is_the_default_and_others_can_be_chosen(self):
        self.assertEqual(catalog.selected_model(), "gemma-4-e4b")
        config = types.SimpleNamespace(assistant=types.SimpleNamespace(slm_model="qwen3-4b"))
        self.assertEqual(catalog.selected_model(config), "qwen3-4b")
        with mock.patch.dict(os.environ, {catalog.ENV_VAR: "Phi-4-Mini"}):
            self.assertEqual(catalog.selected_model(config), "phi-4-mini")
        self.assertEqual(catalog.selected_model(config, model="gemma-4-e2b"), "gemma-4-e2b")

    def test_why_a_model_cant_run(self):
        self.assertIn("unknown model", catalog.model_unavailable_reason("gpt-9"))
        with mock.patch.object(catalog, "_backend_importable", return_value=True):
            self.assertIn("Phi-4-mini-instruct-Q4_K_M.gguf", catalog.model_unavailable_reason("phi-4-mini"))
            (self.models / "Phi-4-mini-instruct-Q4_K_M.gguf").write_bytes(b"")
            self.assertIsNone(catalog.model_unavailable_reason("phi-4-mini"))
            self.assertIn("phi-4-mini", catalog.available_models())
            self.assertIn("hasn't been downloaded", catalog.model_unavailable_reason("gemma-4-e4b"))
        with mock.patch.object(catalog, "_backend_importable", return_value=False):
            self.assertIn("llama-cpp-python", catalog.model_unavailable_reason("phi-4-mini"))

    def test_every_model_is_described_for_the_comparison(self):
        for spec in catalog.MODELS.values():
            self.assertIn(spec.backend, ("litert", "llama.cpp"))
            self.assertTrue(spec.filenames and spec.licence and spec.params, spec.name)

    def test_the_default_loads_as_before(self):
        sentinel = object()
        with mock.patch("thirties_core.brains.slm.runtime.GemmaRuntime.shared", return_value=sentinel) as shared:
            config = types.SimpleNamespace(assistant=types.SimpleNamespace(slm_model_path="/x/gemma.litertlm"))
            self.assertIs(catalog.load_runtime(config), sentinel)
        shared.assert_called_once_with("/x/gemma.litertlm")

    def test_a_missing_model_never_downloads(self):
        with mock.patch.object(catalog, "_backend_importable", return_value=True):
            with self.assertRaises(SlmUnavailable):
                catalog.load_runtime(model="qwen3-4b")

    def test_a_spike_model_loads_through_llama_cpp(self):
        (self.models / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf").write_bytes(b"")
        fake = types.ModuleType("llama_cpp")
        fake.Llama = FakeLlama
        with mock.patch.dict(sys.modules, {"llama_cpp": fake}), \
                mock.patch.object(catalog, "_backend_importable", return_value=True), \
                mock.patch.dict(catalog._loaded, clear=True):
            rt = catalog.load_runtime(model="qwen3-4b")
            self.assertIsInstance(rt, LlamaCppRuntime)
            self.assertIs(catalog.load_runtime(model="qwen3-4b"), rt)  # once per process

    def test_the_apps_check_still_names_gemma(self):
        from thirties_core.brains.slm.runtime import slm_unavailable_reason
        with mock.patch.object(catalog, "_backend_importable", return_value=False):
            self.assertEqual(slm_unavailable_reason(None), "the LiteRT-LM runtime isn't installed.")


if __name__ == "__main__":
    unittest.main()
