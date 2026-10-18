
import os

# Tests and evals never fetch live data (the weather): it's slow, and simulated days aren't today.
os.environ.setdefault("THIRTIES_OFFLINE", "1")
