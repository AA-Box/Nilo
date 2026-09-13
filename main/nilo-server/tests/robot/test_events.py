"""The event bus: fan-out, isolation, bounded queues and shutdown."""
from __future__ import annotations

import asyncio

import pytest

from robot.events import (
    BatteryUpdated,
    EventBus,
    PoseUpdated,
    RobotEvent,
    TelemetryUpdated,
)
from robot.state import RobotBatteryState, RobotPose, RobotTelemetry


def pose_event(x: float = 0.0) -> PoseUpdated:
    return PoseUpdated(robot_id="r1", pose=RobotPose(x_m=x))


async def test_publish_delivers_to_a_subscriber_of_that_type():
    bus = EventBus()
    seen: list[RobotEvent] = []
    bus.subscribe(PoseUpdated, seen.append)
    await bus.publish(pose_event(1.0))
    await bus.drain()
    assert [event.pose.x_m for event in seen] == [1.0]
    await bus.aclose()


async def test_subscribers_only_see_their_own_event_types():
    bus = EventBus()
    poses: list[RobotEvent] = []
    batteries: list[RobotEvent] = []
    everything: list[RobotEvent] = []
    bus.subscribe(PoseUpdated, poses.append)
    bus.subscribe(BatteryUpdated, batteries.append)
    bus.subscribe(RobotEvent, everything.append)
    await bus.publish(pose_event())
    await bus.publish(BatteryUpdated(robot_id="r1", battery=RobotBatteryState(percent=50)))
    await bus.drain()
    assert len(poses) == 1
    assert len(batteries) == 1
    assert len(everything) == 2
    await bus.aclose()


async def test_a_tuple_of_types_subscribes_to_all_of_them():
    bus = EventBus()
    seen: list[RobotEvent] = []
    bus.subscribe((PoseUpdated, BatteryUpdated), seen.append)
    await bus.publish(pose_event())
    await bus.publish(TelemetryUpdated(robot_id="r1", telemetry=RobotTelemetry()))
    await bus.publish(BatteryUpdated(robot_id="r1", battery=RobotBatteryState(percent=9)))
    await bus.drain()
    assert [type(event).__name__ for event in seen] == ["PoseUpdated", "BatteryUpdated"]
    await bus.aclose()


async def test_multiple_subscribers_of_the_same_type_all_receive_the_event():
    bus = EventBus()
    first: list[RobotEvent] = []
    second: list[RobotEvent] = []
    bus.subscribe(PoseUpdated, first.append)
    bus.subscribe(PoseUpdated, second.append)
    await bus.publish(pose_event())
    await bus.drain()
    assert len(first) == len(second) == 1
    await bus.aclose()


async def test_async_handlers_are_awaited():
    bus = EventBus()
    seen: list[RobotEvent] = []

    async def handler(event: PoseUpdated) -> None:
        await asyncio.sleep(0)
        seen.append(event)

    bus.subscribe(PoseUpdated, handler)
    await bus.publish(pose_event())
    await bus.drain()
    assert len(seen) == 1
    await bus.aclose()


async def test_unsubscribe_stops_delivery_and_is_idempotent():
    bus = EventBus()
    seen: list[RobotEvent] = []
    subscription = bus.subscribe(PoseUpdated, seen.append)
    await bus.publish(pose_event())
    await bus.drain()
    assert bus.unsubscribe(subscription)
    assert not bus.unsubscribe(subscription)
    await bus.publish(pose_event())
    await asyncio.sleep(0)
    assert len(seen) == 1
    await bus.aclose()


async def test_a_failing_subscriber_does_not_stop_the_publisher_or_its_peers():
    bus = EventBus()
    survived: list[RobotEvent] = []

    def explode(event: PoseUpdated) -> None:
        raise RuntimeError("subscriber is broken")

    failing = bus.subscribe(PoseUpdated, explode)
    bus.subscribe(PoseUpdated, survived.append)
    for index in range(3):
        await bus.publish(pose_event(float(index)))
    await bus.drain()
    assert [event.pose.x_m for event in survived] == [0.0, 1.0, 2.0]
    assert failing.failed == 3
    assert failing.delivered == 0
    await bus.aclose()


async def test_a_full_queue_drops_the_oldest_event_and_counts_it():
    """A producer faster than its consumer loses the *oldest* events, not the newest."""
    bus = EventBus(queue_size=2)
    release = asyncio.Event()
    seen: list[float] = []

    async def slow(event: PoseUpdated) -> None:
        seen.append(event.pose.x_m)
        await release.wait()

    subscription = bus.subscribe(PoseUpdated, slow)
    for index in range(6):
        await bus.publish(pose_event(float(index)))
    assert subscription.dropped == 6 - 2
    assert bus.dropped == subscription.dropped
    release.set()
    await bus.drain()
    assert seen == [4.0, 5.0]  # the freshest robot state is the one kept
    await bus.aclose()


async def test_a_slow_subscriber_cannot_block_a_fast_one():
    bus = EventBus(queue_size=8)
    release = asyncio.Event()
    fast: list[RobotEvent] = []

    async def slow(event: PoseUpdated) -> None:
        await release.wait()

    bus.subscribe(PoseUpdated, slow)
    fast_subscription = bus.subscribe(PoseUpdated, fast.append)
    for index in range(5):
        await bus.publish(pose_event(float(index)))
    await fast_subscription.queue.join()
    assert len(fast) == 5
    release.set()
    await bus.drain()
    await bus.aclose()


async def test_publishing_never_waits_for_a_handler():
    bus = EventBus(queue_size=1)
    release = asyncio.Event()

    async def slow(event: PoseUpdated) -> None:
        await release.wait()

    bus.subscribe(PoseUpdated, slow)
    await asyncio.wait_for(asyncio.gather(*(bus.publish(pose_event()) for _ in range(50))), timeout=1.0)
    release.set()
    await bus.drain()
    await bus.aclose()


async def test_close_cancels_workers_and_refuses_further_use():
    bus = EventBus()
    subscription = bus.subscribe(PoseUpdated, lambda event: None)
    await bus.aclose()
    await bus.aclose()  # idempotent
    assert bus.closed
    assert subscription._worker is not None and subscription._worker.done()
    with pytest.raises(RuntimeError):
        await bus.publish(pose_event())
    with pytest.raises(RuntimeError):
        bus.subscribe(PoseUpdated, lambda event: None)


def test_queue_size_must_be_positive():
    with pytest.raises(ValueError):
        EventBus(queue_size=0)
