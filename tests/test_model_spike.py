"""The llama.cpp runtime adapter, the device speed probe and the spike harness's pure helpers (no real model)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from thirties_core.brains.slm.llama_server import LlamaParserSession, LlamaServerError, LlamaServerRuntime
from thirties_core.brains.slm.speed_probe import PROBE_MESSAGES, describe, probe


class FakeLlamaServer:
    """Answers /health and /v1/chat/completions like llama-server, recording each request body."""

    def __init__(self, reply=lambda body: '{"actions":[{"type":"chat"}]}'):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"status": "ok"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                text = reply(body)
                if text is None:
                    self._send(500, {"error": "boom"})
                    return
                self._send(200, {"choices": [{"message": {"content": text}}],
                                 "timings": {"prompt_n": 10, "cache_n": 5, "predicted_n": 3}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class LlamaRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeLlamaServer()
        self.rt = LlamaServerRuntime(self.server.url, slots=3, ctx_per_slot=4096, timeout=10)

    def tearDown(self):
        self.server.close()

    def test_json_call_sends_the_schema_and_turns_thinking_off(self):
        schema = {"type": "object", "properties": {"actions": {"type": "array"}}}
        out = self.rt.json_call("sys", "hi", schema)
        self.assertEqual(out, {"actions": [{"type": "chat"}]})
        body = self.server.requests[-1]
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(self.rt.calls[-1]["kind"], "json")
        self.assertEqual(self.rt.calls[-1]["cached_tokens"], 5)

    def test_unparseable_json_is_an_empty_dict(self):
        self.server.close()
        self.server = FakeLlamaServer(reply=lambda body: "not json")
        rt = LlamaServerRuntime(self.server.url, timeout=10)
        self.assertEqual(rt.json_call("sys", "hi", {"type": "object"}), {})

    def test_text_call_has_no_schema(self):
        self.assertIn("actions", self.rt.text_call("sys", "hi"))
        self.assertNotIn("response_format", self.server.requests[-1])

    def test_sessions_get_their_own_slots_and_keep_history(self):
        parser, voice = self.rt.parser("You convert things"), self.rt.parser("You are Thirties")
        self.assertEqual((parser.slot, voice.slot), (0, 1))  # slot 2 stays free for fresh calls
        parser.ask("one")
        parser.ask("two")
        msgs = self.server.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[-1]["content"], "two")
        self.assertEqual(self.server.requests[-1]["id_slot"], 0)
        self.assertEqual(self.rt.calls[-1]["kind"], "parse")

    def test_session_restarts_before_the_context_fills(self):
        session = LlamaParserSession(self.rt, "sys", budget_chars=1000)
        session.ask("x" * 300)
        session.ask("y" * 300)  # 300 + 30ish + 300 + 600 > 1000: history starts over
        self.assertEqual(len(self.server.requests[-1]["messages"]), 2)

    def test_one_slot_means_no_pinning(self):
        rt = LlamaServerRuntime(self.server.url, slots=1, timeout=10)
        rt.parser("sys").ask("hi")
        self.assertNotIn("id_slot", self.server.requests[-1])

    def test_errors_raise_and_health_reports(self):
        self.assertTrue(self.rt.healthy())
        self.server.close()
        self.server = FakeLlamaServer(reply=lambda body: None)
        rt = LlamaServerRuntime(self.server.url, timeout=10)
        with self.assertRaises(LlamaServerError):
            rt.text_call("sys", "hi")
        self.assertFalse(LlamaServerRuntime("http://127.0.0.1:9", timeout=1).healthy())


class SpeedProbeTest(unittest.TestCase):
    def test_probe_times_the_brains_call_pattern(self):
        server = FakeLlamaServer()
        try:
            rt = LlamaServerRuntime(server.url, slots=3, timeout=10, load_seconds=2.5)
            ticks = iter(range(1000))
            result = probe(rt, clock=lambda: float(next(ticks)))
        finally:
            server.close()
        self.assertEqual(result["messages"], len(PROBE_MESSAGES))
        self.assertEqual(result["parsed"], len(PROBE_MESSAGES))
        self.assertEqual(result["message_seconds_median"], 2.0)  # one tick to parse, one for the voice pass
        self.assertTrue(result["json_call_ok"])
        self.assertEqual(result["load_seconds"], 2.5)
        kinds = [c["kind"] for c in rt.calls]
        self.assertEqual(kinds.count("parse"), len(PROBE_MESSAGES) + 1)  # plus the warm-up
        self.assertEqual(kinds.count("json"), 1)

    def test_describe(self):
        self.assertEqual(describe({"message_seconds_median": 0.9, "load_seconds": 2, "warm_seconds": 1}),
                         "Replies take under a second each, after about 3 s to start the model.")
        self.assertEqual(describe({"message_seconds_median": 12.7, "load_seconds": 10, "warm_seconds": 7.1}),
                         "Replies take about 13 seconds each, after about 17 s to start the model.")


class HarnessHelpersTest(unittest.TestCase):
    def test_pick_device_prefers_the_discrete_gpu(self):
        from tests.model_spike import pick_device
        devices = [{"id": "Vulkan0", "name": "Intel(R) Iris(R) Xe Graphics (ADL GT2)", "total_mib": 48095},
                   {"id": "Vulkan1", "name": "NVIDIA GeForce RTX 3080 Ti Laptop GPU", "total_mib": 16384}]
        self.assertEqual(pick_device(devices)["id"], "Vulkan1")
        self.assertEqual(pick_device(devices[:1])["id"], "Vulkan0")
        self.assertIsNone(pick_device([]))

    def test_ram_cap_wraps_the_server_in_a_scope(self):
        from pathlib import Path
        from tests.model_spike import LlamaServer
        server = LlamaServer(Path("/m.gguf"), ram_gb=6, binary="/bin/true")
        prefix = server.ram_limit()
        if prefix:  # systemd-run present
            self.assertIn("MemoryMax=6144M", prefix)
        self.assertEqual(LlamaServer(Path("/m.gguf"), binary="/bin/true").ram_limit(), [])


if __name__ == "__main__":
    unittest.main()
