"""The layering rules of docs/robot-architecture.md Sect. 7, checked mechanically.

``robot/`` must import ``core/`` lazily and must be importable in a process that has no
config file, no ``NILO_CONFIG`` and no loguru configured — ``config/logger.py`` imports
``robot.__version__``, so an expensive or config-dependent robot import is paid by every
process that logs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ROBOT_MODULES = (
    "robot",
    "robot.protocol",
    "robot.state",
    "robot.events",
    "robot.devices",
    "robot.runtime",
    "robot.session",
)


def run_isolated(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "NILO_CONFIG"}
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,  # not the server directory: no config.yaml, no data/.config.yaml
        env=env,
        capture_output=True,
        text=True,
    )


def test_robot_imports_without_a_config_file(tmp_path):
    imports = "; ".join(f"import {name}" for name in ROBOT_MODULES)
    result = run_isolated(imports, tmp_path)
    assert result.returncode == 0, result.stderr


def test_importing_robot_does_not_pull_in_the_inherited_server(tmp_path):
    source = (
        "; ".join(f"import {name}" for name in ROBOT_MODULES)
        + "; import sys"
        + "; leaked = sorted(m for m in sys.modules if m.split('.')[0] in {'core', 'config', 'plugins'})"
        + "; assert not leaked, leaked"
    )
    result = run_isolated(source, tmp_path)
    assert result.returncode == 0, result.stderr
