"""Serve Gemma (LiteRT-LM) to a Thirties client in a sandbox, one JSON request per line.

The desktop app runs as a Flatpak, which has no LiteRT-LM or GPU stack inside.
It starts this script on the host (with ``flatpak-spawn --host`` and the host's
LiteRT venv) and talks to it over stdin/stdout; see ``HostGemmaRuntime``. The
script is standalone on purpose: the host can't import the app's packages.

    python gemma_server.py MODEL_PATH

Requests: {"op": "open", "sid", "system"} | {"op": "send", "sid", "user"} | {"op": "close", "sid"}
| {"op": "json", "system", "user", "schema", "max_output_tokens"} | {"op": "text", "system", "user",
"max_output_tokens"} | {"op": "quit"}. Each gets one reply line; failures reply {"error": "..."}.
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    # Native libraries may print to stdout; keep the protocol on its own descriptor.
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)

    def reply(obj) -> None:
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    try:
        import litert_lm as lm
    except ImportError as e:
        reply({"error": f"litert-lm-api is not installed for {sys.executable}: {e}"})
        return
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thirties" / "litert"
    cache_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    try:
        engine = lm.Engine(sys.argv[1], backend=lm.Backend.GPU(), max_num_tokens=8192, cache_dir=str(cache_dir))
        backend = "gpu"
    except Exception:
        engine = lm.Engine(sys.argv[1], backend=lm.Backend.CPU(), max_num_tokens=8192, cache_dir=str(cache_dir))
        backend = "cpu"
    reply({"ready": True, "backend": backend, "load_seconds": round(time.time() - start, 1)})

    def text_of(message) -> str:
        if isinstance(message, dict):
            return "".join(p.get("text", "") for p in message.get("content", []) if isinstance(p, dict))
        return str(message)

    conversations = {}
    for line in sys.stdin:
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "quit":
                break
            if op == "open":
                old = conversations.pop(req["sid"], None)
                if old is not None:
                    old.close()
                conversations[req["sid"]] = engine.create_conversation(
                    system_message=req["system"], sampler_config=lm.SamplerConfig(temperature=0.0, top_k=1))
                reply({"ok": True})
            elif op == "send":
                reply({"out": text_of(conversations[req["sid"]].send_message(req["user"])).strip()})
            elif op == "close":
                conv = conversations.pop(req["sid"], None)
                if conv is not None:
                    conv.close()
                reply({"ok": True})
            elif op == "json":
                conv = engine.create_conversation(
                    system_message=req["system"],
                    constrained_decoding_config=lm.ConstrainedDecodingConfig(
                        enable=True, provider=lm.LiteRtLmConstraintProviderType.LL_GUIDANCE),
                    sampler_config=lm.SamplerConfig(temperature=0.0, top_k=1),
                    max_output_tokens=req.get("max_output_tokens", 256))
                try:
                    reply({"raw": text_of(conv.send_message(req["user"], response_format=lm.ResponseFormat.json(req["schema"])))})
                finally:
                    conv.close()
            elif op == "text":
                conv = engine.create_conversation(
                    system_message=req["system"], sampler_config=lm.SamplerConfig(temperature=0.3, top_k=20),
                    max_output_tokens=req.get("max_output_tokens", 120))
                try:
                    reply({"out": text_of(conv.send_message(req["user"])).strip()})
                finally:
                    conv.close()
            else:
                reply({"error": f"unknown op {op!r}"})
        except Exception as e:
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
