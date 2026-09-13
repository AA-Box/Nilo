"""Capability discovery over the device MCP tool channel, against a fake device."""
from __future__ import annotations

import asyncio

import pytest

from robot.devices import (
    DEFAULT_ID_BASE,
    MalformedToolError,
    McpError,
    McpProtocolError,
    McpTimeoutError,
    McpToolError,
    RobotToolClient,
    sanitize_tool_name,
    supports_mcp,
    tool_from_mcp,
    unwrap_tool_result,
)
from tests.robot.conftest import FakeMcpDevice, tool_definition


def client_for(device: FakeMcpDevice, **kwargs) -> RobotToolClient:
    client = RobotToolClient(device, robot_id="aa-bb", **kwargs)
    device.client = client
    return client


async def test_discovery_runs_the_handshake_then_lists_tools():
    device = FakeMcpDevice([tool_definition("self.battery"), tool_definition("robot.move", distance_cm=1)])
    client = client_for(device)

    capabilities = await client.discover()

    assert device.requested_methods == ["initialize", "tools/list"]
    assert capabilities.mcp
    assert capabilities.server_name == "fake-robot"
    assert capabilities.server_version == "2.1.0"
    assert capabilities.protocol_version == "2024-11-05"
    assert capabilities.tool_names == ("self_battery", "robot_move")
    assert capabilities.malformed_tools == 0
    assert client.initialized


async def test_discovered_tools_keep_the_name_the_device_published():
    device = FakeMcpDevice([tool_definition("self.get_device_status", "state of the device")])
    capabilities = await client_for(device).discover()

    tool = capabilities.get_tool("self_get_device_status")
    assert tool is not None
    assert tool.raw_name == "self.get_device_status"
    assert tool.description == "state of the device"
    assert tool.source == "device_mcp"


async def test_capabilities_come_from_the_device_and_not_from_a_hardcoded_list():
    first = await client_for(FakeMcpDevice([tool_definition("self.battery")])).discover()
    second = await client_for(FakeMcpDevice([tool_definition("camera.capture")])).discover()
    assert first.tool_names == ("self_battery",)
    assert second.tool_names == ("camera_capture",)


async def test_tools_list_follows_pagination():
    tools = [tool_definition(f"tool.{index}") for index in range(5)]
    device = FakeMcpDevice(tools, page_size=2)
    client = client_for(device)

    capabilities = await client.discover()

    assert len(capabilities.tools) == 5
    assert device.requested_methods == ["initialize", "tools/list", "tools/list", "tools/list"]
    cursors = [request.get("params", {}).get("cursor") for request in device.sent if request["method"] == "tools/list"]
    assert cursors == [None, "page-2", "page-4"]


async def test_a_device_that_repeats_its_cursor_is_rejected_instead_of_looping():
    device = FakeMcpDevice([tool_definition(f"tool.{index}") for index in range(4)], page_size=2, repeat_cursor=True)
    with pytest.raises(McpProtocolError, match="repeated cursor"):
        await client_for(device).discover()


async def test_a_silent_device_times_out_and_leaves_no_pending_request():
    device = FakeMcpDevice(answer=False)
    client = client_for(device, request_timeout=0.05)

    with pytest.raises(McpTimeoutError):
        await client.discover()

    assert client.pending_requests == 0
    assert not client.initialized


async def test_a_tool_call_times_out_on_its_own_short_budget():
    device = FakeMcpDevice([tool_definition("robot.move")])
    client = client_for(device, call_timeout=0.05)
    await client.discover()
    device.answer = False

    with pytest.raises(McpTimeoutError):
        await client.call_tool("robot_move", {"distance_cm": 10})
    assert client.pending_requests == 0


async def test_malformed_tool_definitions_are_skipped_and_counted():
    device = FakeMcpDevice(
        [
            tool_definition("good.one"),
            {"description": "no name at all"},
            {"name": "   "},
            "not even an object",
            {"name": "loose.schema", "inputSchema": "not a schema"},
            {"name": "good.two", "description": 42, "inputSchema": {"properties": {"a": {}}, "required": ["a", 7]}},
        ]
    )
    capabilities = await client_for(device).discover()

    assert capabilities.tool_names == ("good_one", "loose_schema", "good_two")
    assert capabilities.malformed_tools == 3
    loose = capabilities.get_tool("loose_schema")
    assert loose is not None and loose.input_schema == {"type": "object", "properties": {}, "required": []}
    good_two = capabilities.get_tool("good_two")
    assert good_two is not None and good_two.description == ""
    assert good_two.required_arguments == ("a",)


async def test_a_duplicate_tool_name_keeps_the_first_definition():
    device = FakeMcpDevice([tool_definition("self.battery", "first"), tool_definition("self.battery", "second")])
    capabilities = await client_for(device).discover()
    tool = capabilities.get_tool("self_battery")
    assert len(capabilities.tools) == 1
    assert tool is not None and tool.description == "first"


class _DeviceWithABrokenToolList(FakeMcpDevice):
    def _tools_page(self, params):
        return {"nope": []}


async def test_a_tools_list_result_that_is_not_a_tool_list_is_a_protocol_error():
    with pytest.raises(McpProtocolError):
        await client_for(_DeviceWithABrokenToolList()).discover()


async def test_calling_a_tool_returns_its_text_result():
    device = FakeMcpDevice([tool_definition("robot.move", distance_cm=1)])
    client = client_for(device)
    await client.discover()

    result = await client.call_tool("robot_move", {"distance_cm": 30})

    assert result == "ok:robot_move"
    assert device.calls == [("robot_move", {"distance_cm": 30})]


async def test_a_tool_that_reports_an_error_raises():
    device = FakeMcpDevice([tool_definition("robot.move")], call_error="cliff detected")
    client = client_for(device)
    await client.discover()
    with pytest.raises(McpToolError, match="cliff detected"):
        await client.call_tool("robot_move")


async def test_a_tool_call_before_the_handshake_is_refused():
    client = client_for(FakeMcpDevice())
    with pytest.raises(McpError, match="handshake"):
        await client.call_tool("robot_move")


async def test_request_ids_start_above_the_inherited_handshake_ids():
    """The inherited client uses ids 1 and 2 and starts its call ids at 1 (R10)."""
    device = FakeMcpDevice([tool_definition("self.battery")])
    client = client_for(device)
    await client.discover()
    assert [request["id"] for request in device.sent] == [DEFAULT_ID_BASE, DEFAULT_ID_BASE + 1]


async def test_a_response_for_another_client_on_the_channel_is_ignored():
    device = FakeMcpDevice()
    client = client_for(device)
    assert not client.handle_message({"id": 2, "result": {"tools": []}})
    assert not client.handle_message({"result": {}})
    assert not client.handle_message({"id": "not-a-number", "result": {}})


async def test_a_device_error_response_becomes_an_exception():
    device = FakeMcpDevice()
    client = client_for(device)
    device.answer = False
    task = asyncio.create_task(client.initialize())
    await asyncio.sleep(0)
    with pytest.raises(McpError, match="method not found"):
        client.handle_message({"id": DEFAULT_ID_BASE, "error": {"message": "method not found"}})
        await task


async def test_closing_the_channel_rejects_in_flight_requests():
    device = FakeMcpDevice(answer=False)
    client = client_for(device, request_timeout=5.0)
    task = asyncio.create_task(client.initialize())
    await asyncio.sleep(0)
    await client.aclose()
    with pytest.raises(McpError, match="closed"):
        await task
    with pytest.raises(McpError, match="closed"):
        await client.initialize()


def test_tool_names_are_sanitized_for_the_function_surface():
    assert sanitize_tool_name("self.get_device_status") == "self_get_device_status"
    assert sanitize_tool_name("robot-move") == "robot-move"


def test_mcp_support_is_read_from_the_hello_features_block():
    assert supports_mcp({"mcp": True})
    assert not supports_mcp({"mcp": False})
    assert not supports_mcp({})
    assert not supports_mcp(None)


def test_tool_normalization_rejects_what_it_cannot_use():
    with pytest.raises(MalformedToolError):
        tool_from_mcp(["nope"])
    with pytest.raises(MalformedToolError):
        tool_from_mcp({"name": ""})


def test_unwrapping_falls_back_to_the_string_form():
    assert unwrap_tool_result({"content": [{"text": "hello"}]}) == "hello"
    assert unwrap_tool_result({"content": []}) == "{'content': []}"
    assert unwrap_tool_result(7) == "7"
    with pytest.raises(McpToolError):
        unwrap_tool_result({"isError": True, "error": "boom"})
