"""The simulator against a running server: connection through disconnect and back.

Every test here starts the inherited WebSocket server in-process (``backend`` fixture)
and runs :class:`robot.simulator.SimulatedRobot` against it over a real socket. Nothing
reaches into the server to make an assertion pass: the simulator only has the tools and
notifications a device has, and the assertions read the server's own registry, state
store and event bus.

The clock is accelerated rather than mocked (``--speed``), so a five-second move is a
fraction of a second of test time while the *server* still sees real network timing.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("websockets")
pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from core.utils.util import is_valid_image_file  # noqa: E402
from robot.devices.mcp import McpError, McpTimeoutError, McpToolError  # noqa: E402
from robot.events.types import MotionCompleted, MotionFailed, RobotEvent  # noqa: E402
from robot.simulator import Scenario, SimulatedRobot, SimulatorConfig, Step, World  # noqa: E402
from robot.simulator.clock import RealClock  # noqa: E402
from robot.state.models import normalize_robot_id  # noqa: E402
from tests.integration.conftest import Backend  # noqa: E402

ROBOT_ID = "aa:bb:cc:00:00:01"
NORMALIZED = normalize_robot_id(ROBOT_ID)
#: Simulated seconds per real second. Keeps a 20 s scenario inside a test's patience.
CLOCK_SPEED = 20.0
#: Slower clock for the tests whose scenario drives itself. At CLOCK_SPEED a step at t=2 s
#: lands ~100 ms after connect, which on a loaded CI runner is still inside the handshake.
SCRIPTED_SPEED = 8.0


def make_robot(
    backend: Backend,
    *,
    scenario: Scenario | None = None,
    robot_id: str = ROBOT_ID,
    speed: float = CLOCK_SPEED,
    **config: Any,
) -> SimulatedRobot:
    """A simulator pointed at the test server, with the status endpoint off by default."""
    defaults: dict[str, Any] = {
        "tick_ms": 50, "telemetry_ms": 200, "pose_ms": 100, "status_port": 0, "reconnect": False,
    }
    settings = SimulatorConfig(server_url=backend.ws_url, robot_id=robot_id, **{**defaults, **config})
    chosen = scenario or Scenario(name="test", duration_s=0.0)
    return SimulatedRobot(settings, scenario=chosen, clock=RealClock(speed), world=World(chosen.world))


class _Recorder:
    """Collects robot events off the bus so a test can wait for one."""

    def __init__(self) -> None:
        self.events: list[RobotEvent] = []

    def __call__(self, event: RobotEvent) -> None:
        self.events.append(event)

    def of(self, kind: type[RobotEvent]) -> list[RobotEvent]:
        return [event for event in self.events if isinstance(event, kind)]


def recording(backend: Backend) -> _Recorder:
    """Subscribe to every robot event.

    Call this **before** starting a simulator whose scenario drives itself: on an
    accelerated clock a step at t=2 s fires ~100 ms after connect, which is inside the
    handshake, so a subscription taken after the simulator starts can miss the event it
    is waiting for.
    """
    recorder = _Recorder()
    backend.runtime.events.subscribe(RobotEvent, recorder)
    return recorder


@contextlib.asynccontextmanager
async def running(robot: SimulatedRobot) -> AsyncIterator[SimulatedRobot]:
    """Run a simulator for the duration of a block, and tear it down whatever happens."""
    task = asyncio.create_task(robot.run(), name="simulator-under-test")
    try:
        await robot.wait_connected(timeout=15)
        yield robot
    finally:
        robot.stop()
        await robot.drop_connection(reconnect=False)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest.fixture
async def robot(backend: Backend) -> AsyncIterator[SimulatedRobot]:
    async with running(make_robot(backend)) as simulator:
        yield simulator


# -- connection ------------------------------------------------------------------------


async def test_connection_registers_the_robot(backend: Backend, robot: SimulatedRobot) -> None:
    state = await backend.wait_for(lambda: backend.state(NORMALIZED))
    assert state.robot_id == NORMALIZED
    assert state.is_connected
    assert state.connection.session_id
    assert robot.session_id == state.connection.session_id


async def test_two_robots_are_two_entries(backend: Backend) -> None:
    first = make_robot(backend, robot_id="aa:bb:cc:00:00:01")
    second = make_robot(backend, robot_id="aa:bb:cc:00:00:02")
    async with running(first):
        async with running(second):
            states = await backend.wait_for(lambda: _both(backend), timeout=15)
            assert [state.robot_id for state in states] == ["aa-bb-cc-00-00-01", "aa-bb-cc-00-00-02"]
        remaining = await backend.wait_for(lambda: _only_one(backend), timeout=15)
        assert [state.robot_id for state in remaining] == ["aa-bb-cc-00-00-01"]


async def _both(backend: Backend) -> Any:
    states = await backend.runtime.registry.list()
    return states if len(states) == 2 else None


async def _only_one(backend: Backend) -> Any:
    states = await backend.runtime.registry.list()
    return states if len(states) == 1 else None


# -- capability discovery ----------------------------------------------------------------


async def test_capabilities_are_discovered_over_mcp(backend: Backend, robot: SimulatedRobot) -> None:
    await robot.wait_discovered(timeout=15)
    capabilities = await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
    assert capabilities.mcp is True
    assert capabilities.malformed_tools == 0
    assert "robot_motion_move" in capabilities.tool_names
    assert "robot_camera_capture" in capabilities.tool_names
    assert len(capabilities.tool_names) == 16
    # The device published dotted names; the server sanitizes and keeps the original.
    tool = capabilities.get_tool("robot_motion_move")
    assert tool is not None and tool.raw_name == "robot.motion.move"
    assert tool.required_arguments == ("distance_mm",)


async def test_capabilities_arrive_across_several_tool_pages(backend: Backend) -> None:
    paged = make_robot(backend, tools_page_size=4)
    async with running(paged) as simulator:
        await simulator.wait_discovered(timeout=15)
        capabilities = await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
        assert len(capabilities.tool_names) == 16


# -- telemetry -----------------------------------------------------------------------------


async def test_telemetry_notifications_reach_the_world_state(backend: Backend, robot: SimulatedRobot) -> None:
    state = await backend.wait_for(lambda: _with_battery(backend), timeout=15)
    assert state.telemetry.battery is not None
    assert 0 <= state.telemetry.battery.percent <= 100
    assert state.telemetry.pose is not None
    assert state.telemetry.sensors is not None
    assert state.telemetry.sensors.readings["front_mm"] > 0
    assert state.telemetry.activity is not None


async def _with_battery(backend: Backend) -> Any:
    state = await backend.runtime.registry.get(NORMALIZED)
    return state if state is not None and state.telemetry.battery is not None else None


# -- tool calls ------------------------------------------------------------------------------


async def test_tool_call_round_trip(backend: Backend, robot: SimulatedRobot) -> None:
    await robot.wait_discovered(timeout=15)
    await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
    raw = await backend.runtime.call_tool(NORMALIZED, "robot_get_status", {})
    status = json.loads(raw)
    assert status["battery"]["percent"] > 0
    assert status["pose"]["frame"] == "odom"
    assert robot.tool_calls[-1][0] == "robot.get_status"


async def test_camera_capture_returns_an_image_the_backend_accepts(
    backend: Backend, robot: SimulatedRobot
) -> None:
    await robot.wait_discovered(timeout=15)
    await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
    frame = json.loads(await backend.runtime.call_tool(NORMALIZED, "robot_camera_capture", {}, timeout=10))
    image = base64.b64decode(frame["image_base64"])
    assert frame["mime_type"] == "image/png"
    assert is_valid_image_file(image)  # the server's own sniffer, used by the vision endpoint
    assert frame["image_bytes"] == len(image)


async def test_unpublished_tool_is_refused_before_it_reaches_the_device(
    backend: Backend, robot: SimulatedRobot
) -> None:
    await robot.wait_discovered(timeout=15)
    await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
    before = len(robot.tool_calls)
    with pytest.raises(McpError, match="does not publish"):
        await backend.runtime.call_tool(NORMALIZED, "robot_self_destruct", {})
    assert len(robot.tool_calls) == before


# -- movement lifecycle -------------------------------------------------------------------


async def test_movement_reports_moving_then_completed(backend: Backend, robot: SimulatedRobot) -> None:
    recorder = recording(backend)
    await robot.wait_discovered(timeout=15)
    await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))

    accepted = json.loads(
        await backend.runtime.call_tool(NORMALIZED, "robot_motion_move", {"distance_mm": 600, "speed_mmps": 200})
    )
    assert accepted["accepted"] is True and accepted["state"] == "moving"
    action_id = accepted["action_id"]

    moving = await backend.wait_for(lambda: _motion_state(backend, moving=True), timeout=15)
    assert moving.action_id == action_id

    completed = await backend.wait_for(lambda: recorder.of(MotionCompleted), timeout=20)
    assert completed[0].action_id == action_id
    state = await backend.runtime.registry.get(NORMALIZED)
    assert state.telemetry.pose is not None
    assert state.telemetry.pose.x_m == pytest.approx(1.2, abs=0.05)
    assert state.telemetry.motion is not None and state.telemetry.motion.moving is False


async def test_movement_can_be_cancelled(backend: Backend, robot: SimulatedRobot) -> None:
    recorder = recording(backend)
    await robot.wait_discovered(timeout=15)
    await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))

    accepted = json.loads(
        await backend.runtime.call_tool(NORMALIZED, "robot_motion_move", {"distance_mm": 2000, "speed_mmps": 100})
    )
    await backend.wait_for(lambda: _motion_state(backend, moving=True), timeout=15)
    stopped = json.loads(await backend.runtime.call_tool(NORMALIZED, "robot_motion_stop", {}))
    assert stopped["stopped_action_id"] == accepted["action_id"]

    failures = await backend.wait_for(lambda: recorder.of(MotionFailed), timeout=20)
    assert failures[0].action_id == accepted["action_id"]
    assert failures[0].reason == "cancelled"
    idle = await backend.wait_for(lambda: _motion_state(backend, moving=False), timeout=15)
    assert idle.action_id is None


async def test_obstacle_appearing_mid_move_fails_the_motion(backend: Backend) -> None:
    scenario = Scenario(
        name="obstacle-under-test",
        duration_s=0.0,
        steps=[
            # The box appears at t=2 s, well before the robot reaches x=1.43 m at t=5.2 s,
            # so the failure is caused by the obstacle and not by where it already was.
            Step(at_s=1.0, do="move", args={"distance_mm": 2000, "speed_mmps": 200}),
            Step(at_s=2.0, do="add_obstacle", args={"id": "box", "x_mm": 1600, "y_mm": 200, "radius_mm": 100}),
        ],
    )
    recorder = recording(backend)
    async with running(make_robot(backend, scenario=scenario, speed=SCRIPTED_SPEED)) as simulator:
        failures = await backend.wait_for(lambda: recorder.of(MotionFailed), timeout=25)
        assert failures[0].reason == "obstacle"
        assert "box" in failures[0].detail
        state = await backend.runtime.registry.get(NORMALIZED)
        assert state.telemetry.pose is not None and state.telemetry.pose.x_m < 1.5  # stopped short of the box
        assert simulator.state.moving is False


async def test_cliff_appearing_mid_move_fails_the_motion(backend: Backend) -> None:
    scenario = Scenario(
        name="cliff-under-test",
        duration_s=0.0,
        steps=[
            Step(at_s=1.0, do="move", args={"distance_mm": 2000, "speed_mmps": 200}),
            Step(
                at_s=2.0,
                do="add_cliff",
                args={"id": "stairs", "x0_mm": 1600, "y0_mm": -500, "x1_mm": 1800, "y1_mm": 3500},
            ),
        ],
    )
    recorder = recording(backend)
    async with running(make_robot(backend, scenario=scenario, speed=SCRIPTED_SPEED)):
        failures = await backend.wait_for(lambda: recorder.of(MotionFailed), timeout=25)
        assert failures[0].reason == "cliff"
        sensors = await backend.wait_for(lambda: _sensors_with_cliff(backend), timeout=15)
        assert sensors.cliff_detected is True


async def _motion_state(backend: Backend, *, moving: bool) -> Any:
    state = await backend.runtime.registry.get(NORMALIZED)
    if state is None or state.telemetry.motion is None:
        return None
    return state.telemetry.motion if state.telemetry.motion.moving is moving else None


async def _sensors_with_cliff(backend: Backend) -> Any:
    state = await backend.runtime.registry.get(NORMALIZED)
    if state is None or state.telemetry.sensors is None:
        return None
    return state.telemetry.sensors if state.telemetry.sensors.cliff_detected else None


# -- injected failures ------------------------------------------------------------------


async def test_a_tool_that_never_answers_times_out(backend: Backend) -> None:
    scenario = Scenario(name="timeout", duration_s=0.0)
    scenario.faults.tool_timeout = ["robot.get_status"]
    silent = make_robot(backend, scenario=scenario)
    async with running(silent) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
        with pytest.raises((McpTimeoutError, TimeoutError)):
            await backend.runtime.call_tool(NORMALIZED, "robot_get_status", {}, timeout=2)
        # The link is still up: a different tool answers normally.
        assert json.loads(await backend.runtime.call_tool(NORMALIZED, "robot_power_get_battery", {}))["percent"] > 0


async def test_a_failing_tool_reports_an_error(backend: Backend) -> None:
    scenario = Scenario(name="tool-error", duration_s=0.0)
    scenario.faults.camera_failure = True
    broken = make_robot(backend, scenario=scenario)
    async with running(broken) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED))
        with pytest.raises((McpToolError, RuntimeError)) as failure:
            await backend.runtime.call_tool(NORMALIZED, "robot_camera_capture", {}, timeout=5)
        assert "camera failure" in str(failure.value)


async def test_motor_failure_fails_the_motion_in_flight(backend: Backend) -> None:
    scenario = Scenario(
        name="motor-failure",
        duration_s=0.0,
        steps=[
            Step(at_s=1.0, do="move", args={"distance_mm": 2000, "speed_mmps": 150}),
            Step(at_s=2.0, do="fault", args={"motor_failure": True}),
        ],
    )
    recorder = recording(backend)
    async with running(make_robot(backend, scenario=scenario, speed=SCRIPTED_SPEED)):
        failures = await backend.wait_for(lambda: recorder.of(MotionFailed), timeout=25)
        assert failures[0].reason == "motor_failure"


# -- disconnect and reconnect --------------------------------------------------------------


async def test_an_abrupt_disconnect_clears_the_registry_entry(backend: Backend, robot: SimulatedRobot) -> None:
    await backend.wait_for(lambda: backend.state(NORMALIZED))
    await robot.drop_connection(reconnect=False)
    gone = await backend.wait_for(lambda: _disconnected(backend), timeout=20)
    assert gone.connection.is_connected is False
    assert await backend.runtime.registry.list(connected_only=True) == []
    assert await backend.runtime.capabilities.get(NORMALIZED) is None


async def test_the_robot_reconnects_and_rediscovers(backend: Backend) -> None:
    reconnecting = make_robot(backend, reconnect=True, reconnect_delay_s=0.2)
    async with running(reconnecting) as simulator:
        await simulator.wait_discovered(timeout=15)
        first = await backend.wait_for(lambda: backend.state(NORMALIZED))
        await simulator.drop_connection(reconnect=True)
        await backend.wait_for(lambda: _disconnected(backend), timeout=20)

        back = await backend.wait_for(lambda: _reconnected(backend, first.connection.session_id), timeout=25)
        assert back.connection.reconnect_count == 1
        assert back.connection.is_connected
        capabilities = await backend.wait_for(lambda: backend.runtime.capabilities.get(NORMALIZED), timeout=20)
        assert len(capabilities.tool_names) == 16


async def _disconnected(backend: Backend) -> Any:
    state = await backend.runtime.registry.get(NORMALIZED)
    return state if state is not None and not state.connection.is_connected else None


async def _reconnected(backend: Backend, previous_session: str) -> Any:
    state = await backend.runtime.registry.get(NORMALIZED)
    if state is None or not state.connection.is_connected:
        return None
    return state if state.connection.session_id != previous_session else None
