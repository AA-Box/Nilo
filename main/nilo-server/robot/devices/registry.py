"""The robot registry: which robots are connected, and what the server knows about them.

This is the subsystem's first primitive, because the inherited server has none: its
connection handler is a local variable in the WebSocket server and nothing maps a device
id to a live session (robot-architecture R1).

Every method is a coroutine guarded by one lock, so concurrent connects, disconnects and
telemetry updates cannot interleave into a half-written state. State itself lives in a
:class:`~robot.state.store.RobotStateStore`, so a future Redis-backed deployment changes
the store and not this class.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from robot.devices.tools import RobotCapabilityRegistry, RobotToolRegistry
from robot.events.bus import EventBus
from robot.events.types import (
    BatteryUpdated,
    RobotEvent,
    CapabilitiesRefreshed,
    PoseUpdated,
    RobotConnected,
    RobotDisconnected,
    SensorUpdated,
    TelemetryUpdated,
    ToolDiscovered,
)
from robot.state.models import (
    ConnectionStatus,
    DisconnectReason,
    RobotCapabilities,
    RobotConnection,
    RobotIdentity,
    RobotState,
    RobotTelemetry,
    utcnow,
)
from robot.state.store import InMemoryRobotStateStore, RobotStateStore

logger = logging.getLogger(__name__)


class RobotRegistry:
    """Live robots, their state, and the events that announce changes to both."""

    def __init__(
        self,
        store: RobotStateStore | None = None,
        events: EventBus | None = None,
        capabilities: RobotCapabilityRegistry | None = None,
    ) -> None:
        self._store = store or InMemoryRobotStateStore()
        self._events = events
        self._capabilities = capabilities or RobotCapabilityRegistry()
        # ponytail: one registry-wide lock. Shard by robot id if a fleet ever contends.
        self._lock = asyncio.Lock()

    @property
    def store(self) -> RobotStateStore:
        return self._store

    @property
    def capabilities(self) -> RobotCapabilityRegistry:
        return self._capabilities

    async def register(self, identity: RobotIdentity, connection: RobotConnection) -> RobotState:
        """Register a session for a robot, or take over the robot from an older session.

        A device that reconnects keeps its robot id, its last known telemetry (stale, and
        timestamped as such) and its previously discovered capabilities until discovery
        refreshes them. An older live session for the same robot is superseded, which is
        an observable :class:`RobotDisconnected`, not a silent drop.
        """
        robot_id = identity.robot_id
        async with self._lock:
            previous = await self._store.get(robot_id)
            reconnect = previous is not None
            superseded = (
                previous is not None
                and previous.connection.is_connected
                and previous.connection.session_id != connection.session_id
            )
            reconnect_count = 0
            if previous is not None:
                same_session = previous.connection.session_id == connection.session_id
                reconnect_count = previous.connection.reconnect_count + (0 if same_session else 1)
            live_connection = connection.model_copy(
                update={
                    "robot_id": robot_id,
                    "status": ConnectionStatus.CONNECTED,
                    "reconnect_count": reconnect_count,
                    "disconnected_at": None,
                    "disconnect_reason": None,
                }
            )
            live_identity = identity
            if previous is not None and not identity.capabilities.tools and previous.identity.capabilities.tools:
                # Keep the known capabilities until discovery refreshes them, so a
                # reconnecting robot is never briefly capability-less.
                live_identity = identity.model_copy(update={"capabilities": previous.identity.capabilities})
            state = RobotState(
                robot_id=robot_id,
                identity=live_identity,
                connection=live_connection,
                telemetry=previous.telemetry if previous is not None else RobotTelemetry(),
                updated_at=utcnow(),
            )
            await self._store.put(state)
        if superseded and previous is not None:
            await self._publish(
                RobotDisconnected(
                    robot_id=robot_id,
                    session_id=previous.connection.session_id,
                    reason=DisconnectReason.SUPERSEDED,
                )
            )
        await self._publish(
            RobotConnected(
                robot_id=robot_id,
                identity=state.identity,
                connection=state.connection,
                reconnect=reconnect,
            )
        )
        logger.info(
            "robot %s registered (device=%s session=%s reconnect=%s)",
            robot_id,
            identity.device_id,
            live_connection.session_id,
            reconnect,
        )
        return state

    async def unregister(
        self,
        robot_id: str,
        *,
        session_id: str | None = None,
        reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED,
    ) -> bool:
        """Mark a robot disconnected and release its live capabilities.

        Idempotent, and a no-op when ``session_id`` names a session that has already been
        replaced — a late teardown from an old session must not unregister the new one.
        Double teardown is the normal flow in the inherited connection handler.
        """
        async with self._lock:
            state = await self._store.get(robot_id)
            if state is None:
                return False
            if session_id is not None and state.connection.session_id != session_id:
                logger.debug(
                    "robot %s: ignoring teardown from stale session %s", robot_id, session_id
                )
                return False
            if not state.connection.is_connected:
                return False
            closed = state.connection.model_copy(
                update={
                    "status": ConnectionStatus.DISCONNECTED,
                    "disconnected_at": utcnow(),
                    "disconnect_reason": reason,
                }
            )
            await self._store.put(state.model_copy(update={"connection": closed, "updated_at": utcnow()}))
            await self._capabilities.drop(robot_id)
        await self._publish(
            RobotDisconnected(robot_id=robot_id, session_id=state.connection.session_id, reason=reason)
        )
        logger.info("robot %s disconnected (%s)", robot_id, reason.value)
        return True

    async def get(self, robot_id: str) -> RobotState | None:
        return await self._store.get(robot_id)

    async def list(self, *, connected_only: bool = True) -> list[RobotState]:
        states = await self._store.list_states()
        if connected_only:
            states = [state for state in states if state.connection.is_connected]
        return sorted(states, key=lambda state: state.robot_id)

    async def update_state(self, robot_id: str, telemetry: RobotTelemetry) -> RobotState | None:
        """Merge a telemetry patch into the robot's world state.

        Only the fields set on ``telemetry`` are applied; the rest keep their previous
        value and their previous age. Returns ``None`` for an unknown robot.
        """
        now = utcnow()
        seen: list[RobotState] = []

        def apply(state: RobotState) -> RobotState:
            seen.append(state)
            merged = state.telemetry.merge(telemetry)
            return state.model_copy(
                update={
                    "telemetry": merged,
                    "connection": state.connection.model_copy(update={"last_seen_at": now, "updated_at": now}),
                    "identity": state.identity.model_copy(update={"last_seen_at": now}),
                    "updated_at": now,
                }
            )

        updated = await self._store.mutate(robot_id, apply)
        if updated is None:
            return None
        changed = updated.telemetry.changed_fields(seen[-1].telemetry)
        if not changed:
            return updated
        await self._publish(
            TelemetryUpdated(robot_id=robot_id, telemetry=updated.telemetry, changed=changed)
        )
        if "pose" in changed and updated.telemetry.pose is not None:
            await self._publish(PoseUpdated(robot_id=robot_id, pose=updated.telemetry.pose))
        if "battery" in changed and updated.telemetry.battery is not None:
            await self._publish(BatteryUpdated(robot_id=robot_id, battery=updated.telemetry.battery))
        if "sensors" in changed and updated.telemetry.sensors is not None:
            await self._publish(SensorUpdated(robot_id=robot_id, sensors=updated.telemetry.sensors))
        return updated

    async def update_last_seen(self, robot_id: str, when: datetime | None = None) -> RobotState | None:
        """Refresh liveness without touching telemetry. Returns ``None`` if unknown."""
        moment = when or utcnow()

        def apply(state: RobotState) -> RobotState:
            return state.model_copy(
                update={
                    "connection": state.connection.model_copy(
                        update={"last_seen_at": moment, "updated_at": moment}
                    ),
                    "identity": state.identity.model_copy(update={"last_seen_at": moment}),
                    "updated_at": moment,
                }
            )

        return await self._store.mutate(robot_id, apply)

    async def get_capabilities(self, robot_id: str) -> RobotCapabilities | None:
        """Live capabilities of a connected robot, or ``None`` before discovery."""
        return await self._capabilities.get(robot_id)

    async def get_tools(self, robot_id: str) -> RobotToolRegistry | None:
        return await self._capabilities.tools(robot_id)

    async def set_capabilities(self, robot_id: str, capabilities: RobotCapabilities) -> RobotState | None:
        """Record a discovery result against a registered robot and announce it.

        Writes through to the robot's identity as well, so a state snapshot taken later
        carries the capabilities that were live at the time.
        """
        now = utcnow()
        seen: list[RobotState] = []

        def apply(current: RobotState) -> RobotState:
            seen.append(current)
            identity_update: dict[str, object] = {"capabilities": capabilities}
            # Identity is discovered, not configured: fill in what the device reported
            # about itself during the handshake, without overwriting anything known.
            if capabilities.server_name and current.identity.hardware_model == "unknown":
                identity_update["hardware_model"] = capabilities.server_name
            if capabilities.server_version and current.identity.firmware_version == "unknown":
                identity_update["firmware_version"] = capabilities.server_version
            if capabilities.protocol_version and current.identity.protocol_version == "unknown":
                identity_update["protocol_version"] = capabilities.protocol_version
            return current.model_copy(
                update={
                    "identity": current.identity.model_copy(update=identity_update),
                    "updated_at": now,
                }
            )

        updated = await self._store.mutate(robot_id, apply)
        if updated is None:
            logger.warning("robot %s: discovery finished after the robot was gone", robot_id)
            return None
        refreshed = bool(seen[-1].identity.capabilities.tools)
        added = await self._capabilities.set(robot_id, capabilities)
        for tool in added:
            await self._publish(ToolDiscovered(robot_id=robot_id, tool=tool))
        await self._publish(
            CapabilitiesRefreshed(robot_id=robot_id, capabilities=capabilities, refreshed=refreshed)
        )
        logger.info(
            "robot %s: %d tool(s) discovered (%d new, %d malformed)",
            robot_id,
            len(capabilities.tools),
            len(added),
            capabilities.malformed_tools,
        )
        return updated

    async def _publish(self, event: RobotEvent) -> None:
        if self._events is not None:
            await self._events.publish(event)
