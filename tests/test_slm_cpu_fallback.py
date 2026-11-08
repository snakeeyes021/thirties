"""The SLM runtime with no usable GPU: LiteRT's "-gpu" builds don't run on the CPU backend (11-04)."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from thirties_core.brains.slm.runtime import LiteRtRuntime, SlmUnavailable, cpu_build


def fake_litert(gpu_works: bool = False):
    """A stand-in for litert_lm whose GPU backend fails (no usable GPU) and whose CPU backend, like the real one,
    can't open a "-gpu" build."""
    lm = types.ModuleType("litert_lm")
    opened = []

    class Backend:
        class GPU:
            pass

        class CPU:
            pass

    class Engine:
        def __init__(self, path, backend=None, max_num_tokens=None, cache_dir=None):
            if isinstance(backend, Backend.GPU) and not gpu_works:
                raise RuntimeError("no usable GPU")
            if isinstance(backend, Backend.CPU) and path.endswith("-gpu.litertlm"):
                raise RuntimeError(f"Failed to create LiteRT-LM engine for {path}")
            opened.append((path, type(backend).__name__))

    lm.Backend, lm.Engine, lm.opened = Backend, Engine, opened
    return lm


class CpuFallbackTest(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.models = Path(self.dir.name)
        self.gpu_build = self.models / "gemma-4-E4B-it-gpu.litertlm"
        self.gpu_build.write_bytes(b"")
        env = mock.patch.dict("os.environ", {"XDG_CACHE_HOME": str(self.models / "cache")})
        env.start()
        self.addCleanup(env.stop)

    def load(self, lm):
        with mock.patch.dict(sys.modules, {"litert_lm": lm}):
            return LiteRtRuntime(self.gpu_build)

    def test_no_gpu_uses_the_generic_build_beside_it(self) -> None:
        (self.models / "gemma-4-E4B-it.litertlm").write_bytes(b"")
        lm = fake_litert()
        rt = self.load(lm)
        self.assertEqual(rt.device, "cpu")
        self.assertEqual(rt.model_path.name, "gemma-4-E4B-it.litertlm")
        self.assertEqual(lm.opened, [(str(self.models / "gemma-4-E4B-it.litertlm"), "CPU")])

    def test_no_gpu_and_no_generic_build_says_why(self) -> None:
        with self.assertRaises(SlmUnavailable) as caught:
            self.load(fake_litert())
        self.assertIn("only runs on a GPU", str(caught.exception))
        self.assertIn("gemma-4-E4B-it.litertlm", str(caught.exception))

    def test_with_a_gpu_the_gpu_build_is_used(self) -> None:
        (self.models / "gemma-4-E4B-it.litertlm").write_bytes(b"")
        lm = fake_litert(gpu_works=True)
        rt = self.load(lm)
        self.assertEqual((rt.device, rt.model_path), ("gpu", self.gpu_build))

    def test_cpu_build_of_a_generic_file_is_itself(self) -> None:
        generic = self.models / "gemma-4-E2B-it.litertlm"
        self.assertEqual(cpu_build(generic), generic)


if __name__ == "__main__":
    unittest.main()
