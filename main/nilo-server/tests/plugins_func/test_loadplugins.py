"""Test for the auto-import mechanism in plugins_func/loadplugins.py.

We import a real subpackage (`plugins_func.functions`) and verify the
import side-effect runs. This protects against accidentally deleting
`auto_import_modules` from `app.py` startup code.

Importing `plugins_func.functions` needs the user config (tests/conftest.py sets NILO_CONFIG)
and the full runtime dependencies (opuslib_next, numpy), so it is skipped on the dev-only slice.
"""
import pytest

pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from plugins_func.functions import get_time  # noqa: F401, E402


def test_functions_package_importable():
    """If this test runs, plugins_func.functions.__init__.py has been imported."""
    import plugins_func.functions as fns
    assert hasattr(fns, "get_time")