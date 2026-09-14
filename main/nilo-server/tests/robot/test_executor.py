"""The executor end to end, against a scriptable device that never opens a socket.

Everything the Phase 3 acceptance criteria name is here: priority, conflicts, preemption,
timeouts, disconnect, safety rejection, emergency stop, stale sensor state, cancellation
and duplicate completion events. The integration suite runs the same behaviours against a
real server and the real simulator; this one runs them in milliseconds and can make the
device do things a real one only manages on a bad day.

The watchdog is *driven* rather than started: ``executor.watchdog.tick(now=...)`` fires
deadlines at an exact simulated instant, so a timeout test asserts a fact instead of
waiting out a real second. ``tests/robot/test_watchdog.py`` covers the threaded half, and
``tests/integration/`` covers the two together.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from robot.actions.executor import RobotActionExecutor
from robot.actions.model import (
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    StopAction,
    TurnAction,
)
from robot.devices.mcp import McpToolError
from robot.events.types import (
    ActionFinished,
    ActionStarted,
    ActionSubmitted,
    EmergencyStopChanged,
    MotionCompleted,
    MotionFailed,
    RobotEvent,
)
from robot.safety import RobotSafetyPolicy, SafetyLimits, Watchdog
from robot.state.actions import (
    ActionPriority,
    ActionSource,
    ActionStatus,
    RejectionReason,
    Resource,
)
from robot.state.models import DisconnectReason, RobotSensorState, utcnow
from tests.robot.conftest import ROBOT_ID, FakeActionRuntime, robot_state

MOVE_TOOL = MoveAction.tool_name
STOP_TOOL = StopAction.tool_name
TURN_TOOL = TurnAction.tool_name


@pytest.fixture
async def runtime() -> FakeActionRuntime:
    fake = FakeActionRuntime()
    try:
        yield fake
    finally:
        await fake.aclose()


@pytest.fixture
async def executor(runtime) -> RobotActionExecutor:
    """An executor whose watchdog never ticks on its own, so time is the test's to move."""
    active = RobotActionExecutor(
        runtime,
        policy=RobotSafetyPolicy(SafetyLimits()),
        watchdog=Watchdog(interval_s=60.0),
        start_watchdog=False,
    )
    await active.start()
    try:
        yield active
    finally:
        await active.aclose()


async def settle(runtime: FakeActionRuntime, rounds: int = 12) -> None:
    """Let the pump, the dispatch tasks and the bus catch up. Deterministic, not timed."""
    import asyncio

    for _ in range(rounds):
        await asyncio.sleep(0)
    await runtime.events.drain()
    for _ in range(rounds):
        await asyncio.sleep(0)


async def complete(runtime: FakeActionRuntime, action, *, kind: str = "move") -> None:
    """Report the completion the device would send for an action that is running."""
    assert action.device_action_id, "the action was never dispatched"
    await runtime.events.publish(
        MotionCompleted(robot_id=action.robot_id, action_id=action.device_action_id, kind=kind)
    )
    await settle(runtime)


def collect(runtime: FakeActionRuntime, *types: type[RobotEvent]) -> list[RobotEvent]:
    seen: list[RobotEvent] = []
    runtime.events.subscribe(types if len(types) > 1 else types[0], seen.append)
    return seen


# -- the happy path --------------------------------------------------------------------------------


async def test_a_move_is_admitted_dispatched_and_completed_by_the_device(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID, source=ActionSource.LLM)
    assert action.status is ActionStatus.PENDING  # submit never waits for hardware
    await settle(runtime)
    assert action.status is ActionStatus.RUNNING
    assert runtime.called(MOVE_TOOL) == ({"distance_mm": 300, "speed_mmps": 200},)
    await complete(runtime, action)
    record = action.record()
    assert record.status is ActionStatus.SUCCEEDED
    assert record.started_at is not None and record.finished_at is not None
    assert executor.queue.claimed(ROBOT_ID) == frozenset()


async def test_submit_returns_without_waiting_for_the_device(executor, runtime) -> None:
    """An LLM tool handler must return in microseconds; the chat loop pins a worker
    otherwise (``core/connection.py``)."""
    runtime.latency_s = 0.2
    action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    assert action.status is ActionStatus.PENDING
    assert runtime.calls == []


async def test_an_action_whose_reply_is_its_completion_succeeds_at_once(executor, runtime) -> None:
    """A servo angle or a face change has no notification to wait for."""
    action = await executor.submit(LookAtAction(x_pct=20, y_pct=70), ROBOT_ID)
    await settle(runtime)
    assert action.status is ActionStatus.SUCCEEDED
    assert executor.queue.claimed(ROBOT_ID) == frozenset()


async def test_the_dispatch_carries_an_explicit_short_timeout(executor, runtime) -> None:
    """Never the inherited 30 s default, which nothing else overrides."""
    seen: list[float | None] = []
    original = runtime.call_tool

    async def record_timeout(robot_id, name, arguments=None, *, timeout=None):
        seen.append(timeout)
        return await original(robot_id, name, arguments, timeout=timeout)

    runtime.call_tool = record_timeout  # type: ignore[method-assign]
    await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    assert seen and all(value is not None and value <= 5.0 for value in seen)


# -- safety rejection --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    sorted(ActionSource, key=lambda source: source.value),
)
async def test_a_cliff_rejects_a_move_whoever_asked_and_nothing_is_dispatched(executor, runtime, source) -> None:
    runtime.set_state(robot_state(sensors=RobotSensorState(cliff_detected=True)))
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID, source=source)
    assert action.status is ActionStatus.REJECTED
    assert action.record().rejection is RejectionReason.CLIFF_HAZARD
    await settle(runtime)
    assert runtime.called(MOVE_TOOL) == ()


async def test_a_move_beyond_the_limit_is_rejected_and_the_reason_reaches_the_caller(executor) -> None:
    action = await executor.submit(MoveAction(distance_mm=9000), ROBOT_ID, source=ActionSource.LLM)
    record = action.record()
    assert record.status is ActionStatus.REJECTED
    assert record.rejection is RejectionReason.DISTANCE_LIMIT_EXCEEDED
    assert "9000" in record.error.message
    assert executor.query(action.action_id) == record  # and it is queryable afterwards


async def test_stale_sensor_state_rejects_a_move(executor, runtime) -> None:
    stale = RobotSensorState(updated_at=utcnow() - timedelta(seconds=30), readings={"front_mm": 2000.0})
    runtime.set_state(robot_state(sensors=stale))
    action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    assert action.record().rejection is RejectionReason.SENSOR_DATA_STALE


async def test_safety_is_evaluated_again_between_queueing_and_dispatch(executor, runtime) -> None:
    """A cliff that appears after admission still stops the move. The queued action is
    rejected at the second evaluation, not dispatched on the strength of the first."""
    blocker = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    queued = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
    assert queued.status is ActionStatus.PENDING
    runtime.set_state(robot_state(sensors=RobotSensorState(cliff_detected=True)))
    await complete(runtime, blocker)  # frees the drive, so the turn is dispatched next
    assert queued.record().rejection is RejectionReason.CLIFF_HAZARD
    assert runtime.called(TURN_TOOL) == ()


async def test_an_action_that_outlives_its_ttl_in_the_queue_is_rejected(runtime) -> None:
    executor = RobotActionExecutor(
        runtime,
        policy=RobotSafetyPolicy(SafetyLimits(action_ttl_s=0.5)),
        watchdog=Watchdog(interval_s=60.0),
        start_watchdog=False,
    )
    await executor.start()
    try:
        import asyncio

        blocker = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
        await settle(runtime)
        queued = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
        await asyncio.sleep(0.6)
        await complete(runtime, blocker)
        assert queued.record().rejection is RejectionReason.TTL_EXPIRED
    finally:
        await executor.aclose()


async def test_the_rate_limit_refuses_a_flood_of_motion_commands(runtime) -> None:
    executor = RobotActionExecutor(
        runtime,
        policy=RobotSafetyPolicy(SafetyLimits(max_motion_per_window=2)),
        watchdog=Watchdog(interval_s=60.0),
        start_watchdog=False,
    )
    await executor.start()
    try:
        outcomes = []
        for _ in range(4):
            action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID, source=ActionSource.LLM)
            await settle(runtime)
            if action.device_action_id:
                await complete(runtime, action)
            outcomes.append(action.record())
        assert [record.status for record in outcomes[:2]] == [ActionStatus.SUCCEEDED] * 2
        assert outcomes[-1].rejection is RejectionReason.RATE_LIMIT_EXCEEDED
    finally:
        await executor.aclose()


# -- priority, conflicts, preemption -------------------------------------------------------------------------------


async def test_conflicting_actions_run_one_at_a_time(executor, runtime) -> None:
    first = await executor.submit(MoveAction(distance_mm=200), ROBOT_ID)
    second = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
    await settle(runtime)
    assert first.status is ActionStatus.RUNNING
    assert second.status is ActionStatus.PENDING
    assert runtime.called(TURN_TOOL) == ()
    assert executor.queue.claimed(ROBOT_ID) == frozenset({Resource.DRIVE})
    await complete(runtime, first)
    assert second.status is ActionStatus.RUNNING
    assert runtime.called(TURN_TOOL) == ({"angle_deg": 90, "speed_dps": 90},)


async def test_actions_on_different_subsystems_run_concurrently(executor, runtime) -> None:
    move = await executor.submit(MoveAction(distance_mm=200), ROBOT_ID)
    lift = await executor.submit(LiftAction(height_pct=80), ROBOT_ID)
    face = await executor.submit(ExpressionAction(emotion="happy"), ROBOT_ID)
    await settle(runtime)
    assert move.status is ActionStatus.RUNNING
    assert lift.status is ActionStatus.SUCCEEDED
    assert face.status is ActionStatus.SUCCEEDED


async def test_the_queue_dispatches_in_priority_order(executor, runtime) -> None:
    blocker = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    low = await executor.submit(TurnAction(angle_deg=10), ROBOT_ID, priority=ActionPriority.LOW)
    high = await executor.submit(TurnAction(angle_deg=20), ROBOT_ID, priority=ActionPriority.HIGH)
    await complete(runtime, blocker)
    assert high.status is ActionStatus.RUNNING
    assert low.status is ActionStatus.PENDING
    assert runtime.called(TURN_TOOL) == ({"angle_deg": 20, "speed_dps": 90},)


async def test_a_higher_priority_action_preempts_a_running_one(executor, runtime) -> None:
    """A behaviour is following someone; the user says "come here". The user wins, and the
    behaviour's action ends as CANCELLED — an observable transition, not a dropped request."""
    behaviour = await executor.submit(
        FollowTargetAction(target_id="person-1"),
        ROBOT_ID,
        source=ActionSource.BEHAVIOR,
        priority=ActionPriority.LOW,
    )
    await settle(runtime)
    assert behaviour.status is ActionStatus.RUNNING
    user = await executor.submit(
        MoveAction(distance_mm=300), ROBOT_ID, source=ActionSource.USER, priority=ActionPriority.HIGH
    )
    await settle(runtime)
    assert behaviour.status is ActionStatus.CANCELLED
    assert behaviour.record().error.code == "preempted"
    assert user.status is ActionStatus.RUNNING
    assert runtime.called(STOP_TOOL)  # and the robot was told to stop what it was doing


async def test_an_equal_priority_action_waits_instead_of_preempting(executor, runtime) -> None:
    running = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    peer = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
    await settle(runtime)
    assert running.status is ActionStatus.RUNNING
    assert peer.status is ActionStatus.PENDING


async def test_conflict_detection_is_queryable_before_submitting(executor, runtime) -> None:
    running = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    blocked = executor.conflicts(TurnAction(angle_deg=90), ROBOT_ID)
    assert [record.action_id for record in blocked] == [running.action_id]
    assert executor.conflicts(LookAtAction(), ROBOT_ID) == ()


# -- timeouts ---------------------------------------------------------------------------------------------------------


async def test_an_action_that_never_reports_completion_times_out_and_a_stop_is_attempted(
    executor, runtime
) -> None:
    """The device accepted the command and went quiet. The watchdog is what ends it."""
    action = await executor.submit(MoveAction(distance_mm=300, speed_mmps=200), ROBOT_ID)
    await settle(runtime)
    assert action.status is ActionStatus.RUNNING
    assert executor.watchdog.deadline(action.action_id) is not None
    executor.watchdog.tick(now=executor.watchdog.deadline(action.action_id) + 0.1)
    await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.TIMED_OUT
    assert "no completion" in record.error.message
    assert runtime.called(STOP_TOOL)
    assert executor.queue.claimed(ROBOT_ID) == frozenset()


async def test_a_device_that_never_acknowledges_a_dispatch_times_out(executor, runtime) -> None:
    """A different failure from the one above, and it gets a different message: the frame
    was sent and nothing came back at all."""
    runtime.never_answers(MOVE_TOOL)
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.TIMED_OUT
    assert record.error.code == "dispatch_timeout"
    assert runtime.called(STOP_TOOL)


async def test_the_timeout_budget_follows_the_distance(executor, runtime) -> None:
    short = await executor.submit(MoveAction(distance_mm=100, speed_mmps=200), ROBOT_ID)
    long = await executor.submit(MoveAction(distance_mm=1000, speed_mmps=200), ROBOT_ID)
    assert long.timeout_s > short.timeout_s


async def test_a_caller_timeout_beyond_the_ceiling_is_held_at_the_ceiling(executor) -> None:
    """Clamping *down* only: a caller cannot buy ten minutes of unsupervised motion."""
    action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID, timeout_s=9999.0)
    assert action.timeout_s == executor.limits.max_timeout_s


async def test_a_device_error_fails_the_action_rather_than_timing_it_out(executor, runtime) -> None:
    runtime.replies[MOVE_TOOL] = McpToolError("the motors reported a fault")
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.FAILED
    assert "motors" in record.error.message


async def test_an_accepted_command_with_no_action_id_fails_because_it_cannot_be_supervised(
    executor, runtime
) -> None:
    runtime.replies[MOVE_TOOL] = {"accepted": True}
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    assert action.record().error.code == "no_device_action_id"
    assert runtime.called(STOP_TOOL)


# -- device feedback ---------------------------------------------------------------------------------------------------


async def test_a_motion_failure_from_the_device_fails_the_action_with_its_reason(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    await runtime.events.publish(
        MotionFailed(
            robot_id=ROBOT_ID,
            action_id=action.device_action_id,
            reason="obstacle",
            detail="obstacle chair-1 in the path",
        )
    )
    await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.FAILED
    assert record.error.code == "obstacle"
    assert "chair-1" in record.error.message


async def test_a_device_cancellation_reports_as_cancelled_not_failed(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    await runtime.events.publish(
        MotionFailed(robot_id=ROBOT_ID, action_id=action.device_action_id, reason="cancelled")
    )
    await settle(runtime)
    assert action.status is ActionStatus.CANCELLED


async def test_a_duplicate_completion_event_is_dropped(executor, runtime) -> None:
    """Firmware that retries a notification, or a reconnect that replays one, must not
    settle an action twice — and must not settle a *later* action by accident."""
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    device_id = action.device_action_id
    await complete(runtime, action)
    finished_at = action.record().finished_at
    for _ in range(3):
        await runtime.events.publish(MotionCompleted(robot_id=ROBOT_ID, action_id=device_id))
        await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.SUCCEEDED
    assert record.finished_at == finished_at


async def test_a_replayed_completion_does_not_settle_a_later_action(executor, runtime) -> None:
    first = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    stale_device_id = first.device_action_id
    await complete(runtime, first)
    second = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    assert second.device_action_id != stale_device_id
    await runtime.events.publish(MotionCompleted(robot_id=ROBOT_ID, action_id=stale_device_id))
    await settle(runtime)
    assert second.status is ActionStatus.RUNNING  # untouched


async def test_a_completion_for_an_unknown_action_is_ignored(executor, runtime) -> None:
    await runtime.events.publish(MotionCompleted(robot_id=ROBOT_ID, action_id="never-existed"))
    await settle(runtime)
    assert executor.list_actions() == ()


# -- cancellation -----------------------------------------------------------------------------------------------------------


async def test_cancelling_a_pending_action_never_dispatches_it(executor, runtime) -> None:
    blocker = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    queued = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
    assert await executor.cancel(queued.action_id) is not None
    await complete(runtime, blocker)
    assert queued.status is ActionStatus.CANCELLED
    assert runtime.called(TURN_TOOL) == ()


async def test_cancelling_a_running_action_attempts_a_stop(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    record = await executor.cancel(action.action_id, reason="the user changed their mind")
    await settle(runtime)
    assert record.status is ActionStatus.CANCELLED
    assert "changed their mind" in record.error.message
    assert runtime.called(STOP_TOOL)
    assert executor.watchdog.deadline(action.action_id) is None


async def test_cancelling_twice_or_cancelling_something_finished_returns_none(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    await settle(runtime)
    assert await executor.cancel(action.action_id) is not None
    assert await executor.cancel(action.action_id) is None
    assert await executor.cancel("no-such-action") is None


async def test_cancel_all_empties_the_queue_and_stops_what_is_running(executor, runtime) -> None:
    running = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    queued = [await executor.submit(TurnAction(angle_deg=angle), ROBOT_ID) for angle in (10, 20, 30)]
    cancelled = await executor.cancel_all(ROBOT_ID, reason="shutting down")
    await settle(runtime)
    assert len(cancelled) == 4
    assert running.status is ActionStatus.CANCELLED
    assert all(item.status is ActionStatus.CANCELLED for item in queued)
    assert executor.pending(ROBOT_ID) == ()
    assert executor.queue.claimed(ROBOT_ID) == frozenset()
    assert runtime.called(STOP_TOOL)


async def test_cancel_all_is_scoped_to_one_robot_when_asked(executor, runtime) -> None:
    runtime.set_state(robot_state("robot-b"))
    mine = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    theirs = await executor.submit(MoveAction(distance_mm=800), "robot-b")
    await settle(runtime)
    await executor.cancel_all(ROBOT_ID)
    await settle(runtime)
    assert mine.status is ActionStatus.CANCELLED
    assert theirs.status is ActionStatus.RUNNING


# -- stop and the emergency stop -----------------------------------------------------------------------------------------------


async def test_a_stop_bypasses_the_queue_entirely(executor, runtime) -> None:
    """Rule 2: queueing a stop would make its latency a function of queue depth."""
    running = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    for angle in (10, 20, 30):
        await executor.submit(TurnAction(angle_deg=angle), ROBOT_ID)
    stop = await executor.submit(StopAction(), ROBOT_ID, priority=ActionPriority.EMERGENCY)
    await settle(runtime)
    assert stop.status is ActionStatus.SUCCEEDED
    assert running.status is ActionStatus.CANCELLED
    assert runtime.called(STOP_TOOL)


async def test_a_stop_is_accepted_with_a_cliff_asserted(executor, runtime) -> None:
    """A robot that will not stop because a sensor says it is unsafe to move is the wrong
    failure (docs/safety-model.md, "Emergency stop", rule 1)."""
    runtime.set_state(robot_state(sensors=RobotSensorState(cliff_detected=True)))
    stop = await executor.submit(StopAction(), ROBOT_ID, source=ActionSource.LLM)
    await settle(runtime)
    assert stop.status is ActionStatus.SUCCEEDED


async def test_an_emergency_stop_cancels_everything_and_refuses_new_work(executor, runtime) -> None:
    running = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    queued = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)

    stop = await executor.emergency_stop(ROBOT_ID, "a person stepped in front")
    await settle(runtime)
    assert stop.status is ActionStatus.SUCCEEDED
    assert running.status is ActionStatus.CANCELLED
    assert queued.status is ActionStatus.CANCELLED
    assert executor.emergency_stopped(ROBOT_ID)
    assert runtime.called(STOP_TOOL)

    for source in sorted(ActionSource, key=lambda item: item.value):
        if source is ActionSource.SAFETY:
            continue
        refused = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID, source=source)
        assert refused.record().rejection is RejectionReason.EMERGENCY_STOP_ENGAGED, source


async def test_nothing_resumes_until_the_latch_is_cleared_explicitly(executor, runtime) -> None:
    await executor.emergency_stop(ROBOT_ID)
    await settle(runtime)
    assert (await executor.submit(LookAtAction(), ROBOT_ID)).status is ActionStatus.REJECTED
    assert await executor.clear_emergency_stop(ROBOT_ID) is True
    action = await executor.submit(LookAtAction(), ROBOT_ID)
    await settle(runtime)
    assert action.status is ActionStatus.SUCCEEDED
    assert await executor.clear_emergency_stop(ROBOT_ID) is False


async def test_the_emergency_stop_is_per_robot(executor, runtime) -> None:
    runtime.set_state(robot_state("robot-b"))
    await executor.emergency_stop(ROBOT_ID)
    await settle(runtime)
    other = await executor.submit(MoveAction(distance_mm=100), "robot-b")
    await settle(runtime)
    assert other.status is ActionStatus.RUNNING


async def test_the_llm_cannot_clear_a_latch_by_asking_for_something_else(executor, runtime) -> None:
    """There is no path from an action request to a cleared latch. Only the explicit
    management call clears it."""
    await executor.emergency_stop(ROBOT_ID, "cliff")
    await settle(runtime)
    for spec in (MoveAction(distance_mm=1), TurnAction(angle_deg=1), AnimationAction(name="ignore-the-stop")):
        refused = await executor.submit(spec, ROBOT_ID, source=ActionSource.LLM)
        assert refused.status is ActionStatus.REJECTED
    assert executor.emergency_stopped(ROBOT_ID)


# -- disconnect and supervision --------------------------------------------------------------------------------------------------


async def test_a_disconnect_cancels_everything_for_that_robot(executor, runtime) -> None:
    from robot.events.types import RobotDisconnected

    running = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    queued = await executor.submit(TurnAction(angle_deg=90), ROBOT_ID)
    await runtime.events.publish(
        RobotDisconnected(robot_id=ROBOT_ID, session_id="session-1", reason=DisconnectReason.ERROR)
    )
    await settle(runtime)
    assert running.status is ActionStatus.CANCELLED
    assert queued.status is ActionStatus.CANCELLED
    assert executor.queue.claimed(ROBOT_ID) == frozenset()


async def test_a_disconnected_robot_refuses_new_work(executor, runtime) -> None:
    runtime.set_state(robot_state(connected=False))
    action = await executor.submit(MoveAction(distance_mm=100), ROBOT_ID)
    assert action.record().rejection is RejectionReason.DEVICE_DISCONNECTED


async def test_the_supervisor_stops_a_running_motion_when_the_heartbeat_lapses(executor, runtime) -> None:
    """"Stop motion when heartbeat expires", for an action that was legal when admitted."""
    action = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    assert action.status is ActionStatus.RUNNING
    runtime.set_state(robot_state(last_seen=utcnow() - timedelta(seconds=60)))
    await executor.supervise()
    await settle(runtime)
    record = action.record()
    assert record.status is ActionStatus.CANCELLED
    assert record.rejection is RejectionReason.HEARTBEAT_EXPIRED
    assert runtime.called(STOP_TOOL)


async def test_the_supervisor_stops_a_running_motion_when_a_cliff_appears(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    runtime.set_state(robot_state(sensors=RobotSensorState(cliff_detected=True)))
    await executor.supervise()
    await settle(runtime)
    assert action.record().rejection is RejectionReason.CLIFF_HAZARD
    assert runtime.called(STOP_TOOL)


async def test_the_supervisory_sweep_is_registered_on_the_watchdog(executor) -> None:
    """The pass above is wired to the watchdog thread, not merely callable by a test."""
    assert executor.watchdog.sweeps == 1


async def test_the_supervisor_leaves_a_healthy_motion_alone(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    for _ in range(3):
        await executor.supervise()
        await settle(runtime)
    assert action.status is ActionStatus.RUNNING


async def test_the_supervisor_does_not_pretend_it_can_stop_a_gone_robot(executor, runtime) -> None:
    """A stop over a closed session cannot arrive. The action is still cancelled, and the
    log says the firmware watchdog is what stops the robot."""
    action = await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    before = len(runtime.called(STOP_TOOL))
    runtime.set_state(robot_state(connected=False))
    await executor.supervise()
    await settle(runtime)
    assert action.record().rejection is RejectionReason.DEVICE_DISCONNECTED
    assert len(runtime.called(STOP_TOOL)) == before


# -- query and events ------------------------------------------------------------------------------------------------------------------


async def test_an_action_is_queryable_by_id_while_live_and_after_it_retires(executor, runtime) -> None:
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID, source=ActionSource.LLM)
    assert executor.query(action.action_id).status is ActionStatus.PENDING
    await settle(runtime)
    assert executor.query(action.action_id).status is ActionStatus.RUNNING
    await complete(runtime, action)
    record = executor.query(action.action_id)
    assert record.status is ActionStatus.SUCCEEDED
    assert record.source is ActionSource.LLM
    assert executor.query("no-such-action") is None


async def test_listing_actions_filters_by_robot_and_status(executor, runtime) -> None:
    runtime.set_state(robot_state("robot-b"))
    mine = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await executor.submit(MoveAction(distance_mm=9999), "robot-b")  # rejected
    await settle(runtime)
    assert {record.action_id for record in executor.list_actions(ROBOT_ID)} == {mine.action_id}
    rejected = executor.list_actions(status=ActionStatus.REJECTED)
    assert len(rejected) == 1 and rejected[0].robot_id == "robot-b"


async def test_the_lifecycle_is_published_on_the_event_bus(executor, runtime) -> None:
    events = collect(runtime, ActionSubmitted, ActionStarted, ActionFinished)
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    await complete(runtime, action)
    kinds = [type(event).__name__ for event in events]
    assert kinds == ["ActionSubmitted", "ActionStarted", "ActionFinished"]
    assert events[-1].action.status is ActionStatus.SUCCEEDED


async def test_a_rejection_is_published_too(executor, runtime) -> None:
    """The audit trail shows what was asked for, not only what ran."""
    events = collect(runtime, ActionSubmitted, ActionFinished)
    await executor.submit(MoveAction(distance_mm=9999), ROBOT_ID, source=ActionSource.LLM)
    await settle(runtime)
    assert [type(event).__name__ for event in events] == ["ActionSubmitted", "ActionFinished"]
    assert events[-1].action.rejection is RejectionReason.DISTANCE_LIMIT_EXCEEDED


async def test_the_emergency_stop_is_announced(executor, runtime) -> None:
    events = collect(runtime, EmergencyStopChanged)
    await executor.emergency_stop(ROBOT_ID, "a person stepped in front")
    await settle(runtime)
    await executor.clear_emergency_stop(ROBOT_ID)
    await settle(runtime)
    assert [event.engaged for event in events] == [True, False]
    assert events[0].reason == "a person stepped in front"


# -- the semantic surface --------------------------------------------------------------------------------------------------------------------


async def test_await_robot_move_waits_for_the_robot_to_finish(executor, runtime) -> None:
    import asyncio

    handle = executor.robot(ROBOT_ID)
    waiting = asyncio.create_task(handle.move(distance_mm=300))
    await settle(runtime)
    running = executor.list_actions(ROBOT_ID, status=ActionStatus.RUNNING)
    assert len(running) == 1
    await runtime.events.publish(
        MotionCompleted(robot_id=ROBOT_ID, action_id=executor.registry.get(running[0].action_id).device_action_id)
    )
    record = await asyncio.wait_for(waiting, timeout=2.0)
    assert record.status is ActionStatus.SUCCEEDED


async def test_the_semantic_methods_all_go_through_the_executor(executor, runtime) -> None:
    """No second path to a device: every method builds a spec and submits it."""
    handle = executor.robot(ROBOT_ID)
    await handle.look_at(x_pct=20, y_pct=70)
    await handle.head_angle(pitch_deg=15)
    await handle.lift(height_pct=40)
    await handle.set_expression("happy")
    await handle.play_animation("greet", duration_ms=800)
    await handle.capture_image()
    await handle.stop()
    await settle(runtime)
    dispatched = {name for _, name, _ in runtime.calls}
    assert dispatched == {
        LookAtAction.tool_name,
        "robot_head_set_angle",
        LiftAction.tool_name,
        ExpressionAction.tool_name,
        AnimationAction.tool_name,
        CaptureImageAction.tool_name,
        STOP_TOOL,
    }
    assert all(record.status is ActionStatus.SUCCEEDED for record in executor.list_actions(ROBOT_ID))


async def test_a_rejection_comes_back_as_a_record_not_an_exception(executor) -> None:
    """A refusal is an outcome. A caller that had to catch it would end up swallowing it."""
    record = await executor.robot(ROBOT_ID).move(distance_mm=9999)
    assert record.status is ActionStatus.REJECTED
    assert record.rejection is RejectionReason.DISTANCE_LIMIT_EXCEEDED


async def test_wait_false_returns_as_soon_as_the_action_is_admitted(executor, runtime) -> None:
    """What the LLM bridge uses: the model is told what was accepted, not what finished."""
    record = await executor.robot(ROBOT_ID).move(distance_mm=300, wait=False)
    assert record.status is ActionStatus.PENDING
    await settle(runtime)
    assert executor.query(record.action_id).status is ActionStatus.RUNNING


async def test_a_handle_can_attribute_its_actions_to_another_source(executor, runtime) -> None:
    handle = executor.robot(ROBOT_ID).as_source(ActionSource.LLM)
    await handle.look_at()
    await settle(runtime)
    assert executor.list_actions(ROBOT_ID)[0].source is ActionSource.LLM


async def test_a_different_source_does_not_buy_a_different_answer(executor) -> None:
    for source in sorted(ActionSource, key=lambda item: item.value):
        record = await executor.robot(ROBOT_ID).as_source(source).move(distance_mm=9999)
        assert record.rejection is RejectionReason.DISTANCE_LIMIT_EXCEEDED, source


# -- lifecycle of the executor itself -----------------------------------------------------------------------------------------------------------


async def test_closing_twice_is_safe(runtime) -> None:
    executor = RobotActionExecutor(runtime, watchdog=Watchdog(interval_s=60.0), start_watchdog=False)
    await executor.start()
    await executor.aclose()
    await executor.aclose()
    assert executor.closed


async def test_a_closed_executor_refuses_new_work_instead_of_swallowing_it(runtime) -> None:
    """The pump is gone after close, so an accepted action would never be dispatched and a
    caller awaiting it would wait forever. Refuse loudly instead."""
    executor = RobotActionExecutor(runtime, watchdog=Watchdog(interval_s=60.0), start_watchdog=False)
    await executor.start()
    await executor.aclose()
    with pytest.raises(RuntimeError):
        await executor.submit(LookAtAction(), ROBOT_ID)


async def test_an_action_cancelled_while_the_device_answers_is_not_correlated(executor, runtime) -> None:
    """Binding a device id onto an already-retired action would leave a mapping nothing can
    resolve or clean up. The completion for it resolves to nothing, as it should."""
    import asyncio

    runtime.latency_s = 0.05
    action = await executor.submit(MoveAction(distance_mm=300), ROBOT_ID)
    await settle(runtime)
    await executor.cancel(action.action_id)
    await asyncio.sleep(0.15)
    await settle(runtime)
    assert action.status is ActionStatus.CANCELLED
    assert action.device_action_id is None
    assert executor.registry.by_device_action(ROBOT_ID, "dev-1") is None


async def test_the_executor_starts_itself_on_first_use(runtime) -> None:
    executor = RobotActionExecutor(runtime, watchdog=Watchdog(interval_s=60.0), start_watchdog=False)
    try:
        action = await executor.submit(LookAtAction(), ROBOT_ID)
        await settle(runtime)
        assert action.status is ActionStatus.SUCCEEDED
    finally:
        await executor.aclose()


async def test_closing_does_not_pretend_to_stop_the_robots(runtime) -> None:
    """Teardown runs when the process is going away; a stop from a dying process is the
    hope this design refuses to rely on (docs/safety-model.md)."""
    executor = RobotActionExecutor(runtime, watchdog=Watchdog(interval_s=60.0), start_watchdog=False)
    await executor.start()
    await executor.submit(MoveAction(distance_mm=800), ROBOT_ID)
    await settle(runtime)
    runtime.calls.clear()
    await executor.aclose()
    assert runtime.called(STOP_TOOL) == ()
