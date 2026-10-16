"""The host-served Gemma runtime (for the Flatpak), against a stand-in LiteRT-LM package."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

FAKE_LITERT = '''
print("native library noise on stdout")  # must not corrupt the protocol


class _Conversation:
    def __init__(self, system):
        self.system = system

    def send_message(self, user, response_format=None):
        text = '{"actions": [{"type": "chat"}]}' if response_format is not None else f"{self.system}|{user}"
        return {"content": [{"text": text}]}

    def close(self):
        pass


class Engine:
    def __init__(self, path, backend=None, max_num_tokens=0, cache_dir=""):
        print("loading", path)

    def create_conversation(self, system_message="", **_):
        return _Conversation(system_message)


class Backend:
    GPU = staticmethod(lambda: "gpu")
    CPU = staticmethod(lambda: "cpu")


def SamplerConfig(**kw):
    return kw


def ConstrainedDecodingConfig(**kw):
    return kw


class LiteRtLmConstraintProviderType:
    LL_GUIDANCE = 1


class ResponseFormat:
    @staticmethod
    def json(schema):
        return {"schema": schema}
'''


class TestHostGemmaRuntime(unittest.TestCase):

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="thirties_host_"))
        (root / "pkgs" / "litert_lm").mkdir(parents=True)
        (root / "pkgs" / "litert_lm" / "__init__.py").write_text(FAKE_LITERT)
        venv = root / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("")
        os.symlink(sys.executable, venv / "bin" / "python")
        self.model = root / "gemma.litertlm"
        self.model.write_text("")
        for p in (patch.dict(os.environ, {"PYTHONPATH": str(root / "pkgs"), "XDG_CACHE_HOME": str(root / "cache")}),
                  patch("thirties_core.brains.slm.runtime.HOST_VENV", venv),
                  patch("thirties_core.brains.slm.runtime.SERVER_COPY", root / "gemma_server.py")):
            p.start()
            self.addCleanup(p.stop)

    def test_sessions_and_calls_over_the_pipe(self) -> None:
        from thirties_core.brains.slm.runtime import HostGemmaRuntime
        rt = HostGemmaRuntime(self.model)
        self.addCleanup(rt.close)
        self.assertEqual(rt.backend, "gpu")
        session = rt.parser("SYS")
        self.assertEqual(session.ask("hello"), "SYS|hello")
        self.assertEqual(session.ask("again"), "SYS|again")
        session.close()
        self.assertEqual(rt.json_call("s", "u", {"type": "object"}), {"actions": [{"type": "chat"}]})
        self.assertEqual(rt.text_call("T", "hi"), "T|hi")
        self.assertEqual([c["kind"] for c in rt.calls][-2:], ["json", "text"])
        with self.assertRaises(RuntimeError):
            rt.send(999, "no such session")  # an error on the host comes back as an exception


if __name__ == "__main__":
    unittest.main()
