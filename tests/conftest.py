import sys
import os
from pathlib import Path

from hypothesis import settings

_REPO_ROOT = Path(__file__).parent.parent

# Add the superlink package to path so tests can `import superlink.*`
sys.path.insert(0, str(_REPO_ROOT / "tools" / "sx1302"))
# Add the repo root so tests can `from tests.fixtures.captured_frames import ...`
sys.path.insert(0, str(_REPO_ROOT))

# Keep wall-clock performance out of protocol correctness checks. The session
# tests advance an explicit virtual clock and never sleep or access hardware.
settings.register_profile("dev", max_examples=50, stateful_step_count=30, deadline=None)
settings.register_profile("ci", max_examples=100, stateful_step_count=50,
                          deadline=None, derandomize=True)
settings.register_profile("extended", max_examples=500, stateful_step_count=100,
                          deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
