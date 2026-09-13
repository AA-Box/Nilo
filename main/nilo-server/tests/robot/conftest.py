"""Fakes for the robot tests: an MCP device and a session, neither of which is real.

No test in this package opens a socket, starts a server or calls an external API. The
device is a dictionary that answers JSON-RPC; the session is an object with the handful
of attributes the robot seam reads off a ConnectionHandler.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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


# -- the action layer ---------------------------------------------------------------------
#
# The executor talks to exactly three things: an event bus, a tool channel and the world
# model. FakeActionRuntime is all three, in sixty lines, so an executor test needs no
# socket, no device and no registry — and can make the device behave in ways a real one
# only manages on a bad day.

from robot.actions.model import SPEC_TYPES  # noqa: E402
from robot.devices.mcp import McpTimeoutError, sanitize_tool_name  # noqa: E402
from robot.events.bus import EventBus  # noqa: E402
from robot.events.types import RobotEvent  # noqa: E402
from robot.state.models import (  # noqa: E402
    ConnectionStatus,
    RobotBatteryState,
    RobotCapabilities,
    RobotConnection,
    RobotIdentity,
    RobotSensorState,
    RobotState,
    RobotTelemetry,
    RobotTool,
    utcnow,
)

ROBOT_ID = "test-robot"

#: Every tool the ten action specs dispatch to, as a device would publish them.
ACTION_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted({spec.device_tool_name for spec in SPEC_TYPES.values()})
)


def robot_tools(names: Iterable[str] = ACTION_TOOL_NAMES) -> tuple[RobotTool, ...]:
    """Discovered tools, named the way discovery names them (sanitized, raw kept)."""
    return tuple(
        RobotTool(name=sanitize_tool_name(name), raw_name=name, description=f"{name} tool")
        for name in names
    )


def robot_state(
    robot_id: str = ROBOT_ID,
    *,
    connected: bool = True,
    tools: Iterable[str] | None = None,
    sensors: RobotSensorState | None = None,
    telemetry: RobotTelemetry | None = None,
    last_seen: datetime | None = None,
    battery: Any = None,
) -> RobotState:
    """A registered, connected robot with fresh sensors and a full tool table.

    Every argument exists so a test can make exactly one thing wrong — a stale sensor
    frame, a missing tool, a closed connection — and leave the rest healthy.
    """
    moment = last_seen or utcnow()
    capabilities = RobotCapabilities(
        mcp=True, tools=robot_tools(ACTION_TOOL_NAMES if tools is None else tools)
    )
    identity = RobotIdentity(
        device_id=robot_id, robot_id=robot_id, capabilities=capabilities, last_seen_at=moment
    )
    connection = RobotConnection(
        robot_id=robot_id,
        device_id=robot_id,
        session_id="session-1",
        status=ConnectionStatus.CONNECTED if connected else ConnectionStatus.DISCONNECTED,
        last_seen_at=moment,
        updated_at=moment,
    )
    picture = telemetry or RobotTelemetry(
        sensors=sensors if sensors is not None else RobotSensorState(readings={"front_mm": 1500.0}),
        battery=battery,
    )
    return RobotState(
        robot_id=robot_id, identity=identity, connection=connection, telemetry=picture
    )


class FakeActionRuntime:
    """An :class:`~robot.actions.executor.ActionRuntime` with a scriptable device.

    ``replies`` maps a sanitized tool name to what the call returns: a string (sent back
    verbatim), a dict (JSON-encoded, as firmware would), an exception instance (raised),
    or a callable taking the arguments. Anything unscripted gets a generic accepted
    reply with a fresh device action id, which is what a healthy robot does.
    """

    def __init__(
        self,
        states: dict[str, RobotState] | None = None,
        *,
        events: EventBus | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.events = events or EventBus()
        self.states: dict[str, RobotState] = states or {ROBOT_ID: robot_state()}
        self.replies: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.latency_s = latency_s
        self.published: list[RobotEvent] = []
        self._device_ids = itertools.count(1)

    # -- the ActionRuntime surface ---------------------------------------------------------

    async def call_tool(
        self,
        robot_id: str,
        name: str,
        arguments: Any = None,
        *,
        timeout: float | None = None,
    ) -> str:
        self.calls.append((robot_id, name, dict(arguments or {})))
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        scripted = self.replies.get(name, _MISSING)
        if scripted is _MISSING:
            return json.dumps({"accepted": True, "action_id": self.next_device_id()})
        if callable(scripted) and not isinstance(scripted, BaseException):
            scripted = scripted(dict(arguments or {}))
        if isinstance(scripted, BaseException):
            raise scripted
        if isinstance(scripted, dict):
            return json.dumps(scripted)
        return str(scripted)

    async def get_state(self, robot_id: str) -> RobotState | None:
        return self.states.get(robot_id)

    # -- helpers for tests -------------------------------------------------------------------

    def next_device_id(self) -> str:
        return f"dev-{next(self._device_ids)}"

    def set_state(self, state: RobotState) -> None:
        self.states[state.robot_id] = state

    def called(self, name: str) -> tuple[dict[str, Any], ...]:
        return tuple(arguments for _, tool, arguments in self.calls if tool == name)

    def never_answers(self, name: str) -> None:
        """Make a tool behave like a device that accepted the frame and went quiet."""
        self.replies[name] = McpTimeoutError(f"{name} did not answer")

    async def aclose(self) -> None:
        await self.events.aclose()


_MISSING = object()


# -- the behaviour engine ---------------------------------------------------------------------
#
# Three things every behaviour test needs and none of them is real: a clock a test moves by
# hand, a robot handle that records instead of commanding, and a world snapshot builder.
# Between them a test for "boredom rises over five minutes" runs in microseconds.

from robot.behavior.base import AutonomyMode  # noqa: E402
from robot.behavior.builtins import default_behaviors  # noqa: E402
from robot.behavior.explain import NullRobot  # noqa: E402
from robot.behavior.scheduler import BehaviorRegistry, BehaviorScheduler  # noqa: E402
from robot.behavior.tuning import BehaviorTuning  # noqa: E402
from robot.state.world import Interaction, WorldState  # noqa: E402

#: The fixed instant every behaviour test builds its worlds at. Freshness inside a snapshot
#: is measured against the snapshot's own ``updated_at``, so no test depends on wall time.
WORLD_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Time that only moves when a test says so."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class RecordingRobot(NullRobot):
    """A robot handle that records every semantic command and executes nothing."""

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.calls)

    def arguments(self, name: str) -> tuple[dict[str, Any], ...]:
        return tuple(args for called, args in self.calls if called == name)


def behavior_world(
    robot_id: str = ROBOT_ID,
    *,
    battery_percent: int = 80,
    charging: bool = False,
    touch: bool = False,
    cliff: bool = False,
    now: datetime | None = None,
    entities: Iterable[Any] = (),
    interaction_ended_s: float | None = None,
    **fields: Any,
) -> WorldState:
    """A world snapshot with the telemetry a behaviour reads and any entities you name."""
    moment = now or WORLD_T0
    world = WorldState(
        robot_id=robot_id,
        telemetry=RobotTelemetry(
            battery=RobotBatteryState(percent=battery_percent, charging=charging, updated_at=moment),
            sensors=RobotSensorState(
                touch_detected=touch,
                cliff_detected=cliff,
                readings={"front_mm": 1500.0},
                updated_at=moment,
            ),
            updated_at=moment,
        ),
        updated_at=moment,
        **fields,
    )
    for entity in entities:
        world = world.observe(entity, now=moment)
    if interaction_ended_s is not None:
        ended = moment - timedelta(seconds=interaction_ended_s)
        world = world.start_interaction(
            Interaction(id="chat-1", started_at=ended - timedelta(seconds=30), ended_at=ended),
            now=moment,
        ).end_interaction(now=ended)
        world = world.model_copy(update={"updated_at": moment})
    return world


def make_scheduler(
    *behaviors: Any,
    robot: RecordingRobot | None = None,
    clock: FakeClock | None = None,
    tuning: BehaviorTuning | None = None,
    mode: AutonomyMode = AutonomyMode.NORMAL,
    seed: int = 0,
    events: Any = None,
    robot_id: str = ROBOT_ID,
) -> tuple[BehaviorScheduler, RecordingRobot, FakeClock]:
    """A scheduler over the behaviours given (or all the built-ins), on a fake clock."""
    handle = robot or RecordingRobot()
    fake_clock = clock or FakeClock()
    registry = BehaviorRegistry(behaviors if behaviors else default_behaviors())
    scheduler = BehaviorScheduler(
        robot_id,
        handle,
        registry=registry,
        tuning=tuning or BehaviorTuning(),
        mode=mode,
        events=events,
        seed=seed,
        clock=fake_clock,
    )
    return scheduler, handle, fake_clock
