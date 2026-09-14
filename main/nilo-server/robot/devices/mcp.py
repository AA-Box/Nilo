"""The device tool channel: JSON-RPC MCP, wrapped in robot terms.

The device publishes its hardware as MCP tools over the session WebSocket
(docs/mcp.md). This module is the robot-side abstraction over that channel:

    transport ──▶ RobotToolClient ──▶ RobotCapabilities (RobotTool models)

:class:`RobotToolClient` owns nothing but JSON-RPC: request ids, pending futures,
timeouts, ``tools/list`` pagination and result unwrapping. It is transport-agnostic on
purpose — the production transport writes to a device WebSocket, the test transport is a
dictionary — so capability discovery is testable without a socket, a device or a server.

Two properties are deliberate:

* **Its own request-id space.** Ids start at :data:`DEFAULT_ID_BASE`, above the inherited
  handshake ids (1 and 2) and above its call ids, so a response cannot resolve the wrong
  future when both clients share one channel (robot-architecture R10).
* **Every request has an explicit, short timeout.** The inherited ``call_mcp_tool``
  defaults to 30 s, two orders of magnitude too long for a motion acknowledgement.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any, Protocol

from robot import __version__
from robot.state.models import RobotCapabilities, RobotTool, utcnow

logger = logging.getLogger(__name__)

#: First JSON-RPC id this client uses. Above the inherited handshake ids (R10).
DEFAULT_ID_BASE = 1000
#: Timeout for a handshake or discovery request, in seconds.
DEFAULT_REQUEST_TIMEOUT = 5.0
#: Timeout for a tool call, in seconds. Short: an unacknowledged command is a failure.
DEFAULT_CALL_TIMEOUT = 5.0
#: Hard stop for a device that keeps handing back a ``nextCursor``.
MAX_TOOL_PAGES = 64
#: Hard stop on how many tools one device may publish.
#:
#: The robot vocabulary is sixteen tools. A device offering hundreds is a device with a
#: bug or a device that is not what it says it is, and either way the cost is not just
#: memory: every tool name becomes a metric label and a line in the model's function list,
#: so an unbounded tool list is unbounded cardinality in the scrape and an unbounded
#: prompt. Extra tools are dropped with a warning rather than refused, because a device
#: that publishes one tool too many should still be a usable robot.
MAX_TOOLS = 128
#: MCP revision the inherited handshake speaks; kept identical so firmware sees no skew.
MCP_PROTOCOL_VERSION = "2024-11-05"

# Same rule as core.utils.util.sanitize_tool_name. Duplicated (one regex) rather than
# imported: robot/ must not import core/ at module scope (robot-architecture Sect. 7).
_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_\-一-鿿]")


class McpError(RuntimeError):
    """Any failure on the device tool channel."""


class McpTimeoutError(McpError):
    """The device did not answer a request inside its timeout."""


class McpUnsupportedError(McpError):
    """The device does not speak MCP, so it has no discoverable tools."""


class McpProtocolError(McpError):
    """The device answered with something that is not a valid JSON-RPC result."""


class McpToolError(McpError):
    """The device reported that a tool call failed."""


class MalformedToolError(ValueError):
    """A tool definition that cannot be turned into a :class:`RobotTool`."""


class McpTransport(Protocol):
    """Anything that can put a JSON-RPC payload in front of the device."""

    async def send(self, payload: Mapping[str, Any]) -> None: ...


def sanitize_tool_name(name: str) -> str:
    """Tool name with every character an LLM function name may not contain replaced."""
    return _UNSAFE_NAME.sub("_", name)


def supports_mcp(features: Mapping[str, Any] | None) -> bool:
    """Whether the device advertised MCP in its ``hello`` features block."""
    return bool(features and features.get("mcp"))


def tool_from_mcp(raw: Any, *, source: str = "device_mcp") -> RobotTool:
    """Normalize one ``tools/list`` entry into a :class:`RobotTool`.

    Raises :class:`MalformedToolError` for anything unusable, so a device that publishes
    one bad definition loses that tool and not the rest of its capabilities.
    """
    if not isinstance(raw, Mapping):
        raise MalformedToolError(f"tool definition must be an object, got {type(raw).__name__}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MalformedToolError(f"tool definition has no usable name: {raw!r}")
    description = raw.get("description")
    schema = raw.get("inputSchema")
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    if isinstance(schema, Mapping):
        schema_type = schema.get("type")
        properties = schema.get("properties")
        required = schema.get("required")
        input_schema["type"] = schema_type if isinstance(schema_type, str) else "object"
        input_schema["properties"] = dict(properties) if isinstance(properties, Mapping) else {}
        input_schema["required"] = [item for item in required if isinstance(item, str)] if isinstance(required, list) else []
    return RobotTool(
        name=sanitize_tool_name(name),
        raw_name=name,
        description=description if isinstance(description, str) else "",
        input_schema=input_schema,
        source=source,
    )


def unwrap_tool_result(raw: Any) -> str:
    """Turn an MCP ``tools/call`` result into text, the way the inherited channel does."""
    if isinstance(raw, Mapping):
        if raw.get("isError") is True:
            raise McpToolError(str(raw.get("error", "tool call returned an error without details")))
        content = raw.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, Mapping) and isinstance(first.get("text"), str):
                text: str = first["text"]
                return text
    return str(raw)


class RobotToolClient:
    """MCP client for one device, over one transport.

    Responses arrive asynchronously: the session read loop calls :meth:`handle_message`
    with every inbound MCP payload, which resolves the future the matching request is
    waiting on. Unknown ids are ignored (they belong to another client on the channel).
    """

    def __init__(
        self,
        transport: McpTransport,
        *,
        robot_id: str = "unknown",
        features: Mapping[str, Any] | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        id_base: int = DEFAULT_ID_BASE,
        max_tool_pages: int = MAX_TOOL_PAGES,
        client_name: str = "nilo-robot",
    ) -> None:
        self._transport = transport
        self._robot_id = robot_id
        self._features: dict[str, Any] = dict(features or {})
        self._request_timeout = request_timeout
        self._call_timeout = call_timeout
        self._next_id = id_base
        self._max_tool_pages = max_tool_pages
        self._client_name = client_name
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized = False
        self._server_name: str | None = None
        self._server_version: str | None = None
        self._protocol_version: str | None = None
        self._closed = False

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def pending_requests(self) -> int:
        return len(self._pending)

    def handle_message(self, payload: Mapping[str, Any]) -> bool:
        """Feed one inbound MCP payload in. Returns whether it settled a request here."""
        raw_id = payload.get("id")
        if not isinstance(raw_id, (int, str)):
            return False
        try:
            message_id = int(raw_id)
        except ValueError:
            return False
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return False
        if "error" in payload:
            error = payload["error"]
            message = error.get("message", "unknown error") if isinstance(error, Mapping) else str(error)
            future.set_exception(McpError(f"device reported an MCP error: {message}"))
        else:
            future.set_result(payload.get("result"))
        return True

    async def initialize(self) -> RobotCapabilities:
        """Run the MCP handshake. Safe to call again; the second call is a no-op."""
        if self._initialized:
            return self._capabilities(())
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                "clientInfo": {"name": self._client_name, "version": __version__},
            },
        )
        if isinstance(result, Mapping):
            server_info = result.get("serverInfo")
            if isinstance(server_info, Mapping):
                name = server_info.get("name")
                version = server_info.get("version")
                self._server_name = name if isinstance(name, str) else None
                self._server_version = version if isinstance(version, str) else None
            protocol_version = result.get("protocolVersion")
            self._protocol_version = protocol_version if isinstance(protocol_version, str) else None
        self._initialized = True
        return self._capabilities(())

    async def list_tools(self) -> tuple[tuple[RobotTool, ...], int]:
        """Every tool the device publishes, following ``nextCursor`` pagination.

        Returns the tools and the number of definitions that were malformed and skipped.
        """
        tools: list[RobotTool] = []
        seen: set[str] = set()
        malformed = 0
        cursor: str | None = None
        for page in range(self._max_tool_pages):
            params = {"cursor": cursor} if cursor else None
            result = await self._request("tools/list", params)
            if not isinstance(result, Mapping):
                raise McpProtocolError(f"tools/list returned {type(result).__name__}, expected an object")
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise McpProtocolError("tools/list result has no 'tools' array")
            for raw in raw_tools:
                try:
                    tool = tool_from_mcp(raw)
                except (MalformedToolError, ValueError) as exc:
                    malformed += 1
                    logger.warning("robot %s: skipping malformed tool definition: %s", self._robot_id, exc)
                    continue
                if tool.name in seen:
                    logger.warning("robot %s: duplicate tool %r, keeping the first", self._robot_id, tool.name)
                    continue
                if len(tools) >= MAX_TOOLS:
                    logger.warning(
                        "robot %s: more than %d tools published; ignoring the rest",
                        self._robot_id,
                        MAX_TOOLS,
                    )
                    return tuple(tools), malformed
                seen.add(tool.name)
                tools.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor == cursor:
                raise McpProtocolError(f"tools/list repeated cursor {next_cursor!r}")
            cursor = next_cursor
            if page == self._max_tool_pages - 1:
                logger.warning(
                    "robot %s: stopping tools/list after %d pages", self._robot_id, self._max_tool_pages
                )
        return tuple(tools), malformed

    async def discover(self) -> RobotCapabilities:
        """Handshake plus a full ``tools/list``: everything this robot can do."""
        await self.initialize()
        tools, malformed = await self.list_tools()
        return self._capabilities(tools, malformed=malformed)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        """Invoke a device tool and return its text result."""
        if not self._initialized:
            raise McpError("tool call before the MCP handshake completed")
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
            timeout=timeout if timeout is not None else self._call_timeout,
        )
        return unwrap_tool_result(result)

    async def aclose(self) -> None:
        """Reject every in-flight request. A dropped channel never answers."""
        self._closed = True
        pending = list(self._pending.items())
        self._pending.clear()
        for _, future in pending:
            if not future.done():
                future.set_exception(McpError("device tool channel closed"))

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._closed:
            raise McpError("device tool channel closed")
        message_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._transport.send(payload)
            return await asyncio.wait_for(future, timeout=timeout if timeout is not None else self._request_timeout)
        except TimeoutError as exc:
            raise McpTimeoutError(f"{method} timed out after {timeout or self._request_timeout}s") from exc
        finally:
            self._pending.pop(message_id, None)

    def _capabilities(self, tools: tuple[RobotTool, ...], *, malformed: int = 0) -> RobotCapabilities:
        return RobotCapabilities(
            mcp=True,
            features=frozenset(str(key) for key, value in self._features.items() if value),
            tools=tools,
            protocol_version=self._protocol_version,
            server_name=self._server_name,
            server_version=self._server_version,
            malformed_tools=malformed,
            updated_at=utcnow(),
        )
