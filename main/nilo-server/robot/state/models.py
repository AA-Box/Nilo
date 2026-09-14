"""Frozen domain models for one connected robot.

Every model here is immutable and has no dependency beyond pydantic, so it can be
serialized into a future Redis/Postgres state store, published on the event bus and
asserted on in tests without a server, a config file or an event loop.

The world model is a *cache*: the robot is authoritative (docs/robot-architecture.md
Sect. 2.4). Every timestamped model therefore exposes its age, and a reader that cannot
act on stale data calls :meth:`Timestamped.require_fresh` instead of reading the field.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NON_ID = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    """Timezone-aware now. All robot timestamps are UTC."""
    return datetime.now(UTC)


def normalize_robot_id(device_id: str) -> str:
    """Stable logical id for a device id (a MAC address, usually).

    The device id is what the firmware sends; the robot id is what the rest of the
    subsystem keys on, so a device whose id differs only in case or separator is one
    robot and not two.
    """
    robot_id = _NON_ID.sub("-", device_id.strip().lower()).strip("-")
    if not robot_id:
        raise ValueError(f"device id has no usable characters: {device_id!r}")
    return robot_id


class StaleStateError(RuntimeError):
    """A state entry was read past its freshness budget."""

    def __init__(self, what: str, age_s: float, max_age_s: float) -> None:
        super().__init__(f"{what} is {age_s:.3f}s old, budget is {max_age_s:.3f}s")
        self.what = what
        self.age_s = age_s
        self.max_age_s = max_age_s


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Timestamped(_Frozen):
    """A value that knows when it was last written."""

    updated_at: datetime = Field(default_factory=utcnow)

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.updated_at).total_seconds()

    def is_fresh(self, max_age_s: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) <= max_age_s

    def require_fresh(self, max_age_s: float, now: datetime | None = None) -> Self:
        """Return self, or raise :class:`StaleStateError` if it is too old to act on."""
        age = self.age_seconds(now)
        if age > max_age_s:
            raise StaleStateError(type(self).__name__, age, max_age_s)
        return self


class ConnectionStatus(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class DisconnectReason(str, Enum):
    CLIENT_CLOSED = "client_closed"
    SUPERSEDED = "superseded"
    TIMEOUT = "timeout"
    ERROR = "error"
    SERVER_SHUTDOWN = "server_shutdown"


class RobotActivity(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MOVING = "moving"
    CHARGING = "charging"
    ERROR = "error"


class RobotTool(_Frozen):
    """One capability the device advertised over the MCP tool channel.

    ``name`` is the sanitized name the rest of the server uses; ``raw_name`` is what the
    device published and what a ``tools/call`` must carry.
    """

    name: str
    raw_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    source: str = "device_mcp"

    @field_validator("name", "raw_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool names must not be empty")
        return value

    @property
    def required_arguments(self) -> tuple[str, ...]:
        required = self.input_schema.get("required", [])
        if not isinstance(required, list):
            return ()
        return tuple(item for item in required if isinstance(item, str))


class RobotCapabilities(Timestamped):
    """What this robot can do, as discovered — never hardcoded.

    Empty capabilities are a legitimate state: a device that does not speak MCP, or one
    whose discovery has not finished yet.
    """

    mcp: bool = False
    features: frozenset[str] = frozenset()
    tools: tuple[RobotTool, ...] = ()
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    malformed_tools: int = 0

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def has_tool(self, name: str) -> bool:
        return any(tool.name == name for tool in self.tools)

    def get_tool(self, name: str) -> RobotTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class RobotIdentity(_Frozen):
    """Who the robot is. ``device_id`` comes from the transport, the rest from ``hello``."""

    device_id: str
    robot_id: str
    name: str = "robot"
    hardware_model: str = "unknown"
    firmware_version: str = "unknown"
    protocol_version: str = "unknown"
    capabilities: RobotCapabilities = Field(default_factory=RobotCapabilities)
    connected_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)

    @field_validator("device_id")
    @classmethod
    def _device_id_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("device_id must not be empty")
        return value


class RobotConnection(Timestamped):
    """The live transport binding for a robot. One per session, replaced on reconnect."""

    robot_id: str
    device_id: str
    session_id: str
    status: ConnectionStatus = ConnectionStatus.CONNECTED
    protocol: str = "nilo"
    transport: str = "websocket"
    remote_address: str | None = None
    connected_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    disconnected_at: datetime | None = None
    disconnect_reason: DisconnectReason | None = None
    reconnect_count: int = 0

    @property
    def is_connected(self) -> bool:
        return self.status is ConnectionStatus.CONNECTED


class RobotPose(Timestamped):
    """Position and heading in a named frame. Metres and radians."""

    x_m: float = 0.0
    y_m: float = 0.0
    theta_rad: float = 0.0
    frame: str = "odom"


class RobotMotionState(Timestamped):
    moving: bool = False
    linear_speed_mps: float = 0.0
    angular_speed_rps: float = 0.0
    action_id: str | None = None


class RobotBatteryState(Timestamped):
    percent: int = Field(default=0, ge=0, le=100)
    charging: bool = False
    voltage_v: float | None = None

    @property
    def is_low(self) -> bool:
        return self.percent <= 20 and not self.charging


class RobotSensorState(Timestamped):
    cliff_detected: bool = False
    bump_detected: bool = False
    picked_up: bool = False
    touch_detected: bool = False
    readings: dict[str, float] = Field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        """Any sensor that makes driving unsafe right now."""
        return self.cliff_detected or self.bump_detected or self.picked_up


class RobotAudioState(Timestamped):
    listening: bool = False
    speaking: bool = False
    volume_percent: int = Field(default=50, ge=0, le=100)
    sound_direction_deg: float | None = None


class RobotVisionState(Timestamped):
    camera_active: bool = False
    faces_detected: int = 0
    tracked_target_id: str | None = None
    last_frame_at: datetime | None = None


class RobotExpressionState(Timestamped):
    emotion: str = "neutral"
    intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    display_text: str | None = None
    animation: str | None = None


class RobotActivityState(Timestamped):
    activity: RobotActivity = RobotActivity.IDLE
    detail: str | None = None
    since: datetime = Field(default_factory=utcnow)


class RobotTelemetry(Timestamped):
    """Everything perception knows about the robot right now.

    Every field is optional: a field that has never been reported is ``None``, which is
    different from a field reported as zero.
    """

    pose: RobotPose | None = None
    motion: RobotMotionState | None = None
    battery: RobotBatteryState | None = None
    sensors: RobotSensorState | None = None
    audio: RobotAudioState | None = None
    vision: RobotVisionState | None = None
    expression: RobotExpressionState | None = None
    activity: RobotActivityState | None = None

    def merge(self, patch: RobotTelemetry) -> RobotTelemetry:
        """Overlay the non-``None`` fields of ``patch`` onto this snapshot."""
        updates = {key: value for key, value in patch.__dict__.items() if value is not None and key != "updated_at"}
        if not updates:
            return self
        updates["updated_at"] = patch.updated_at
        return self.model_copy(update=updates)

    def changed_fields(self, previous: RobotTelemetry) -> tuple[str, ...]:
        """Which blocks actually hold a different value than they did.

        The timestamp is excluded from the comparison, and that is the whole point. Every
        block is :class:`Timestamped`, so a device that re-sends the same numbers five
        times a second produces five *unequal* objects; comparing them whole made
        ``changed`` mean "present in the patch" rather than "different", and every
        subscriber — the world model, the metrics fold, anything watching
        :class:`~robot.events.types.BatteryUpdated` — was woken for a battery that had not
        moved.
        """
        names = ("pose", "motion", "battery", "sensors", "audio", "vision", "expression", "activity")
        return tuple(name for name in names if _differs(getattr(self, name), getattr(previous, name)))


def _differs(current: Any, previous: Any) -> bool:
    """Whether two telemetry blocks hold different values, ignoring when they were written."""
    if current is None or previous is None:
        return current is not previous
    if isinstance(current, Timestamped) and isinstance(previous, Timestamped):
        return current.model_dump(exclude={"updated_at"}) != previous.model_dump(exclude={"updated_at"})
    return bool(current != previous)


class RobotState(Timestamped):
    """The complete server-side picture of one robot."""

    robot_id: str
    identity: RobotIdentity
    connection: RobotConnection
    telemetry: RobotTelemetry = Field(default_factory=RobotTelemetry)

    @property
    def capabilities(self) -> RobotCapabilities:
        return self.identity.capabilities

    @property
    def is_connected(self) -> bool:
        return self.connection.is_connected


class DeviceInfo(_Frozen):
    """What the transport and the ``hello`` handshake say about a device.

    The raw material for a :class:`RobotIdentity`: the session layer fills this in from
    headers and the hello payload, and the runtime turns it into identity plus
    connection so those two models have exactly one construction path.
    """

    device_id: str
    session_id: str
    name: str = "robot"
    hardware_model: str = "unknown"
    firmware_version: str = "unknown"
    protocol_version: str = "unknown"
    protocol: str = "nilo"
    transport: str = "websocket"
    remote_address: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)

    @property
    def robot_id(self) -> str:
        return normalize_robot_id(self.device_id)

    @property
    def supports_mcp(self) -> bool:
        return bool(self.features.get("mcp"))

    def identity(self, *, capabilities: RobotCapabilities | None = None, now: datetime | None = None) -> RobotIdentity:
        moment = now or utcnow()
        return RobotIdentity(
            device_id=self.device_id,
            robot_id=self.robot_id,
            name=self.name,
            hardware_model=self.hardware_model,
            firmware_version=self.firmware_version,
            protocol_version=self.protocol_version,
            capabilities=capabilities or RobotCapabilities(),
            connected_at=moment,
            last_seen_at=moment,
        )

    def connection(self, *, now: datetime | None = None) -> RobotConnection:
        moment = now or utcnow()
        return RobotConnection(
            robot_id=self.robot_id,
            device_id=self.device_id,
            session_id=self.session_id,
            protocol=self.protocol,
            transport=self.transport,
            remote_address=self.remote_address,
            connected_at=moment,
            last_seen_at=moment,
            updated_at=moment,
        )
