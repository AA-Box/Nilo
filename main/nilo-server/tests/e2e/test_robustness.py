"""The things that go wrong in a process that runs for months, exercised against a server.

Every test here is one of the failure modes Phase 12 asks for by name: connect/disconnect
churn, leaked tasks, unclean shutdown, malformed frames, duplicate responses, timeouts,
stale telemetry and cancellation races. They are separated from the scenarios because a
scenario says "the product works" and these say "it keeps working" — and because these are
the ones worth running in a loop when something smells.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from robot.actions import ActionStatus, RejectionReason
from robot.events.types import MotionCompleted
from robot.state.actions import ActionSource
from tests.e2e.conftest import NORMALIZED, E2EBackend, make_robot, running

pytestmark = pytest.mark.e2e

#: How many connect/disconnect cycles the churn test runs. Enough that a leak of one task
#: or one subscription per session is a number nobody can argue with.
CHURN_CYCLES = 8


def robot_tasks() -> list[asyncio.Task[Any]]:
    """Every live task this subsystem named. The naming convention is what makes it possible."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and (task.get_name().startswith(("robot-", "e2e-")) or "simulator" in task.get_name())
    ]


# -- churn ------------------------------------------------------------------------------------


async def test_repeated_connect_disconnect_leaks_nothing(backend: E2EBackend) -> None:
    """Eight sessions in a row: no growing registry, no growing task set, no growing bus."""
    before_tasks = len(robot_tasks())
    before_subscriptions = len(backend.runtime.events.subscriptions)

    for _ in range(CHURN_CYCLES):
        async with running(make_robot(backend)) as simulator:
            await simulator.wait_discovered(timeout=20)
            await backend.wait_for(lambda: _connected(backend))
        await backend.wait_for(lambda: _gone(backend), timeout=20)

    states = await backend.runtime.registry.list()
    assert len(states) <= 1, "a session left a second registry entry behind"
    assert await backend.runtime.registry.list(connected_only=True) == []
    assert backend.runtime.channel(NORMALIZED) is None, "a tool channel outlived its session"

    # One session's worth of slack, not eight: the point is that it does not grow with the
    # number of sessions, and a task settling a moment after the socket closed is normal.
    assert len(robot_tasks()) <= before_tasks + 2, [task.get_name() for task in robot_tasks()]
    assert len(backend.runtime.events.subscriptions) <= before_subscriptions + 2
    assert backend.runtime.events.dropped == 0, "the event bus dropped events under churn"


async def test_reconnect_while_a_previous_session_is_still_closing(backend: E2EBackend) -> None:
    """Two sessions for one robot at once: the newer one owns the registry entry.

    The race a reconnecting device creates every time: the new socket is accepted before
    the old one's teardown has run, so a detach that does not check the session id would
    unregister the robot that just arrived.
    """
    first = make_robot(backend, reconnect=False)
    async with running(first) as old:
        await old.wait_discovered(timeout=20)
        original = await backend.wait_for(lambda: _connected(backend))

        second = make_robot(backend, reconnect=False)
        async with running(second) as new:
            await new.wait_discovered(timeout=20)
            current = await backend.wait_for(lambda: _session_changed(backend, original.connection.session_id))
            assert current.is_connected
            # The old session goes away *now*, after the new one registered.
            await old.drop_connection(reconnect=False)
            await asyncio.sleep(0.3)
            still_here = await backend.state()
            assert still_here is not None and still_here.is_connected
            assert still_here.connection.session_id == current.connection.session_id


# -- malformed and duplicate traffic -----------------------------------------------------------


async def test_malformed_frames_do_not_break_the_session(backend: E2EBackend, robot: Any) -> None:
    """Junk in, session still up: every one of these is a frame a broken device can send."""
    for payload in (
        "not json at all",
        "[]",
        '{"type": "mcp"}',
        '{"type": "mcp", "payload": null}',
        '{"type": "mcp", "payload": {"jsonrpc": "2.0"}}',
        '{"type": "mcp", "payload": {"jsonrpc": "2.0", "method": "notifications/telemetry"}}',
        '{"type": "mcp", "payload": {"jsonrpc": "2.0", "method": "notifications/telemetry", "params": 7}}',
        '{"type": "mcp", "payload": {"jsonrpc": "2.0", "method": "notifications/motion_completed"}}',
        '{"type": "listen", "state": "detect"}',
        '{"type": "unheard_of"}',
        json.dumps({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": 9_999, "result": {"nonsense": True}}}),
    ):
        await robot.send_raw(payload)

    await asyncio.sleep(0.5)
    state = await backend.state()
    assert state is not None and state.is_connected, "a malformed frame closed the session"

    # And the link still works for something real.
    status = json.loads(await backend.runtime.call_tool(NORMALIZED, "robot_get_status", {}))
    assert status["battery"]["percent"] > 0


async def test_a_duplicate_completion_settles_nothing_twice(backend: E2EBackend, robot: Any) -> None:
    """The same motion completion, sent twice: the second one is dropped, not re-applied."""
    record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
        distance_mm=200, speed_mmps=100, wait=False
    )
    settled = await backend.wait_for(lambda: _terminal(backend, record.action_id), timeout=25)
    assert settled.status is ActionStatus.SUCCEEDED
    device_action_id = settled.device_action_id
    assert device_action_id

    # Replay the notification the device already sent. Nothing may change.
    replays = 0
    async for _ in _replay(robot, device_action_id, times=3):
        replays += 1
    await asyncio.sleep(0.3)
    again = backend.runtime.actions.query(record.action_id)
    assert replays == 3
    assert again is not None
    assert again.status is ActionStatus.SUCCEEDED
    assert again.finished_at == settled.finished_at, "a duplicate completion re-settled the action"


async def _replay(robot: Any, device_action_id: str, *, times: int) -> Any:
    for _ in range(times):
        await robot._notify(
            "notifications/motion_completed", {"action_id": device_action_id, "kind": "move"}
        )
        yield True


async def test_a_completion_for_an_unknown_action_is_dropped(backend: E2EBackend, robot: Any) -> None:
    """Firmware that replays an old notification after a reconnect must settle nothing."""
    live = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
        distance_mm=600, speed_mmps=60, wait=False
    )
    await backend.wait_for(lambda: robot.state.moving, timeout=20)

    received: list[MotionCompleted] = []
    backend.runtime.events.subscribe(MotionCompleted, received.append)
    await robot._notify("notifications/motion_completed", {"action_id": "never-existed", "kind": "move"})
    await backend.wait_for(lambda: received, timeout=10)
    await asyncio.sleep(0.2)

    # The event reached the bus, and settled nothing: the action that *is* running is
    # still running, because a completion is matched on the device's id and not on
    # "whatever this robot happens to be doing".
    still_running = backend.runtime.actions.query(live.action_id)
    assert still_running is not None and not still_running.is_terminal, still_running
    state = await backend.state()
    assert state is not None and state.is_connected
    await backend.runtime.actions.cancel(live.action_id, reason="test over")


# -- timeouts and staleness ------------------------------------------------------------------------


async def test_a_silent_device_times_the_action_out_rather_than_hanging(backend: E2EBackend) -> None:
    """A device that accepts a move and never reports completion: the watchdog settles it."""
    from robot.simulator import Scenario

    plan = Scenario(name="no-completion", duration_s=0.0)
    plan.faults.drop_motion_completion = True
    async with running(make_robot(backend, scenario=plan)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: _ready(backend))
        record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
            distance_mm=200, speed_mmps=200, wait=False
        )
        settled = await backend.wait_for(lambda: _terminal(backend, record.action_id), timeout=40)
        assert settled.status is ActionStatus.TIMED_OUT
        assert settled.finished_at is not None


async def test_stale_telemetry_stops_being_acted_on(backend: E2EBackend, robot: Any) -> None:
    """Telemetry that stops arriving makes the policy refuse, and it says which check failed."""
    limits = backend.runtime.actions.limits
    # Freeze the device: no more notifications, so the sensor picture ages out.
    robot.faults.drop_notifications = True
    await asyncio.sleep(limits.max_sensor_age_s + 0.5)

    record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.LLM).move(distance_mm=200)
    assert record.status is ActionStatus.REJECTED
    assert record.rejection in (
        RejectionReason.SENSOR_DATA_STALE,
        RejectionReason.HEARTBEAT_EXPIRED,
    ), record.error


# -- cancellation --------------------------------------------------------------------------------


async def test_cancel_racing_a_completion_settles_once(backend: E2EBackend, robot: Any) -> None:
    """Cancel and complete arriving together: exactly one terminal transition happens."""
    for _ in range(5):
        record = await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
            distance_mm=150, speed_mmps=300, wait=False
        )
        # No sleep: the cancel goes out while dispatch is still in flight, which is the race.
        cancelled, settled = await asyncio.gather(
            backend.runtime.actions.cancel(record.action_id, reason="racing the completion"),
            _await_terminal(backend, record.action_id),
            return_exceptions=True,
        )
        assert not isinstance(settled, BaseException), settled
        assert settled.status.is_terminal
        assert settled.finished_at is not None
        # Whichever won, the action is terminal exactly once: a second cancel changes nothing.
        again = await backend.runtime.actions.cancel(record.action_id)
        assert again is None


async def test_closing_the_runtime_leaves_nothing_running(backend: E2EBackend) -> None:
    """The shutdown path: one call, and every task, thread and subscription is gone."""
    async with running(make_robot(backend)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: _ready(backend))
        # Wake every lazily-built subsystem, so closing has something to close.
        backend.runtime.behavior(NORMALIZED).start(interval_s=0.05)
        backend.runtime.vision(NORMALIZED)
        backend.runtime.animations(NORMALIZED)
        await backend.runtime.agent(NORMALIZED)
        await backend.runtime.robot(NORMALIZED).as_source(ActionSource.USER).move(
            distance_mm=150, wait=False
        )
        watchdog = backend.runtime.actions.watchdog

        await backend.runtime.aclose()

        assert backend.runtime.closed
        assert not watchdog.running, "the action watchdog thread outlived the runtime"
        assert backend.runtime.events.subscriptions == ()
        await asyncio.sleep(0.2)
        leaked = [task.get_name() for task in robot_tasks() if "simulator" not in task.get_name()]
        assert leaked in ([], ["e2e-ws-server"]), leaked


# -- helpers ---------------------------------------------------------------------------------------


async def _await_terminal(backend: E2EBackend, action_id: str) -> Any:
    return await backend.wait_for(lambda: _terminal(backend, action_id), timeout=25)


async def _terminal(backend: E2EBackend, action_id: str) -> Any:
    record = backend.runtime.actions.query(action_id)
    return record if record is not None and record.is_terminal else None


async def _connected(backend: E2EBackend) -> Any:
    state = await backend.state()
    return state if state is not None and state.is_connected else None


async def _gone(backend: E2EBackend) -> Any:
    states = await backend.runtime.registry.list(connected_only=True)
    return True if not states else None


async def _session_changed(backend: E2EBackend, previous: str) -> Any:
    state = await backend.state()
    if state is None or not state.is_connected:
        return None
    return state if state.connection.session_id != previous else None


async def _ready(backend: E2EBackend) -> Any:
    state = await backend.state()
    if state is None or state.telemetry.sensors is None:
        return None
    return state if state.capabilities.has_tool("robot_motion_move") else None
