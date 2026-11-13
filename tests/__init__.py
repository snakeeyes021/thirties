
import os
import sys

# Unit tests never reach a real model: the agy CLI is never found (unless a test patches it in, mocked), and there's
# no Gemini API key. A test that builds the real app core used to run the user's signed-in agy. The live evals in this
# package (run as their own modules, not under unittest) are unaffected; THIRTIES_LIVE_MODELS=1 lifts this.
if getattr(sys.modules.get("__main__"), "__package__", None) == "unittest" \
        and os.environ.get("THIRTIES_LIVE_MODELS") != "1":
    from unittest import mock as _mock
    import thirties_core.inference as _inference
    os.environ.pop("GEMINI_API_KEY", None)
    _mock.patch.object(_inference, "find_agy_binary", lambda *_a, **_k: None).start()

# Tests and evals never fetch live data (the weather): it's slow, and simulated days aren't today.
os.environ.setdefault("THIRTIES_OFFLINE", "1")

# Their own state folder (the on-device model's marker and events, brains/slm/fit.py): a test run never reads or
# clears the real app's.
if "THIRTIES_TEST_STATE" not in os.environ:
    import atexit
    import shutil
    import tempfile
    os.environ["THIRTIES_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="thirties_state_")
    atexit.register(shutil.rmtree, os.environ["THIRTIES_TEST_STATE"], True)
