"""Pytest configuration — must run BEFORE any test imports project modules.

* Adds the nilo-server directory to sys.path so the implicit top-level imports
  (``from core.utils.textUtils import ...``) resolve.
* Points ``NILO_CONFIG`` at a committed, empty override file so the config and
  logging machinery works without touching the developer's ``data/.config.yaml``.
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("NILO_CONFIG", str(_PROJECT_ROOT / "tests" / "fixtures" / "test_config.yaml"))


# -- TEMPORARY CI DIAGNOSTIC (remove once the runner cancellation is understood) ----------
#
# The full-dependency CI job is cancelled mid-run with no reason attached, always inside
# the integration tests. Nothing in the pytest output says why, so this prints one line per
# test with the resources the process is holding. Unbuffered, so the last line before the
# runner dies is the test that was running when it died.
import resource  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def _open_fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def pytest_runtest_logstart(nodeid, location):
    print(
        f"[trace] start rss={_rss_mb():7.1f}MB threads={threading.active_count():3d} "
        f"fds={_open_fds():4d} t={time.monotonic():8.1f} {nodeid}",
        file=sys.stderr,
        flush=True,
    )
