
import os

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
