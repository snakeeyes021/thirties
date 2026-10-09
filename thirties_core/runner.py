"""Standalone inference runner for LiteRT-LM models on host."""

import argparse
import os
import sys
from pathlib import Path


def find_model() -> str:
    username = os.environ.get("USER", "matt")
    candidates = [
        Path(f"/var/home/{username}/.local/share/thirties/models/gemma-4-E4B-it-gpu.litertlm"),
        Path(f"/var/home/{username}/.local/share/thirties/models/gemma-4-e4b.litertlm"),
        Path.home() / ".local" / "share" / "thirties" / "models" / "gemma-4-E4B-it-gpu.litertlm",
        Path.home() / ".local" / "share" / "thirties" / "models" / "gemma-4-e4b.litertlm",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=str, required=True)
    parser.add_argument("--model", type=str, default="")
    args = parser.parse_args()

    model_path = args.model or find_model()
    if not model_path:
        print("ERROR: Model bundle not found", file=sys.stderr)
        sys.exit(1)

    import litert_lm

    # Initialize GPU engine with fallback to CPU
    try:
        engine = litert_lm.Engine(model_path, backend=litert_lm.Backend.GPU())
    except Exception:
        engine = litert_lm.Engine(model_path, backend=litert_lm.Backend.CPU())

    conv = engine.create_conversation()
    reply = conv.send_message(args.prompt)

    print("---THIRTIES_RESPONSE_START---")
    print(reply)
    print("---THIRTIES_RESPONSE_END---")


if __name__ == "__main__":
    main()
