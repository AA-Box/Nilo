"""The writer in front of :class:`~robot.state.world.WorldState`.

One :class:`WorldModel` per runtime holds one immutable snapshot per robot and is the only
thing that replaces them. Everything else reads.

    model = WorldModel()
    model.attach(runtime.events)          # events now fold into the snapshots
    world = await model.state("nilo-sim-01")

Why events rather than direct writes: perception, telemetry and the action layer all have
something to say about the world, and if each wrote into the map directly there would be
three orderings to reason about after an incident. There is one, and it is the bus.

The model deliberately does *not* publish an event of its own for every change. A world
update is not news — the events that caused it already were — and a snapshot-changed event
would make the bus loop through itself once per telemetry frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from robot.events.bus import EventBus, Subscription
from robot.events.types import (
    BatteryUpdated,
    PoseUpdated,
    RobotConnected,
    RobotDisconnected,
    RobotEvent,
    SensorUpdated,
    TelemetryUpdated,
)
from robot.state.models import RobotTelemetry, utcnow
from robot.state.world import (
    Entity,
    EntityType,
    Environment,
    Interaction,
    WorldState,
)

logger = logging.getLogger(__name__)

#: How often :meth:`WorldModel.run_decay` ages entities when it owns the timer.
DEFAULT_DECAY_INTERVAL_S = 1.0


class WorldModel:
    """Per-robot world snapshots, written by events and read by everything else."""

    def __init__(
        self,
        *,
        ttl_s: Mapping[EntityType, float] | None = None,
        half_life_s: Mapping[EntityType, float] | None = None,
    ) -> None:
        self._worlds: dict[str, WorldState] = {}
        self._lock = asyncio.Lock()
        self._subscriptions: list[Subscription] = []
        self._bus: EventBus | None = None
        self._decay_task: asyncio.Task[None] | None = None
        self._ttl_s = dict(ttl_s or {})
        self._half_life_s = dict(half_life_s or {})

    # -- reading ---------------------------------------------------------------------------

    async def state(self, robot_id: str) -> WorldState:
        """The snapshot for one robot, created empty on first ask."""
        async with self._lock:
            return self._ensure(robot_id)

    def snapshot(self, robot_id: str) -> WorldState:
        """The snapshot without awaiting a lock. Safe: snapshots are immutable.

        For the synchronous paths — a scoring pass, a CLI, a log line — which must not be
        able to interleave an await between reading the world and using it.
        """
        return self._worlds.get(robot_id) or WorldState(robot_id=robot_id)

    def robot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._worlds))

    # -- writing ---------------------------------------------------------------------------

    async def update(self, robot_id: str, change: Callable[[WorldState], WorldState]) -> WorldState:
        """Apply a pure function to one robot's snapshot, atomically."""
        async with self._lock:
            updated = change(self._ensure(robot_id))
            self._worlds[robot_id] = updated
            return updated

    async def observe(self, robot_id: str, entity: Entity, *, attend: bool = False) -> WorldState:
        """Record a sighting, optionally making it the attention target."""

        def change(world: WorldState) -> WorldState:
            updated = world.observe(entity)
            return updated.attend_to(entity.id) if attend else updated

        return await self.update(robot_id, change)

    async def forget(self, robot_id: str, entity_id: str) -> WorldState:
        return await self.update(robot_id, lambda world: world.forget(entity_id))

    async def attend_to(self, robot_id: str, entity_id: str | None) -> WorldState:
        return await self.update(robot_id, lambda world: world.attend_to(entity_id))

    async def set_environment(self, robot_id: str, environment: Environment) -> WorldState:
        return await self.update(robot_id, lambda world: world.with_environment(environment))

    async def start_interaction(self, robot_id: str, interaction: Interaction) -> WorldState:
        return await self.update(robot_id, lambda world: world.start_interaction(interaction))

    async def end_interaction(self, robot_id: str, summary: str = "") -> WorldState:
        return await self.update(robot_id, lambda world: world.end_interaction(summary=summary))

    async def decay(self, now: datetime | None = None) -> None:
        """Age every world once. Idempotent, and cheap when nothing has changed."""
        moment = now or utcnow()
        async with self._lock:
            for robot_id, world in list(self._worlds.items()):
                self._worlds[robot_id] = world.decay(
                    moment, ttl_s=self._ttl_s, half_life_s=self._half_life_s
                )

    async def drop(self, robot_id: str) -> bool:
        """Forget a robot's world entirely. Called when a robot is unregistered for good."""
        async with self._lock:
            return self._worlds.pop(robot_id, None) is not None

    # -- the event seam ----------------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        """Subscribe to the events that write the world. Idempotent per bus."""
        if self._bus is bus and self._subscriptions:
            return
        self.detach()
        self._bus = bus
        self._subscriptions.append(
            bus.subscribe(
                (RobotConnected, RobotDisconnected, TelemetryUpdated, BatteryUpdated, PoseUpdated, SensorUpdated),
                self._on_event,
            )
        )

    def detach(self) -> None:
        bus, self._bus = self._bus, None
        for subscription in self._subscriptions:
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.unsubscribe(subscription)
        self._subscriptions.clear()

    def start_decay(self, interval_s: float = DEFAULT_DECAY_INTERVAL_S) -> None:
        """Run :meth:`decay` on a timer. Optional: a caller with its own tick calls decay itself."""
        if self._decay_task is not None and not self._decay_task.done():
            return
        self._decay_task = asyncio.create_task(self._run_decay(interval_s), name="robot-world-decay")

    async def aclose(self) -> None:
        self.detach()
        task, self._decay_task = self._decay_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- internals -----------------------------------------------------------------------------

    def _ensure(self, robot_id: str) -> WorldState:
        world = self._worlds.get(robot_id)
        if world is None:
            world = WorldState(robot_id=robot_id)
            self._worlds[robot_id] = world
        return world

    async def _on_event(self, event: RobotEvent) -> None:
        try:
            await self._fold(event)
        except Exception:  # a bad event must not take the world model down with it
            logger.exception("world model failed to fold %s", event.name)

    async def _fold(self, event: RobotEvent) -> None:
        if isinstance(event, RobotConnected):
            await self.observe(
                event.robot_id,
                Entity(
                    id=event.robot_id,
                    type=EntityType.ROBOT,
                    attributes={
                        "name": event.identity.name,
                        "hardware_model": event.identity.hardware_model,
                        "self": True,
                    },
                    first_seen=event.identity.connected_at,
                    last_seen=event.occurred_at,
                    updated_at=event.occurred_at,
                ),
            )
            return
        if isinstance(event, RobotDisconnected):
            await self.update(event.robot_id, lambda world: world.end_interaction(summary="disconnected"))
            return
        patch = _telemetry_patch(event)
        if patch is not None:
            await self.update(event.robot_id, lambda world: world.with_telemetry(patch))

    async def _run_decay(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.decay()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive, the loop must survive
                logger.exception("world decay pass failed")

    def __repr__(self) -> str:
        sizes = {robot_id: len(world.entities) for robot_id, world in self._worlds.items()}
        return f"<WorldModel {sizes}>"


def _telemetry_patch(event: Any) -> RobotTelemetry | None:
    """The telemetry an event carries, as a patch to merge, or ``None``."""
    if isinstance(event, TelemetryUpdated):
        return event.telemetry
    if isinstance(event, BatteryUpdated):
        return RobotTelemetry(battery=event.battery)
    if isinstance(event, PoseUpdated):
        return RobotTelemetry(pose=event.pose)
    if isinstance(event, SensorUpdated):
        return RobotTelemetry(sensors=event.sensors)
    return None


__all__ = ["DEFAULT_DECAY_INTERVAL_S", "WorldModel"]
