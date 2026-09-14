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

