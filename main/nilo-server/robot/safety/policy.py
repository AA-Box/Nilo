"""The deterministic safety policy: the one place that decides what may be dispatched.

:meth:`RobotSafetyPolicy.evaluate` is a **pure function** of its arguments. It reads no
clock, opens no socket, holds no counter and consults no global. Everything time-dependent
— the current instant, how long the request has waited, how many motions were admitted
recently — arrives in a :class:`SafetyContext`, which is what makes "the same request in
the same world is always decided the same way" a property a test can assert rather than a
hope.

What this layer is, exactly
---------------------------

It is a **policy filter, not a guarantee**, and the distinction is not a formality. The
process this runs in can be killed mid-motion (``core/connection.py`` calls ``os._exit(0)``
from a daemon thread on a restart message), its event loop can stall for an unbounded time
(``core/utils/gc_manager.py`` walks ``gc.get_objects()`` twice every 300 s, holding the
GIL), and a command the device has already accepted cannot be cancelled from here. Cliff
protection, the motor watchdog, acceleration bounds and the local emergency stop are
implemented **again, independently, in firmware**, and the firmware copy is the one that
holds when this process is gone. See docs/safety-model.md.

Rejections are typed and they are *rejections*, not clamps. A request beyond a configured
limit comes back as :class:`~robot.state.actions.RejectionReason.DISTANCE_LIMIT_EXCEEDED`
with the number that failed, and the caller is told. Silently clamping to the maximum hides
the bug that produced a 40-metre "move" until the day the ceiling is wrong.

No layer above may weaken it. Personality and behaviour choose *which* action to propose;
they do not get a vote on whether it is permitted, and this module imports nothing from
them — enforced by ``robot/.ruff.toml`` and asserted by ``tests/robot/test_layering.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from robot.safety.estop import EmergencyStop
from robot.safety.limits import SafetyLimits
from robot.state.actions import (
    ActionSource,
    ActionType,
    RejectionReason,
    Resource,
)
from robot.state.models import RobotState, utcnow

logger = logging.getLogger(__name__)


class Request(Protocol):
    """What the policy needs from an action spec.

    A structural type rather than an import of :class:`robot.actions.model.ActionSpec`:
    ``robot/safety`` sits *below* ``robot/actions`` in the layering (robot-architecture
    Sect. 7), so safety may not import it. The protocol is the seam.
    """

    @property
    def action_type(self) -> ActionType: ...

    @property
    def resources(self) -> tuple[Resource, ...]: ...

    @property
    def tool_name(self) -> str: ...

    def arguments(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SafetyContext:
    """Everything time- or world-dependent the decision may consult.

    Passed in rather than read, so :meth:`RobotSafetyPolicy.evaluate` stays deterministic.
    """

    robot_id: str
    #: The world model entry for this robot, or ``None`` when it is not registered.
    state: RobotState | None
    now: datetime
    #: How long the request has existed. Compared against the TTL.
    age_s: float = 0.0
    #: Motion actions admitted for this robot inside the rate window. The executor owns
    #: the window; the policy only compares.
    recent_motions: int = 0
    #: True on the second evaluation, immediately before the device call. The world may
    #: have changed while the action sat in the queue, which is the whole point of
    #: checking twice (docs/robot-architecture.md Sect. 5).
    at_dispatch: bool = False


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Allowed, or rejected with a typed reason. There is no third outcome and no clamp."""

    allowed: bool
    reason: RejectionReason | None = None
    message: str = ""

    @staticmethod
    def allow() -> SafetyDecision:
        return SafetyDecision(allowed=True)

    @staticmethod
    def deny(reason: RejectionReason, message: str = "") -> SafetyDecision:
        return SafetyDecision(allowed=False, reason=reason, message=message or reason.value)

    def __bool__(self) -> bool:
        return self.allowed


class RobotSafetyPolicy:
    """Judges every action request, identically whoever asked.

    Constructed per runtime with its own limits and its own emergency-stop latch, so one
    process can host two robots with different chassis and different stop states.
    """

    def __init__(self, limits: SafetyLimits | None = None, estop: EmergencyStop | None = None) -> None:
        self.limits = limits or SafetyLimits()
        self.estop = estop or EmergencyStop()

    # -- the decision ---------------------------------------------------------------------

    def evaluate(self, request: Request, source: ActionSource, context: SafetyContext) -> SafetyDecision:
        """Decide one request. Deterministic: same request, same context, same answer.

        The checks run in a fixed order, cheapest and most categorical first, so the
        reason a caller is given is the *first* thing that was wrong rather than an
        arbitrary one of several.
        """
        is_stop = request.action_type is ActionType.STOP
        # Rule 1 of the emergency stop: stop itself is never refused, and the safety layer
        # can always command one. Everything else is refused until the latch is cleared.
        if self.estop.engaged(context.robot_id) and not (is_stop or source is ActionSource.SAFETY):
            latched = self.estop.state(context.robot_id)
            return SafetyDecision.deny(
                RejectionReason.EMERGENCY_STOP_ENGAGED,
                f"emergency stop engaged: {latched.reason if latched else 'unknown reason'}",
            )

        link = self._check_link(request, context)
        if link is not None:
            return link

        if context.age_s > self.limits.action_ttl_s:
            return SafetyDecision.deny(
                RejectionReason.TTL_EXPIRED,
                f"the request is {context.age_s:.1f}s old, the TTL is {self.limits.action_ttl_s:.1f}s",
            )

        # A stop that got this far is dispatchable. It is not gated on sensors, battery,
        # limits or rate: a robot that will not stop because a cliff sensor is asserted is
        # exactly the wrong failure.
        if is_stop:
            return SafetyDecision.allow()

        state = context.state
        assert state is not None  # _check_link rejects a missing state before this point

        heartbeat_age = state.connection.age_seconds(context.now)
        if heartbeat_age > self.limits.heartbeat_timeout_s:
            return SafetyDecision.deny(
                RejectionReason.HEARTBEAT_EXPIRED,
                f"no traffic from the robot for {heartbeat_age:.1f}s "
                f"(budget {self.limits.heartbeat_timeout_s:.1f}s)",
            )

        if request.action_type in _MOTION_TYPES:
            motion = self._check_motion(request, context, state)
            if motion is not None:
                return motion

        return self._check_parameters(request)

    # -- checks ---------------------------------------------------------------------------

    def _check_link(self, request: Request, context: SafetyContext) -> SafetyDecision | None:
        """Is there a robot, is it connected, and does it publish the tool we would call?

        The capability check is an **allow-list**: the tool must appear in what the device
        actually advertised. An action whose tool is absent is rejected, never forwarded
        on the assumption that firmware will understand it.
        """
        state = context.state
        if state is None:
            return SafetyDecision.deny(
                RejectionReason.ROBOT_UNKNOWN, f"robot {context.robot_id} is not registered"
            )
        if not state.connection.is_connected:
            return SafetyDecision.deny(
                RejectionReason.DEVICE_DISCONNECTED,
                f"robot {context.robot_id} is {state.connection.status.value}",
            )
        if not state.capabilities.has_tool(request.tool_name):
            return SafetyDecision.deny(
                RejectionReason.UNSUPPORTED_ACTION,
                f"robot {context.robot_id} does not publish {request.tool_name!r}",
            )
        return None

    def _check_motion(
        self, request: Request, context: SafetyContext, state: RobotState
    ) -> SafetyDecision | None:
        """Sensor and battery gating for anything that drives. Order: fatal first."""
        if context.recent_motions >= self.limits.max_motion_per_window:
            return SafetyDecision.deny(
                RejectionReason.RATE_LIMIT_EXCEEDED,
                f"{context.recent_motions} motion commands in the last "
                f"{self.limits.rate_window_s:.0f}s (limit {self.limits.max_motion_per_window})",
            )

        sensors = state.telemetry.sensors
        if sensors is None:
            return SafetyDecision.deny(
                RejectionReason.SENSOR_DATA_MISSING,
                "the robot has never reported its sensors; motion needs a sensor picture",
            )
        age = sensors.age_seconds(context.now)
        if age > self.limits.max_sensor_age_s:
            return SafetyDecision.deny(
                RejectionReason.SENSOR_DATA_STALE,
                f"sensor data is {age:.1f}s old, the budget is {self.limits.max_sensor_age_s:.1f}s",
            )

        if sensors.cliff_detected:
            return SafetyDecision.deny(
                RejectionReason.CLIFF_HAZARD, "a cliff sensor is asserted; no motion is dispatched"
            )
        if sensors.picked_up:
            return SafetyDecision.deny(
                RejectionReason.ROBOT_LIFTED, "the robot reports it is off the ground"
            )

        battery = state.telemetry.battery
        if battery is not None and not battery.charging and battery.percent < self.limits.min_battery_percent:
            return SafetyDecision.deny(
                RejectionReason.BATTERY_TOO_LOW,
                f"battery at {battery.percent}%, the motion floor is {self.limits.min_battery_percent}%",
            )

        if not _drives_forward(request):
            return None

        # Forward-only checks. Reversing away from a bump or an obstacle is how a robot
        # recovers, so refusing it would strand the machine against the thing it hit.
        if sensors.bump_detected:
            return SafetyDecision.deny(
                RejectionReason.BUMP_HAZARD, "the bumper is pressed; forward motion is refused"
            )
        front_mm = sensors.readings.get("front_mm")
        if front_mm is not None and front_mm < self.limits.min_obstacle_distance_mm:
            return SafetyDecision.deny(
                RejectionReason.OBSTACLE_TOO_CLOSE,
                f"{int(front_mm)} mm ahead, the floor is {self.limits.min_obstacle_distance_mm} mm",
            )
        return None

    def _check_parameters(self, request: Request) -> SafetyDecision:
        """Bounds, per action type. Reject with the number that failed, never clamp."""
        arguments = request.arguments()
        limits = self.limits
        kind = request.action_type

        if kind is ActionType.MOVE:
            distance = int(arguments["distance_mm"])
            if distance == 0:
                return SafetyDecision.deny(
                    RejectionReason.PARAMETER_OUT_OF_RANGE, "a move of zero distance does not terminate"
                )
            if abs(distance) > limits.max_distance_mm:
                return SafetyDecision.deny(
                    RejectionReason.DISTANCE_LIMIT_EXCEEDED,
                    f"{distance} mm exceeds the {limits.max_distance_mm} mm ceiling per command",
                )
            return self._check_speed(int(arguments["speed_mmps"]), limits.max_speed_mmps, "mm/s")

        if kind is ActionType.TURN:
            angle = int(arguments["angle_deg"])
            if angle == 0:
                return SafetyDecision.deny(
                    RejectionReason.PARAMETER_OUT_OF_RANGE, "a turn of zero degrees does not terminate"
                )
            if abs(angle) > limits.max_angle_deg:
                return SafetyDecision.deny(
                    RejectionReason.ANGLE_LIMIT_EXCEEDED,
                    f"{angle}° exceeds the {limits.max_angle_deg}° ceiling per command",
                )
            return self._check_speed(int(arguments["speed_dps"]), limits.max_turn_speed_dps, "°/s")

        if kind is ActionType.FOLLOW_TARGET:
            return self._check_duration(
                int(arguments["duration_ms"]), limits.max_follow_duration_ms, "follow"
            )

        if kind is ActionType.ANIMATION:
            return self._check_duration(
                int(arguments["duration_ms"]), limits.max_animation_duration_ms, "animation"
            )

        return self._check_percentages(arguments)

    @staticmethod
    def _check_speed(speed: int, ceiling: int, unit: str) -> SafetyDecision:
        if speed <= 0:
            return SafetyDecision.deny(
                RejectionReason.PARAMETER_OUT_OF_RANGE, f"a speed of {speed} {unit} does not terminate"
            )
        if speed > ceiling:
            return SafetyDecision.deny(
                RejectionReason.SPEED_LIMIT_EXCEEDED, f"{speed} {unit} exceeds the {ceiling} {unit} ceiling"
            )
        return SafetyDecision.allow()

    @staticmethod
    def _check_duration(duration_ms: int, ceiling_ms: int, what: str) -> SafetyDecision:
        if duration_ms <= 0:
            return SafetyDecision.deny(
                RejectionReason.PARAMETER_OUT_OF_RANGE, f"a {what} of {duration_ms} ms does not terminate"
            )
        if duration_ms > ceiling_ms:
            return SafetyDecision.deny(
                RejectionReason.DURATION_LIMIT_EXCEEDED,
                f"a {what} of {duration_ms} ms exceeds the {ceiling_ms} ms ceiling",
            )
        return SafetyDecision.allow()

    @staticmethod
    def _check_percentages(arguments: dict[str, Any]) -> SafetyDecision:
        """Every ``*_pct`` argument is 0-100 by definition of the unit in its name."""
        for name, value in arguments.items():
            if not name.endswith("_pct") or not isinstance(value, int):
                continue
            if not 0 <= value <= 100:
                return SafetyDecision.deny(
                    RejectionReason.PARAMETER_OUT_OF_RANGE, f"{name}={value} is outside 0-100"
                )
        return SafetyDecision.allow()

    # -- supervisory checks, used by the watchdog rather than by admission ----------------

    def link_is_live(self, state: RobotState | None, now: datetime | None = None) -> bool:
        """Whether a robot's session is connected and its heartbeat is inside the budget.

        What the supervisor asks about an action that is already running: a request that
        was admitted a second ago must still be stoppable, and a link that has gone quiet
        means the world model is a guess rather than a picture.
        """
        if state is None or not state.connection.is_connected:
            return False
        return state.connection.age_seconds(now or utcnow()) <= self.limits.heartbeat_timeout_s

    def hazard_for(self, state: RobotState | None, now: datetime | None = None) -> RejectionReason | None:
        """The hazard that should stop motion already in flight, or ``None``.

        Deliberately narrower than :meth:`evaluate`: a robot that is *already* moving is
        stopped for a cliff, a pickup or stale sensors, not for a rate limit or a speed
        ceiling it was admitted under.
        """
        if state is None:
            return RejectionReason.ROBOT_UNKNOWN
        if not state.connection.is_connected:
            return RejectionReason.DEVICE_DISCONNECTED
        moment = now or utcnow()
        if state.connection.age_seconds(moment) > self.limits.heartbeat_timeout_s:
            return RejectionReason.HEARTBEAT_EXPIRED
        sensors = state.telemetry.sensors
        if sensors is None:
            return None
        if sensors.age_seconds(moment) > self.limits.max_sensor_age_s:
            return RejectionReason.SENSOR_DATA_STALE
        if sensors.cliff_detected:
            return RejectionReason.CLIFF_HAZARD
        if sensors.picked_up:
            return RejectionReason.ROBOT_LIFTED
        return None

    def clamp_timeout(self, timeout_s: float) -> float:
        """Hold a caller-supplied completion budget inside the configured ceiling.

        The one place the policy adjusts a value instead of rejecting it, and it adjusts
        in the safe direction only: a shorter timeout makes the watchdog fire *sooner*. A
        caller that asks for a ten-minute move is not granted ten minutes of unsupervised
        motion.
        """
        if timeout_s <= 0:
            return self.limits.default_timeout_s
        if timeout_s > self.limits.max_timeout_s:
            logger.info(
                "action timeout %.1fs exceeds the %.1fs ceiling; supervising at the ceiling instead",
                timeout_s,
                self.limits.max_timeout_s,
            )
            return self.limits.max_timeout_s
        return timeout_s


_MOTION_TYPES = frozenset({ActionType.MOVE, ActionType.TURN, ActionType.FOLLOW_TARGET})


def _drives_forward(request: Request) -> bool:
    """Whether this request would carry the robot towards whatever is in front of it."""
    if request.action_type is ActionType.FOLLOW_TARGET:
        return True
    if request.action_type is not ActionType.MOVE:
        return False
    return int(request.arguments()["distance_mm"]) > 0


__all__ = ["Request", "RobotSafetyPolicy", "SafetyContext", "SafetyDecision"]
