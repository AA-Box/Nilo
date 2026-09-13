"""The action layer against a real server and a real simulated robot, over a real socket.

Nothing here reaches into the backend to make an assertion pass. The simulator has only
the tools and notifications a device has; the executor has only the world model the
server built from what arrived over the wire. If a behaviour cannot be produced that way,
it is not tested here — which is the point of having a simulator at all
(docs/robot-roadmap.md, "No hardware in CI").

The unit suite (``tests/robot/test_executor.py``) covers the same behaviours in
milliseconds against a scriptable fake. This file exists to prove the wiring: the real
device MCP channel, the real telemetry path, the real threaded watchdog, and the real
timing between them.

The clock is accelerated rather than mocked, so a five-second move is a fraction of a
second of test time while the *server* still sees real network timing.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("websockets")
pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from robot.actions import (  # noqa: E402
    ActionPriority,
    ActionSource,
    ActionStatus,
    FollowTargetAction,
    LookAtAction,
    MoveAction,
    RejectionReason,
    RobotActionExecutor,
    TurnAction,
)
from robot.safety import RobotSafetyPolicy, SafetyLimits  # noqa: E402
from robot.simulator import Faults, Person, Scenario, SimulatedRobot, Step, default_room  # noqa: E402
from robot.state.models import normalize_robot_id  # noqa: E402
from tests.integration.conftest import Backend  # noqa: E402
from tests.integration.test_simulator_e2e import CLOCK_SPEED, make_robot, running  # noqa: E402

ROBOT_ID = "aa:bb:cc:00:00:01"
NORMALIZED = normalize_robot_id(ROBOT_ID)

#: Limits generous enough for the simulated room, tight enough that a test can exceed them.
TEST_LIMITS = SafetyLimits(
    max_distance_mm=800,
    max_angle_deg=180,
    max_speed_mmps=300,
    min_obstacle_distance_mm=150,
    # The simulator's accelerated clock means telemetry arrives fast in wall time, but a
    # loaded CI runner still has to schedule it.
    max_sensor_age_s=5.0,
    heartbeat_timeout_s=5.0,
    action_ttl_s=30.0,
)


@pytest.fixture
async def executor(backend: Backend) -> AsyncIterator[RobotActionExecutor]:
    """The runtime's own executor, with test limits and its real threaded watchdog.

    Deliberately the production object: ``backend.runtime.actions`` is what a bridge would
    reach for, and the watchdog runs on its own thread exactly as it would in a server.
    """
    active = RobotActionExecutor(backend.runtime, policy=RobotSafetyPolicy(TEST_LIMITS))
    await active.start()
    try:
        yield active
    finally:
        await active.aclose()


@pytest.fixture
async def ready(backend: Backend, executor: RobotActionExecutor) -> AsyncIterator[SimulatedRobot]:
    """A connected, discovered simulator whose telemetry has reached the world model."""
    async with running(make_robot(backend, robot_id=ROBOT_ID)) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        yield simulator


async def _has_sensors(backend: Backend) -> Any:
    """Ready to act on: registered, discovered, and with a sensor picture in the store.

    Both halves matter. Safety keys its allow-list on the tools discovery wrote into the
    robot's identity, and gates motion on sensor freshness — and ``wait_discovered`` only
    proves the *device* served ``tools/list``, not that the server has finished recording
    the result.
    """
    state = await backend.state(NORMALIZED)
    if state is None or state.telemetry.sensors is None:
        return None
    return state if state.capabilities.has_tool(MoveAction.tool_name) else None


async def _status(executor: RobotActionExecutor, action_id: str) -> Any:
    record = executor.query(action_id)
    return record if record is not None and record.is_terminal else None


async def settled(executor: RobotActionExecutor, backend: Backend, action_id: str, *, timeout: float = 20.0) -> Any:
    """Wait for one action to reach a terminal state, and return its record."""
    return await backend.wait_for(lambda: _status(executor, action_id), timeout=timeout)


# -- the happy path -------------------------------------------------------------------------


async def test_a_move_runs_end_to_end_and_the_robot_actually_moved(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    """Submit, dispatch over device MCP, the simulator drives, the notification settles it."""
    started_x = ready.state.x_m
    record = await executor.robot(NORMALIZED).as_source(ActionSource.LLM).move(distance_mm=400)
    assert record.status is ActionStatus.SUCCEEDED
    assert record.started_at is not None and record.finished_at is not None
    assert ready.state.x_m > started_x + 0.3
    assert ("robot.motion.move", {"distance_mm": 400, "speed_mmps": 200}) in ready.tool_calls
    # And the claim came back, so the next action can have the drive.
    assert executor.queue.claimed(NORMALIZED) == frozenset()


async def test_the_world_model_learns_the_new_pose(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    before = await backend.state(NORMALIZED)
    await executor.robot(NORMALIZED).move(distance_mm=400)
    after = await backend.wait_for(
        lambda: _pose_beyond(backend, before.telemetry.pose.x_m + 0.3), timeout=15
    )
    assert after.telemetry.pose.x_m > before.telemetry.pose.x_m


async def _pose_beyond(backend: Backend, x_m: float) -> Any:
    state = await backend.state(NORMALIZED)
    if state is None or state.telemetry.pose is None:
        return None
    return state if state.telemetry.pose.x_m >= x_m else None


async def test_a_turn_and_a_head_movement_run_concurrently(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    """Different subsystems, no contention: they overlap instead of queueing."""
    handle = executor.robot(NORMALIZED)
    turn, look = await asyncio.gather(
        handle.turn(angle_deg=90, speed_dps=45), handle.look_at(x_pct=20, y_pct=70)
    )
    assert turn.status is ActionStatus.SUCCEEDED
    assert look.status is ActionStatus.SUCCEEDED
    assert ready.state.head_yaw_deg != 0


async def test_follow_runs_on_the_device_and_closes_the_distance(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """Following is closed-loop firmware work; the backend names a target and a deadline."""
    world = default_room()
    world.people = [Person(id="person-1", x_m=2.2, y_m=0.2, name="Ada")]
    scenario = Scenario(name="follow", duration_s=0.0, world=world)
    async with running(make_robot(backend, scenario=scenario, robot_id=ROBOT_ID)) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        before = simulator.state.x_m
        record = await executor.robot(NORMALIZED).follow("person-1", duration_ms=3000)
        assert record.status is ActionStatus.SUCCEEDED
        assert simulator.state.x_m > before
        assert executor.queue.claimed(NORMALIZED) == frozenset()


async def test_following_a_target_the_robot_cannot_see_fails_rather_than_driving(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    record = await executor.robot(NORMALIZED).follow("nobody", duration_ms=1000)
    assert record.status is ActionStatus.FAILED
    assert "nobody" in record.error.message


# -- safety, with real sensors --------------------------------------------------------------------


async def test_a_cliff_the_robot_reports_rejects_a_move(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """The rejection is driven by telemetry the device sent, not by a patched world model."""
    world = default_room()
    scenario = Scenario(
        name="cliff-at-the-wheels",
        duration_s=0.0,
        world=world,
        # A cliff under the robot's own sensors, so the flag is asserted while it is still.
        steps=[Step(at_s=1.0, do="add_cliff", args={"x0_mm": 600, "y0_mm": -500, "x1_mm": 900, "y1_mm": 1000})],
    )
    async with running(make_robot(backend, scenario=scenario, robot_id=ROBOT_ID, speed=4.0)) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        await backend.wait_for(lambda: _cliff_reported(backend), timeout=20)
        for source in (ActionSource.LLM, ActionSource.BEHAVIOR, ActionSource.USER, ActionSource.SYSTEM):
            record = await executor.robot(NORMALIZED).as_source(source).move(distance_mm=300)
            assert record.rejection is RejectionReason.CLIFF_HAZARD, source
        # And nothing was dispatched: the device never saw a move command.
        assert not [name for name, _ in simulator.tool_calls if name == "robot.motion.move"]


async def _cliff_reported(backend: Backend) -> Any:
    state = await backend.state(NORMALIZED)
    if state is None or state.telemetry.sensors is None:
        return None
    return state if state.telemetry.sensors.cliff_detected else None


async def test_a_move_beyond_the_configured_limit_never_reaches_the_device(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    record = await executor.robot(NORMALIZED).as_source(ActionSource.LLM).move(distance_mm=1900)
    assert record.status is ActionStatus.REJECTED
    assert record.rejection is RejectionReason.DISTANCE_LIMIT_EXCEEDED
    assert "800" in record.error.message  # the ceiling it was judged against
    assert not [name for name, _ in ready.tool_calls if name == "robot.motion.move"]


async def test_stale_sensor_state_rejects_a_move(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """The session is up and healthy-looking; only the telemetry has stopped.

    ``drop_notifications`` is the realistic version of this: firmware that keeps answering
    tool calls while its reporting task has died. The world model ages out, and motion is
    refused rather than dispatched against a picture the server can no longer vouch for.
    """
    scenario = Scenario(name="silent-sensors", duration_s=0.0)
    simulator = make_robot(backend, scenario=scenario, robot_id=ROBOT_ID)
    async with running(simulator):
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        simulator.faults.drop_notifications = True
        await asyncio.sleep(TEST_LIMITS.max_sensor_age_s + 0.5)
        record = await executor.robot(NORMALIZED).move(distance_mm=200)
        assert record.rejection in {
            RejectionReason.SENSOR_DATA_STALE,
            RejectionReason.HEARTBEAT_EXPIRED,
        }


# -- the watchdog, for real ---------------------------------------------------------------------------


async def test_an_action_the_device_never_completes_times_out_and_a_stop_is_sent(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """The real threaded watchdog, on a live session, against a device that goes quiet.

    ``drop_motion_completion`` swallows only the completion notification: the session
    stays up, telemetry keeps flowing, the motion really happens — and the backend is
    never told it ended. Nothing but the watchdog can end that action.
    """
    scenario = Scenario(name="no-completion", duration_s=0.0, faults=Faults(drop_motion_completion=True))
    simulator = make_robot(backend, scenario=scenario, robot_id=ROBOT_ID, speed=CLOCK_SPEED)
    async with running(simulator):
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        action = await executor.submit(
            MoveAction(distance_mm=100, speed_mmps=200), NORMALIZED, timeout_s=1.5
        )
        record = await settled(executor, backend, action.action_id, timeout=25.0)
        assert record.status is ActionStatus.TIMED_OUT
        assert "no completion" in record.error.message
        # A stop was attempted — best effort, and it did reach this device.
        assert await backend.wait_for(lambda: _stop_seen(simulator), timeout=10)
        assert executor.queue.claimed(NORMALIZED) == frozenset()


def _stop_seen(simulator: SimulatedRobot) -> Any:
    return [name for name, _ in simulator.tool_calls if name == "robot.motion.stop"] or None


async def test_a_device_that_never_answers_a_dispatch_times_out(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """A different failure: the frame went out and nothing came back at all."""
    scenario = Scenario(
        name="deaf-move", duration_s=0.0, faults=Faults(tool_timeout=["robot.motion.move"])
    )
    simulator = make_robot(backend, scenario=scenario, robot_id=ROBOT_ID)
    async with running(simulator):
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        action = await executor.submit(MoveAction(distance_mm=200), NORMALIZED)
        record = await settled(executor, backend, action.action_id, timeout=25.0)
        assert record.status is ActionStatus.TIMED_OUT
        assert record.error.code == "dispatch_timeout"


async def test_a_disconnect_mid_action_cancels_it(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    """The link is severed without a close frame, the way a robot losing power does.

    The action is cancelled here; what stops the *robot* is the firmware watchdog, because
    a stop cannot be delivered over a socket that is gone.
    """
    simulator = make_robot(backend, robot_id=ROBOT_ID, reconnect=False)
    async with running(simulator):
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        action = await executor.submit(MoveAction(distance_mm=800, speed_mmps=20), NORMALIZED)
        await backend.wait_for(lambda: _running(executor, action.action_id), timeout=15)
        await simulator.drop_connection(reconnect=False)
        record = await settled(executor, backend, action.action_id, timeout=25.0)
        assert record.status is ActionStatus.CANCELLED
        assert executor.queue.claimed(NORMALIZED) == frozenset()


def _running(executor: RobotActionExecutor, action_id: str) -> Any:
    record = executor.query(action_id)
    return record if record is not None and record.status is ActionStatus.RUNNING else None


# -- arbitration ------------------------------------------------------------------------------------------


async def test_two_drive_actions_run_one_after_the_other(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    first = await executor.submit(MoveAction(distance_mm=200, speed_mmps=100), NORMALIZED)
    second = await executor.submit(TurnAction(angle_deg=45, speed_dps=45), NORMALIZED)
    await backend.wait_for(lambda: _running(executor, first.action_id), timeout=15)
    assert executor.query(second.action_id).status is ActionStatus.PENDING
    assert not [name for name, _ in ready.tool_calls if name == "robot.motion.turn"]
    assert (await settled(executor, backend, first.action_id)).status is ActionStatus.SUCCEEDED
    assert (await settled(executor, backend, second.action_id)).status is ActionStatus.SUCCEEDED


async def test_a_user_request_preempts_a_running_behaviour(
    backend: Backend, executor: RobotActionExecutor
) -> None:
    world = default_room()
    world.people = [Person(id="person-1", x_m=2.2, y_m=0.2, name="Ada")]
    scenario = Scenario(name="preempt", duration_s=0.0, world=world)
    simulator = make_robot(backend, scenario=scenario, robot_id=ROBOT_ID)
    async with running(simulator):
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        behaviour = await executor.submit(
            FollowTargetAction(target_id="person-1", duration_ms=20_000),
            NORMALIZED,
            source=ActionSource.BEHAVIOR,
            priority=ActionPriority.LOW,
        )
        await backend.wait_for(lambda: _running(executor, behaviour.action_id), timeout=15)
        user = await executor.submit(
            MoveAction(distance_mm=200, speed_mmps=200),
            NORMALIZED,
            source=ActionSource.USER,
            priority=ActionPriority.HIGH,
        )
        preempted = await settled(executor, backend, behaviour.action_id)
        assert preempted.status is ActionStatus.CANCELLED
        assert preempted.error.code == "preempted"
        assert (await settled(executor, backend, user.action_id)).status is ActionStatus.SUCCEEDED


# -- the emergency stop ----------------------------------------------------------------------------------------


async def test_an_emergency_stop_halts_the_robot_and_refuses_everything_after(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    running_action = await executor.submit(MoveAction(distance_mm=800, speed_mmps=20), NORMALIZED)
    queued = await executor.submit(TurnAction(angle_deg=45), NORMALIZED)
    await backend.wait_for(lambda: _running(executor, running_action.action_id), timeout=15)

    stop = await executor.emergency_stop(NORMALIZED, "a person stepped in front")
    assert stop.status is ActionStatus.SUCCEEDED
    assert (await settled(executor, backend, running_action.action_id)).status is ActionStatus.CANCELLED
    assert (await settled(executor, backend, queued.action_id)).status is ActionStatus.CANCELLED

    # The robot itself stopped, not merely the bookkeeping.
    await backend.wait_for(lambda: (not ready.state.moving) or None, timeout=10)

    for source in (ActionSource.LLM, ActionSource.USER, ActionSource.BEHAVIOR, ActionSource.SYSTEM):
        refused = await executor.robot(NORMALIZED).as_source(source).move(distance_mm=100)
        assert refused.rejection is RejectionReason.EMERGENCY_STOP_ENGAGED, source

    assert await executor.clear_emergency_stop(NORMALIZED)
    resumed = await executor.robot(NORMALIZED).look_at(x_pct=40, y_pct=60)
    assert resumed.status is ActionStatus.SUCCEEDED


async def test_a_stop_is_dispatched_ahead_of_a_full_queue(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    """Rule 2: a stop's latency must not be a function of queue depth."""
    await executor.submit(MoveAction(distance_mm=800, speed_mmps=20), NORMALIZED)
    for angle in (10, 20, 30, 40):
        await executor.submit(TurnAction(angle_deg=angle, speed_dps=20), NORMALIZED)
    record = await executor.robot(NORMALIZED).stop("the user asked")
    assert record.status is ActionStatus.SUCCEEDED
    assert _stop_seen(ready)


# -- duplicate and late device events ------------------------------------------------------------------------------------


async def test_a_replayed_completion_does_not_disturb_a_later_action(
    backend: Backend, executor: RobotActionExecutor, ready: SimulatedRobot
) -> None:
    """A reconnect that replays a notification, or firmware that retries one.

    The replay is injected the only way a device can inject anything: as a real MCP
    notification frame on the real channel.
    """
    first = await executor.robot(NORMALIZED).move(distance_mm=200)
    assert first.status is ActionStatus.SUCCEEDED
    stale_device_id = executor.query(first.action_id).device_action_id

    second = await executor.submit(MoveAction(distance_mm=400, speed_mmps=20), NORMALIZED)
    await backend.wait_for(lambda: _running(executor, second.action_id), timeout=15)

    for _ in range(3):
        await ready._notify(
            "notifications/motion_completed", {"action_id": stale_device_id, "kind": "move"}
        )
    await asyncio.sleep(0.3)
    assert executor.query(second.action_id).status is ActionStatus.RUNNING
    assert executor.query(first.action_id).status is ActionStatus.SUCCEEDED
    assert (await settled(executor, backend, second.action_id)).status is ActionStatus.SUCCEEDED


# -- the runtime's own executor --------------------------------------------------------------------------------------------


async def test_the_runtime_exposes_a_semantic_handle(backend: Backend) -> None:
    """``await runtime.robot(id).move(...)`` is the whole public surface, wired for real."""
    async with running(make_robot(backend, robot_id=ROBOT_ID)) as simulator:
        await simulator.wait_discovered(timeout=15)
        await backend.wait_for(lambda: _has_sensors(backend), timeout=15)
        assert backend.runtime.actions.limits.max_distance_mm > 0  # the shipped defaults apply
        record = await backend.runtime.robot(NORMALIZED).look_at(x_pct=30, y_pct=60)
        assert record.status is ActionStatus.SUCCEEDED
        assert simulator.state.head_yaw_deg != 0


async def test_closing_the_runtime_shuts_the_executor_down(backend: Backend) -> None:
    executor = backend.runtime.actions
    await executor.submit(LookAtAction(), NORMALIZED)
    with contextlib.suppress(Exception):
        await backend.runtime.aclose()
    assert executor.closed
    assert not executor.watchdog.running
