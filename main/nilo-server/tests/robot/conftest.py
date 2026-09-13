"""Fakes for the robot tests: an MCP device and a session, neither of which is real.

No test in this package opens a socket, starts a server or calls an external API. The
device is a dictionary that answers JSON-RPC; the session is an object with the handful
of attributes the robot seam reads off a ConnectionHandler.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robot.runtime import RobotRuntime
from robot.state.models import DeviceInfo


class FakeMcpDevice:
    """A device that speaks MCP JSON-RPC over an in-process transport.

    Configurable in the ways that matter for discovery: how many tools it publishes, how
    many it puts on a page, whether it answers at all (timeout), and what it does to a
    tool call.
    """

    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        *,
        page_size: int | None = None,
        answer: bool = True,
        server_name: str = "fake-robot",
        server_version: str = "2.1.0",
        protocol_version: str = "2024-11-05",
        call_result: Any = None,
        call_error: str | None = None,
        latency_s: float = 0.0,
        repeat_cursor: bool = False,
    ) -> None:
        self.tools = tools if tools is not None else []
        self.page_size = page_size
        self.answer = answer
        self.server_name = server_name
        self.server_version = server_version
        self.protocol_version = protocol_version
        self.call_result = call_result
        self.call_error = call_error
        self.latency_s = latency_s
        self.repeat_cursor = repeat_cursor
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.client: Any = None

    async def send(self, payload: Any) -> None:
        """The transport side: record the request and feed the answer back to the client."""
        request = dict(payload)
        self.sent.append(request)
        if not self.answer:
            return
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        response = self._respond(request)
        if response is not None and self.client is not None:
            self.client.handle_message(response)

    @property
    def requested_methods(self) -> list[str]:
        return [request.get("method", "") for request in self.sent]

    def _respond(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        message_id = request.get("id")
        if method == "initialize":
            return self._result(
                message_id,
                {
                    "protocolVersion": self.protocol_version,
                    "serverInfo": {"name": self.server_name, "version": self.server_version},
                },
            )
        if method == "tools/list":
            return self._result(message_id, self._tools_page(request.get("params") or {}))
        if method == "tools/call":
            params = request.get("params") or {}
            self.calls.append((params.get("name", ""), dict(params.get("arguments") or {})))
            if self.call_error is not None:
                return self._result(message_id, {"isError": True, "error": self.call_error})
            result = self.call_result
            if result is None:
                result = {"content": [{"type": "text", "text": f"ok:{params.get('name')}"}]}
            return self._result(message_id, result)
        return None

    def _tools_page(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.page_size is None:
            return {"tools": list(self.tools)}
        cursor = params.get("cursor")
        start = int(cursor.split("-")[-1]) if isinstance(cursor, str) and cursor else 0
        page = self.tools[start : start + self.page_size]
        result: dict[str, Any] = {"tools": page}
        end = start + self.page_size
        if end < len(self.tools):
            result["nextCursor"] = f"page-{start if self.repeat_cursor else end}"
        return result

    @staticmethod
    def _result(message_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}


class FakeDeviceMcpClient:
    """Stands in for the inherited ``core`` device-MCP client on a fake session."""

    def __init__(self, tools: dict[str, dict[str, Any]] | None = None, ready: bool = True) -> None:
        self.tools = tools or {}
        self.name_mapping = {name: name for name in self.tools}
        self.ready = ready

    async def is_ready(self) -> bool:
        return self.ready

    def has_tool(self, name: str) -> bool:
        return name in self.tools


class FakeConnection:
    """The handful of ConnectionHandler attributes the robot seam actually reads."""

    def __init__(
        self,
        device_id: str = "aa:bb:cc:dd:ee:ff",
        session_id: str = "session-1",
        features: dict[str, Any] | None = None,
        mcp_client: Any = None,
        client_ip: str = "10.0.0.7",
    ) -> None:
        self.headers = {"device-id": device_id, "client-id": "client-1"}
        self.device_id = device_id
        self.session_id = session_id
        self.features = features
        self.mcp_client = mcp_client
        self.client_ip = client_ip


def tool_definition(name: str, description: str = "", **properties: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"{name} tool",
        "inputSchema": {"type": "object", "properties": dict(properties), "required": list(properties)},
    }


def device_info(device_id: str = "aa:bb:cc:dd:ee:ff", session_id: str = "session-1", **kwargs: Any) -> DeviceInfo:
    return DeviceInfo(device_id=device_id, session_id=session_id, **kwargs)


@pytest.fixture
async def runtime():
    """A runtime per test: no shared registry, no shared bus, no shared state store."""
    active = RobotRuntime(discovery_timeout=2.0)
    try:
        yield active
    finally:
        await active.aclose()
