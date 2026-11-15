import os
import sys

# A test run writes only to temporary folders (tests/hygiene.py): first, before anything imports thirties_core and
# builds a path from the home folder, HOME and the XDG folders become a temporary home; and the run fails if it leaves
# anything new in the repo tree.
from tests import hygiene as _hygiene

if _hygiene.under_test_runner():
    _hygiene.guard_repo()
    _hygiene.isolate()

# Unit tests never reach a real model: the agy CLI is never found (unless a test patches it in, mocked), and there's
# no Gemini API key. A test that builds the real app core used to run the user's signed-in agy. The live evals in this
# package (run as their own modules, not under unittest) are unaffected; THIRTIES_LIVE_MODELS=1 lifts this.
_UNIT_RUN = (getattr(sys.modules.get("__main__"), "__package__", None) == "unittest" or "pytest" in sys.modules) \
    and os.environ.get("THIRTIES_LIVE_MODELS") != "1"
if _UNIT_RUN:
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

# And their own agy ledger: a test's mocked agy call (a fake "ok") was written into the user's real
# ~/.local/share/thirties/agy_calls.log whenever tests ran outside tests/headless.py, and the evals pace their
# calls by that file (11-15: a stray default-model "ok" every few minutes, with no agy run behind it).
# It lives in the run's temporary home when there is one (tests/hygiene.py), else in the run's state folder.
if _UNIT_RUN:
    from pathlib import Path as _Path
    _home = os.environ.get("THIRTIES_TEST_HOME")
    _ledger = (_Path(_home) / ".local" / "share" / "thirties" if _home else _Path(os.environ["THIRTIES_TEST_STATE"])) \
        / "agy_calls.log"
    _mock.patch.object(_inference, "AGY_LEDGER", _ledger).start()
