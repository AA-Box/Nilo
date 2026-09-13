"""Tests for plugins/register.py — the unified plugin function registry.

`FunctionRegistry.__init__` called an undefined `setup_logging()`, so the class
raised NameError on instantiation even though it is re-exported from
`plugins_func.register` and `plugins_func.__init__`. These tests pin the
constructor and the register/unregister/lookup contract so the regression
cannot come back silently.
"""
import pytest

from plugins.register import (
    Action,
    ActionResponse,
    FunctionItem,
    FunctionRegistry,
    ToolType,
    all_function_registry,
    module_func_map,
    register_function,
)


def _item(name="demo"):
    return FunctionItem(name, {"type": "function"}, lambda: None, ToolType.WAIT)


def test_registry_is_constructible():
    """Regression: this used to raise NameError: setup_logging."""
    registry = FunctionRegistry()
    assert registry.get_all_functions() == {}


def test_register_function_with_explicit_item():
    registry = FunctionRegistry()
    item = _item()
    assert registry.register_function("demo", item) is item
    assert registry.get_function("demo") is item


def test_register_function_falls_back_to_global_registry():
    registry = FunctionRegistry()
    item = _item("global_demo")
    all_function_registry["global_demo"] = item
    try:
        assert registry.register_function("global_demo") is item
        assert registry.get_function("global_demo") is item
    finally:
        all_function_registry.pop("global_demo", None)


def test_register_unknown_function_returns_none():
    registry = FunctionRegistry()
    assert registry.register_function("nope_not_registered") is None
    assert registry.get_function("nope_not_registered") is None


def test_unregister_function():
    registry = FunctionRegistry()
    registry.register_function("demo", _item())
    assert registry.unregister_function("demo") is True
    assert registry.unregister_function("demo") is False


def test_get_all_function_desc():
    registry = FunctionRegistry()
    registry.register_function("demo", _item())
    assert registry.get_all_function_desc() == [{"type": "function"}]


def test_register_function_decorator_populates_global_registries():
    @register_function("tmp_decorated", {"type": "function"}, ToolType.WAIT)
    def _tmp():
        return None

    try:
        assert "tmp_decorated" in all_function_registry
        # module_func_map maps the defining module's basename -> function names
        assert "tmp_decorated" in module_func_map["test_register"]
    finally:
        all_function_registry.pop("tmp_decorated", None)
        module_func_map.get("test_register", []).remove("tmp_decorated")


@pytest.mark.parametrize(
    "action,code",
    [(Action.ERROR, -1), (Action.NOTFOUND, 0), (Action.NONE, 1), (Action.RESPONSE, 2),
     (Action.REQLLM, 3), (Action.RECORD, 4)],
)
def test_action_codes_are_stable(action, code):
    """Executors branch on these codes; changing them breaks tool dispatch."""
    assert action.code == code


@pytest.mark.parametrize(
    "tool_type,code",
    [(ToolType.NONE, 1), (ToolType.WAIT, 2), (ToolType.CHANGE_SYS_PROMPT, 3),
     (ToolType.SYSTEM_CTL, 4), (ToolType.IOT_CTL, 5), (ToolType.MCP_CLIENT, 6)],
)
def test_tool_type_codes_are_stable(tool_type, code):
    """ServerPluginExecutor.execute() dispatches on .code == 4 / 5 / 2 / 3."""
    assert tool_type.code == code


def test_action_response_defaults():
    resp = ActionResponse(Action.NONE)
    assert resp.action is Action.NONE
    assert resp.result is None
    assert resp.response is None
