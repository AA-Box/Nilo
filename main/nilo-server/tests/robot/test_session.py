"""End to end over fake transports: connect, discover, call a tool, disconnect.

Two paths are covered, because the subsystem has two:

* the runtime driving a :class:`RobotToolClient` over a fake MCP device, which is what a
  robot-owned channel and the simulator use;
* the session seam (``robot/session.py``) driving the *inherited* device-MCP client on a
  fake ConnectionHandler, which is what a real device connection uses today.

Nothing here opens a socket or imports the heavy runtime dependencies.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from robot.devices import McpError, RobotToolClient
from robot.events import (
    CapabilitiesRefreshed,
    RobotConnected,
    RobotDisconnected,
    RobotEvent,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallStarted,
    ToolDiscovered,
)
from robot.runtime import RobotRuntime, UnknownRobotError, get_runtime, set_runtime
from robot.session import ROBOT_ATTR, ConnectionToolChannel, attach_connection, detach_connection
from robot.state import DisconnectReason
from tests.robot.conftest import FakeConnection, FakeDeviceMcpClient, FakeMcpDevice, device_info, tool_definition

ROBOT_ID = "aa-bb-cc-dd-ee-ff"


def channel_for(device: FakeMcpDevice, **kwargs) -> RobotToolClient:
    client = RobotToolClient(device, robot_id=ROBOT_ID, features={"mcp": True}, **kwargs)
    device.client = client
    return client


async def events_of(runtime: RobotRuntime, kind: type[RobotEvent]) -> list[RobotEvent]:
    await runtime.events.drain()
    return [event for event in _recorded[id(runtime)] if isinstance(event, kind)]


_recorded: dict[int, list[RobotEvent]] = {}


@pytest.fixture
async def recording(runtime):
    """Record every event the runtime publishes, for assertions after the fact."""
    _recorded[id(runtime)] = []
    runtime.events.subscribe(RobotEvent, _recorded[id(runtime)].append)
    yield runtime
    _recorded.pop(id(runtime), None)


async def test_a_connecting_robot_is_registered_and_its_tools_discovered(recording):
    runtime = recording
    device = FakeMcpDevice([tool_definition("self.battery"), tool_definition("robot.move", distance_cm=1)])

    state = await runtime.attach(device_info(features={"mcp": True}), channel_for(device))
    await runtime.wait_for_discovery(state.robot_id)

    assert state.robot_id == ROBOT_ID
    assert [robot.robot_id for robot in await runtime.registry.list()] == [ROBOT_ID]

    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None
    assert capabilities.mcp
    assert capabilities.tool_names == ("self_battery", "robot_move")

    assert len(await events_of(runtime, RobotConnected)) == 1
    assert [event.tool.name for event in await events_of(runtime, ToolDiscovered)] == ["self_battery", "robot_move"]
    assert len(await events_of(runtime, CapabilitiesRefreshed)) == 1


async def test_a_device_with_no_tool_channel_gets_capabilities_from_its_feature_flags(runtime):
    state = await runtime.attach(device_info(features={"mcp": False, "aec": True}))
    capabilities = await runtime.registry.get_capabilities(state.robot_id)
    assert capabilities is not None
    assert not capabilities.mcp
    assert capabilities.features == frozenset({"aec"})
    assert capabilities.tools == ()


async def test_a_silent_device_leaves_the_robot_registered_without_capabilities(runtime):
    device = FakeMcpDevice(answer=False)
    state = await runtime.attach(device_info(), channel_for(device, request_timeout=0.05))
    await runtime.wait_for_discovery(state.robot_id)

    assert await runtime.registry.get(ROBOT_ID) is not None
    assert await runtime.registry.get_capabilities(ROBOT_ID) is None


async def test_discovery_that_overruns_the_budget_is_abandoned():
    runtime = RobotRuntime(discovery_timeout=0.05)
    try:
        device = FakeMcpDevice([tool_definition("self.battery")], latency_s=5.0)
        state = await runtime.attach(device_info(), channel_for(device, request_timeout=5.0))
        await runtime.wait_for_discovery(state.robot_id)
        assert await runtime.registry.get_capabilities(ROBOT_ID) is None
    finally:
        await runtime.aclose()


async def test_calling_a_tool_announces_start_and_completion(recording):
    runtime = recording
    device = FakeMcpDevice([tool_definition("robot.move", distance_cm=1)])
    state = await runtime.attach(device_info(), channel_for(device))
    await runtime.wait_for_discovery(state.robot_id)

    result = await runtime.call_tool(ROBOT_ID, "robot_move", {"distance_cm": 30})

    assert result == "ok:robot_move"
    assert device.calls == [("robot_move", {"distance_cm": 30})]
    started = await events_of(runtime, ToolCallStarted)
    completed = await events_of(runtime, ToolCallCompleted)
    assert [event.tool_name for event in started] == ["robot_move"]
    assert completed[0].call_id == started[0].call_id
    assert completed[0].result == "ok:robot_move"


async def test_a_failing_tool_call_is_announced_and_raised(recording):
    runtime = recording
    device = FakeMcpDevice([tool_definition("robot.move")], call_error="cliff detected")
    state = await runtime.attach(device_info(), channel_for(device))
    await runtime.wait_for_discovery(state.robot_id)

    with pytest.raises(McpError, match="cliff detected"):
        await runtime.call_tool(ROBOT_ID, "robot_move")

    failed = await events_of(runtime, ToolCallFailed)
    assert len(failed) == 1
    assert "cliff detected" in failed[0].error


async def test_a_tool_the_robot_never_published_is_refused_before_it_reaches_the_device(runtime):
    device = FakeMcpDevice([tool_definition("self.battery")])
    state = await runtime.attach(device_info(), channel_for(device))
    await runtime.wait_for_discovery(state.robot_id)

    with pytest.raises(McpError, match="does not publish"):
        await runtime.call_tool(ROBOT_ID, "robot_move")
    assert device.calls == []

    with pytest.raises(UnknownRobotError):
        await runtime.call_tool("nobody", "robot_move")


async def test_disconnecting_cleans_the_robot_up(recording):
    runtime = recording
    device = FakeMcpDevice([tool_definition("self.battery")])
    info = device_info()
    state = await runtime.attach(info, channel_for(device))
    await runtime.wait_for_discovery(state.robot_id)

    assert await runtime.detach(ROBOT_ID, session_id=info.session_id)

    assert await runtime.registry.list() == []
    assert await runtime.registry.get_capabilities(ROBOT_ID) is None
    assert runtime.channel(ROBOT_ID) is None
    assert not await runtime.detach(ROBOT_ID, session_id=info.session_id)
    assert [event.reason for event in await events_of(runtime, RobotDisconnected)] == [
        DisconnectReason.CLIENT_CLOSED
    ]


async def test_a_reconnect_rediscovers_capabilities(recording):
    """Firmware may have changed between sessions, so the tool set is discovered again."""
    runtime = recording
    first = FakeMcpDevice([tool_definition("self.battery")])
    state = await runtime.attach(device_info(session_id="s1"), channel_for(first))
    await runtime.wait_for_discovery(state.robot_id)
    await runtime.detach(ROBOT_ID, session_id="s1")

    second = FakeMcpDevice([tool_definition("self.battery"), tool_definition("robot.move")])
    state = await runtime.attach(device_info(session_id="s2"), channel_for(second))
    await runtime.wait_for_discovery(state.robot_id)

    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None and capabilities.tool_names == ("self_battery", "robot_move")
    current = await runtime.registry.get(ROBOT_ID)
    assert current is not None and current.connection.reconnect_count == 1
    assert [event.reconnect for event in await events_of(runtime, RobotConnected)] == [False, True]
    refreshes = await events_of(runtime, CapabilitiesRefreshed)
    assert [event.refreshed for event in refreshes] == [False, True]


async def test_a_duplicate_connection_supersedes_the_first_and_closes_its_channel(runtime):
    first_device = FakeMcpDevice([tool_definition("self.battery")])
    first_channel = channel_for(first_device)
    state = await runtime.attach(device_info(session_id="s1"), first_channel)
    await runtime.wait_for_discovery(state.robot_id)

    second_device = FakeMcpDevice([tool_definition("self.battery")])
    state = await runtime.attach(device_info(session_id="s2"), channel_for(second_device))
    await runtime.wait_for_discovery(state.robot_id)

    assert len(await runtime.registry.list()) == 1
    assert runtime.channel(ROBOT_ID) is not first_channel
    with pytest.raises(McpError, match="closed"):
        await first_channel.call_tool("self_battery")


async def test_a_stale_teardown_does_not_disconnect_the_live_session(runtime):
    await runtime.attach(device_info(session_id="s1"))
    await runtime.attach(device_info(session_id="s2"))

    assert not await runtime.detach(ROBOT_ID, session_id="s1")
    assert len(await runtime.registry.list()) == 1


async def test_closing_the_runtime_shuts_everything_down():
    runtime = RobotRuntime()
    device = FakeMcpDevice([tool_definition("self.battery")])
    await runtime.attach(device_info(), channel_for(device))
    await runtime.aclose()

    assert runtime.closed
    assert runtime.events.closed
    with pytest.raises(RuntimeError):
        await runtime.attach(device_info())


def test_the_process_runtime_is_replaceable_and_rebuilt_after_a_close():
    original = get_runtime()
    assert get_runtime() is original
    replacement = RobotRuntime()
    set_runtime(replacement)
    assert get_runtime() is replacement
    set_runtime(None)
    assert get_runtime() is not replacement


# --- the session seam: a fake ConnectionHandler ------------------------------------


@pytest.fixture
def fake_call_mcp_tool(monkeypatch):
    """Stub out the inherited ``call_mcp_tool`` so no ``core`` import is needed."""
    calls: list[tuple[str, dict, int]] = []

    async def call_mcp_tool(conn, client, tool_name, args="{}", timeout=30):
        calls.append((tool_name, json.loads(args), timeout))
        return f"called:{tool_name}"

    module = types.ModuleType("core.providers.tools.device_mcp.mcp_handler")
    module.call_mcp_tool = call_mcp_tool
    monkeypatch.setitem(sys.modules, "core.providers.tools.device_mcp.mcp_handler", module)
    return calls


def connection_with_tools(**kwargs) -> FakeConnection:
    tools = {
        "self_battery": tool_definition("self.battery"),
        "robot_move": tool_definition("robot.move", distance_cm=1),
    }
    return FakeConnection(features={"mcp": True}, mcp_client=FakeDeviceMcpClient(tools), **kwargs)


async def test_attaching_a_live_session_registers_the_robot_and_discovers_its_tools(runtime):
    conn = connection_with_tools()

    session = await attach_connection(conn, runtime)

    assert session is not None
    assert session.robot_id == ROBOT_ID
    assert getattr(conn, ROBOT_ATTR) is session
    await runtime.wait_for_discovery(ROBOT_ID)

    state = await runtime.registry.get(ROBOT_ID)
    assert state is not None
    assert state.identity.device_id == conn.device_id
    assert state.connection.remote_address == "10.0.0.7"
    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None and capabilities.tool_names == ("self_battery", "robot_move")


async def test_detaching_a_session_unregisters_the_robot_once(runtime):
    conn = connection_with_tools()
    await attach_connection(conn, runtime)
    await runtime.wait_for_discovery(ROBOT_ID)

    assert await detach_connection(conn, runtime)
    assert await runtime.registry.list() == []
    assert not await detach_connection(conn, runtime)
    assert getattr(conn, ROBOT_ATTR) is None


async def test_a_session_with_no_device_id_is_not_a_robot(runtime):
    conn = FakeConnection(device_id="")
    conn.headers = {}
    assert await attach_connection(conn, runtime) is None
    assert await runtime.registry.list() == []
    assert not await detach_connection(conn, runtime)


async def test_attaching_never_raises_into_the_voice_session(runtime):
    class Hostile:
        @property
        def headers(self):
            raise RuntimeError("this connection object is broken")

    assert await attach_connection(Hostile(), runtime) is None


async def test_a_device_that_does_not_speak_mcp_still_becomes_a_robot(runtime):
    conn = FakeConnection(features={"mcp": False})
    await attach_connection(conn, runtime)
    await runtime.wait_for_discovery(ROBOT_ID)

    state = await runtime.registry.get(ROBOT_ID)
    assert state is not None and state.connection.is_connected
    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None and not capabilities.mcp
    assert capabilities.tools == ()


async def test_the_seam_dispatches_a_tool_call_with_a_short_explicit_timeout(runtime, fake_call_mcp_tool):
    conn = connection_with_tools()
    await attach_connection(conn, runtime)
    await runtime.wait_for_discovery(ROBOT_ID)

    result = await runtime.call_tool(ROBOT_ID, "robot_move", {"distance_cm": 20}, timeout=2.0)

    assert result == "called:robot_move"
    assert fake_call_mcp_tool == [("robot_move", {"distance_cm": 20}, 2)]


async def test_the_seam_defaults_to_a_short_timeout_not_the_inherited_thirty_seconds(runtime, fake_call_mcp_tool):
    conn = connection_with_tools()
    await attach_connection(conn, runtime)
    await runtime.wait_for_discovery(ROBOT_ID)

    await runtime.call_tool(ROBOT_ID, "self_battery")

    assert fake_call_mcp_tool[0][2] <= 5


async def test_discovery_waits_for_the_inherited_handshake_to_finish(runtime):
    client = FakeDeviceMcpClient({"self_battery": tool_definition("self.battery")}, ready=False)
    conn = FakeConnection(features={"mcp": True}, mcp_client=client)
    await attach_connection(conn, runtime)

    await asyncio.sleep(0.05)
    assert await runtime.registry.get_capabilities(ROBOT_ID) is None

    client.ready = True
    await runtime.wait_for_discovery(ROBOT_ID)
    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None and capabilities.tool_names == ("self_battery",)


async def test_the_channel_gives_up_when_the_handshake_never_finishes():
    channel = ConnectionToolChannel(FakeConnection(), ready_timeout=0.05)
    with pytest.raises(McpError):
        await channel.discover()


async def test_a_malformed_tool_on_a_live_session_is_skipped(runtime):
    client = FakeDeviceMcpClient({"good": tool_definition("good.tool"), "bad": {"description": "no name"}})
    conn = FakeConnection(features={"mcp": True}, mcp_client=client)
    await attach_connection(conn, runtime)
    await runtime.wait_for_discovery(ROBOT_ID)

    capabilities = await runtime.registry.get_capabilities(ROBOT_ID)
    assert capabilities is not None
    assert capabilities.tool_names == ("good_tool",)
    assert capabilities.malformed_tools == 1


# -- bounds a device cannot exceed -----------------------------------------------------------------


async def test_a_device_that_publishes_too_many_tools_is_truncated_not_trusted(runtime):
    """An unbounded tool list is unbounded metric cardinality and an unbounded prompt.

    Sixteen is the robot vocabulary. A device offering hundreds has a bug or is not what it
    says it is, and either way the ceiling is the subsystem's, not the device's.
    """
    from robot.devices.mcp import MAX_TOOLS

    flood = [tool_definition(f"tool.{index}") for index in range(MAX_TOOLS + 40)]
    device = FakeMcpDevice(flood)
    capabilities = await channel_for(device).discover()
    assert len(capabilities.tool_names) == MAX_TOOLS


async def test_the_live_session_applies_the_same_ceiling(runtime):
    """The inherited client accumulates every page of tools/list with no limit of its own."""
    from robot.devices.mcp import MAX_TOOLS

    flood = {f"tool_{index}": tool_definition(f"tool.{index}") for index in range(MAX_TOOLS + 40)}
    conn = FakeConnection(features={"mcp": True}, mcp_client=FakeDeviceMcpClient(flood))
    capabilities = await ConnectionToolChannel(conn, ready_timeout=1.0).discover()
    assert len(capabilities.tool_names) == MAX_TOOLS
