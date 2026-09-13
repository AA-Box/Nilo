"""The layering rules of docs/robot-architecture.md Sect. 7, checked mechanically.

Two halves:

* ``robot/`` must import ``core/`` lazily and must be importable in a process that has no
  config file, no ``NILO_CONFIG`` and no loguru configured — ``config/logger.py`` imports
  ``robot.__version__``, so an expensive or config-dependent robot import is paid by every
  process that logs.
* The **import table** of robot-architecture Sect. 7 is enforced over the source, by
  parsing every module's imports. This is what keeps safety *below* behaviour and
  personality: personality may influence which action is chosen, never whether it is
  allowed, and a module that cannot import a layer cannot be talked into consulting it.

The table is checked by reading the code rather than by importing it, so a module that
only imports something lazily inside a function is still caught at its module scope and a
cycle cannot hide behind an import that happens not to run in the test.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROBOT_ROOT = PROJECT_ROOT / "robot"

ROBOT_MODULES = (
    "robot",
    "robot.protocol",
    "robot.state",
    "robot.events",
    "robot.devices",
    "robot.behavior",
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


# -- the import table ----------------------------------------------------------------------

#: Packages under ``robot/`` that each layer must never import, from
#: docs/robot-architecture.md Sect. 7. Only the "may NOT import" column is expressed:
#: the positive column is advisory, the negative one is the invariant.
FORBIDDEN: dict[str, frozenset[str]] = {
    "protocol": frozenset({"events", "state", "safety", "actions", "devices", "behavior",
                           "personality", "vision", "memory", "simulator"}),
    "events": frozenset({"actions", "behavior", "devices", "personality", "simulator"}),
    "state": frozenset({"actions", "behavior", "devices", "personality", "safety", "simulator"}),
    "safety": frozenset({"actions", "behavior", "personality", "devices", "simulator"}),
    "actions": frozenset({"behavior", "personality", "simulator"}),
    "behavior": frozenset({"devices", "simulator"}),
    "devices": frozenset({"actions", "behavior", "personality", "safety", "simulator"}),
    "simulator": frozenset({"actions", "behavior", "devices", "events", "personality",
                            "safety", "state"}),
}


def robot_modules(package: str) -> list[Path]:
    root = ROBOT_ROOT / package
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def imported_robot_packages(path: Path) -> set[str]:
    """Every ``robot.<package>`` this file imports, at module scope or inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if len(parts) >= 2 and parts[0] == "robot":
                found.add(parts[1])
    return found


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_a_layer_never_imports_one_below_or_beside_it(package: str) -> None:
    banned = FORBIDDEN[package]
    for path in robot_modules(package):
        offending = imported_robot_packages(path) & banned
        assert not offending, (
            f"{path.relative_to(PROJECT_ROOT)} imports robot.{sorted(offending)[0]}, which "
            f"robot/{package} may not import (docs/robot-architecture.md Sect. 7)"
        )


def test_the_safety_package_is_self_contained_enough_to_be_trusted() -> None:
    """Phase 3 acceptance, stated on its own because it is the load-bearing one.

    Safety imports the state vocabulary and nothing else from the subsystem. It cannot
    reach the action machinery it judges, and it cannot reach behaviour or personality at
    all — so no layer above it can weaken it, by import or by monkey-patching something it
    holds a reference to.
    """
    imported: set[str] = set()
    for path in robot_modules("safety"):
        imported |= imported_robot_packages(path)
    assert imported <= {"state", "safety"}, sorted(imported)


def test_no_robot_module_imports_core_at_module_scope() -> None:
    """``config/logger.py`` imports ``robot.__version__``, so an eager ``core`` import
    under ``robot/`` would be a cycle paid by every process that logs.

    ``robot/devices`` and ``robot/session.py`` are the one seam allowed to touch ``core``,
    and only lazily — inside a function body, which this check distinguishes.
    """
    offenders: list[str] = []
    for path in sorted(ROBOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module scope only
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] in {"core", "plugins", "plugins_func"} for name in names):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, offenders
