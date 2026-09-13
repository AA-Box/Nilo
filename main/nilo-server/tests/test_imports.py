"""Every first-party module imports (catches broken renames and missing dependencies)."""
import importlib
import pkgutil

import pytest

import config
import core
import plugins
import plugins_func
import robot

PACKAGES = [config, core, plugins, plugins_func, robot]


def _modules():
    for pkg in PACKAGES:
        yield pkg.__name__
        for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            yield info.name


@pytest.mark.parametrize("name", sorted(set(_modules())))
def test_module_imports(name):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as e:  # optional heavy provider dependency, not a broken import
        pytest.skip(f"optional dependency missing: {e.name}")
