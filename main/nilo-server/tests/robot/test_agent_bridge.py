"""Registration into the inherited tool system, and the collision it has to catch.

The inherited namespace is flat and the device-MCP executor is registered *after* the
server-plugin one, so a device that publishes its own ``robot_move`` replaces the guarded
one with nothing worse than a logged warning
(``core/providers/tools/unified_tool_manager.py``). That is the failure this file exists
to make loud.

Nothing here needs a socket, a model, or the audio stack: ``plugins/register.py`` is pure
Python and the bridge imports the rest of ``core`` lazily.
"""
from __future__ import annotations

import pytest

from robot.agent.bridge import detect_collisions, register_robot_tools
from robot.agent.tools import TOOL_NAMES
from robot.devices.mcp import sanitize_tool_name
from robot.state.models import RobotCapabilities, RobotTool
from tests.robot.conftest import robot_tools


@pytest.fixture(autouse=True)
def registry():
    """The inherited registry is process-wide; leave it as it was found."""
    from plugins.register import all_function_registry

    before = dict(all_function_registry)
    try:
        yield all_function_registry
    finally:
        all_function_registry.clear()
        all_function_registry.update(before)


def test_every_robot_tool_registers_as_an_iot_control_plugin(registry):
    """``IOT_CTL`` is the only type the server exposes to the model with no config edit."""
    from plugins.register import ToolType

    register_robot_tools(force=True)

    for name in TOOL_NAMES:
        assert name in registry, f"{name} was not registered"
        assert registry[name].type is ToolType.IOT_CTL


def test_registration_is_idempotent(registry):
    register_robot_tools(force=True)
    first = dict(registry)
    register_robot_tools()
    assert dict(registry) == first


def test_a_registered_handler_is_a_coroutine_function(registry):
    """``ServerPluginExecutor.execute`` awaits a coroutine result; a blocking handler
    would run the whole tool call on the chat loop's worker thread."""
    import inspect

    register_robot_tools(force=True)
    for name in TOOL_NAMES:
        assert inspect.iscoroutinefunction(registry[name].func)


def test_the_registered_description_is_the_tool_schema(registry):
    register_robot_tools(force=True)
    description = registry["robot_move"].description
    assert description["function"]["name"] == "robot_move"
    assert "distance_mm" in description["function"]["parameters"]["properties"]


async def test_a_handler_on_a_session_with_no_robot_says_so(registry):
    from plugins.register import Action

    register_robot_tools(force=True)

    class Bare:
        pass

    response = await registry["robot_move"].func(Bare(), distance_mm=100)
    assert response.action is Action.ERROR
    assert "not attached to a robot" in response.response


# -- collisions -----------------------------------------------------------------------------------


def test_a_device_tool_that_shadows_a_guarded_tool_is_detected():
    capabilities = RobotCapabilities(
        mcp=True,
        tools=(
            RobotTool(name="robot_move", raw_name="robot_move", description="a device tool"),
            RobotTool(name="lamp_on", raw_name="lamp.on", description="a lamp"),
        ),
    )
    assert detect_collisions(capabilities) == ("robot_move",)


def test_the_firmware_vocabulary_does_not_collide_with_the_guarded_tools():
    """Firmware publishes ``robot.motion.move``, which sanitizes to ``robot_motion_move``.
    The guarded tool is ``robot_move``. They must not be the same name."""
    capabilities = RobotCapabilities(mcp=True, tools=robot_tools())
    assert detect_collisions(capabilities) == ()
    assert sanitize_tool_name("robot.motion.move") not in TOOL_NAMES


def test_a_robot_with_no_tools_collides_with_nothing():
    assert detect_collisions(RobotCapabilities()) == ()
