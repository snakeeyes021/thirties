"""First-run model download: resume, cancel and checksum, against a local HTTP server."""

import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from thirties_core.brains.slm.model_download import DownloadCancelled, ModelSpec, download_model

DATA = bytes(range(256)) * 20000  # ~5 MB


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(DATA) - start))
        self.end_headers()
        self.wfile.write(DATA[start:])

    def log_message(self, *_):
        pass


class TestModelDownload(unittest.TestCase):

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.dir = Path(tempfile.mkdtemp())
        self.spec = ModelSpec("m.litertlm", f"http://127.0.0.1:{self.server.server_port}/m",
                              len(DATA), hashlib.sha256(DATA).hexdigest())

    def test_cancel_then_resume_then_verify(self):
        cancel = threading.Event()
        seen = []

        def progress(done, total):
            seen.append(done)
            if done > len(DATA) // 3:
                cancel.set()
        with self.assertRaises(DownloadCancelled):
            download_model(self.spec, progress, cancel, self.dir)
        partial = (self.dir / "m.litertlm.part").stat().st_size
        self.assertTrue(0 < partial < len(DATA))
        path = download_model(self.spec, dest_dir=self.dir)
        self.assertEqual(path.read_bytes(), DATA)
        self.assertFalse((self.dir / "m.litertlm.part").exists())
        self.assertEqual(download_model(self.spec, dest_dir=self.dir), path)  # already there: no download

    def test_corrupt_download_is_removed(self):
        bad = ModelSpec(self.spec.filename, self.spec.url, self.spec.size, "0" * 64)
        with self.assertRaises(IOError):
            download_model(bad, dest_dir=self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
