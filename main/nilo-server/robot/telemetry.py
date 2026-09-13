"""Device telemetry notifications, turned into world state.

The device publishes its hardware as MCP tools; it reports *itself* as MCP notifications
on the same channel — a JSON-RPC message with a ``method`` and no ``id``, so nothing is
waiting for a reply:

    {"type": "mcp", "payload": {"jsonrpc": "2.0",
                                "method": "notifications/battery",
                                "params": {"percent": 41, "charging": false}}}

Without this module those frames reach the inherited handler, which logs the method name
and drops them (``core/providers/tools/device_mcp/mcp_handler.py``, the ``"method" in
payload`` branch), so the server's picture of a robot could only ever be filled in by
polling. Parsing is separated from ingestion on purpose: :func:`parse` is a pure function
over a dict and is tested without a runtime, a socket or an event loop.

The wire vocabulary is the device's, not the domain model's: integer millimetres, degrees
and percentages (docs/robot-simulator.md). Conversion to the metres and radians the world
model stores happens here, in one place, and a malformed field is dropped rather than
raising — a robot that reports one bad number must not lose the rest of its frame.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robot.events.types import MotionCompleted, MotionFailed, RobotEvent
from robot.state.models import (
    RobotActivity,
    RobotActivityState,
    RobotAudioState,
    RobotBatteryState,
    RobotExpressionState,
    RobotMotionState,
    RobotPose,
    RobotSensorState,
    RobotTelemetry,
    RobotVisionState,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle only exists for the type checker
    from robot.runtime import RobotRuntime

logger = logging.getLogger(__name__)

#: Prefix every telemetry notification carries. Anything else is not ours.
NOTIFICATION_PREFIX = "notifications/"

#: Methods whose ``params`` *are* one telemetry slice.
SLICE_METHODS: dict[str, str] = {
    "notifications/pose": "pose",
    "notifications/motion": "motion",
    "notifications/battery": "battery",
    "notifications/sensor": "sensors",
    "notifications/audio": "audio",
    "notifications/vision": "vision",
    "notifications/expression": "expression",
    "notifications/activity": "activity",
}

#: Methods whose ``params`` is a mapping of slice name to slice.
COMPOSITE_METHODS: frozenset[str] = frozenset(
    {"notifications/telemetry", "notifications/motion_completed", "notifications/motion_failed"}
)

TELEMETRY_METHODS: frozenset[str] = frozenset(SLICE_METHODS) | COMPOSITE_METHODS


@dataclass(frozen=True, slots=True)
class ParsedNotification:
    """What one notification means: a telemetry patch, and possibly an event."""

    method: str
    telemetry: RobotTelemetry
    event: RobotEvent | None = None

    @property
    def empty(self) -> bool:
        return self.telemetry.changed_fields(RobotTelemetry(updated_at=self.telemetry.updated_at)) == ()


def is_telemetry(method: str | None) -> bool:
    return isinstance(method, str) and method in TELEMETRY_METHODS


def parse(robot_id: str, method: str, params: Mapping[str, Any] | None) -> ParsedNotification | None:
    """Turn one notification into a telemetry patch. ``None`` for a method we do not own."""
    if method not in TELEMETRY_METHODS:
        return None
    payload: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
    slices: dict[str, Any] = {}
    single = SLICE_METHODS.get(method)
    if single is not None:
        slices[single] = payload
    else:
        for name in ("pose", "motion", "battery", "sensors", "audio", "vision", "expression", "activity"):
            value = payload.get(name)
            if isinstance(value, Mapping):
                slices[name] = value
    built = {name: builder(slices[name]) for name, builder in _BUILDERS.items() if name in slices}
    telemetry = RobotTelemetry(**built)
    event = _motion_event(robot_id, method, payload, telemetry)
    return ParsedNotification(method=method, telemetry=telemetry, event=event)


async def ingest(
    runtime: RobotRuntime, robot_id: str, method: str, params: Mapping[str, Any] | None
) -> bool:
    """Apply one notification to the runtime. Returns whether it was ours to apply.

    Never raises: it runs from the inherited MCP handler, where a robot problem must not
    break a voice session.
    """
    try:
        parsed = parse(robot_id, method, params)
    except Exception as exc:
        logger.warning("robot %s: unparsable %s notification: %s", robot_id, method, exc)
        return False
    if parsed is None:
        return False
    try:
        if not parsed.empty:
            updated = await runtime.update_telemetry(robot_id, parsed.telemetry)
            if updated is None:
                logger.debug("robot %s: telemetry for a robot that is not registered", robot_id)
        if parsed.event is not None:
            await runtime.events.publish(parsed.event)
    except Exception as exc:
        logger.warning("robot %s: failed to apply %s: %s", robot_id, method, exc)
        return False
    return True


def _motion_event(
    robot_id: str, method: str, payload: Mapping[str, Any], telemetry: RobotTelemetry
) -> RobotEvent | None:
    if method not in {"notifications/motion_completed", "notifications/motion_failed"}:
        return None
    action_id = _text(payload.get("action_id"), "")
    if not action_id:
        logger.warning("robot %s: %s without an action_id", robot_id, method)
        return None
    kind = _text(payload.get("kind"), "move")
    if method == "notifications/motion_completed":
        return MotionCompleted(robot_id=robot_id, action_id=action_id, kind=kind, pose=telemetry.pose)
    return MotionFailed(
        robot_id=robot_id,
        action_id=action_id,
        kind=kind,
        reason=_text(payload.get("reason"), "unknown"),
        detail=_text(payload.get("detail"), ""),
    )


# -- slice builders ------------------------------------------------------------------------


def _pose(data: Mapping[str, Any]) -> RobotPose:
    return RobotPose(
        x_m=_number(data.get("x_mm"), 0.0) / 1000.0,
        y_m=_number(data.get("y_mm"), 0.0) / 1000.0,
        theta_rad=math.radians(_number(data.get("yaw_deg"), 0.0)),
        frame=_text(data.get("frame"), "odom"),
    )


def _motion(data: Mapping[str, Any]) -> RobotMotionState:
    action_id = data.get("action_id")
    return RobotMotionState(
        moving=bool(data.get("moving", False)),
        linear_speed_mps=_number(data.get("linear_speed_mmps"), 0.0) / 1000.0,
        angular_speed_rps=math.radians(_number(data.get("angular_speed_mdps"), 0.0) / 1000.0),
        action_id=str(action_id) if isinstance(action_id, str) and action_id else None,
    )


def _battery(data: Mapping[str, Any]) -> RobotBatteryState:
    millivolts = data.get("battery_mv")
    return RobotBatteryState(
        percent=max(0, min(100, int(_number(data.get("percent"), 0.0)))),
        charging=bool(data.get("charging", False)),
        voltage_v=_number(millivolts, 0.0) / 1000.0 if millivolts is not None else None,
    )


def _sensors(data: Mapping[str, Any]) -> RobotSensorState:
    raw = data.get("readings")
    readings: dict[str, float] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                readings[str(key)] = float(value)
    return RobotSensorState(
        cliff_detected=bool(data.get("cliff_detected", False)),
        bump_detected=bool(data.get("bump_detected", False)),
        picked_up=bool(data.get("picked_up", False)),
        touch_detected=bool(data.get("touch_detected", False)),
        readings=readings,
    )


def _audio(data: Mapping[str, Any]) -> RobotAudioState:
    direction = data.get("sound_direction_deg")
    return RobotAudioState(
        listening=bool(data.get("listening", False)),
        speaking=bool(data.get("speaking", False)),
        volume_percent=max(0, min(100, int(_number(data.get("volume_pct"), 50.0)))),
        sound_direction_deg=_number(direction, 0.0) if direction is not None else None,
    )


def _vision(data: Mapping[str, Any]) -> RobotVisionState:
    target = data.get("tracked_target_id")
    return RobotVisionState(
        camera_active=bool(data.get("camera_active", False)),
        faces_detected=max(0, int(_number(data.get("faces_detected"), 0.0))),
        tracked_target_id=str(target) if isinstance(target, str) and target else None,
    )


def _expression(data: Mapping[str, Any]) -> RobotExpressionState:
    animation = data.get("animation")
    return RobotExpressionState(
        emotion=_text(data.get("emotion"), "neutral"),
        intensity=max(0.0, min(1.0, _number(data.get("intensity_pct"), 0.0) / 100.0)),
        animation=str(animation) if isinstance(animation, str) and animation else None,
    )


def _activity(data: Mapping[str, Any]) -> RobotActivityState:
    name = _text(data.get("activity"), RobotActivity.IDLE.value)
    try:
        activity = RobotActivity(name)
    except ValueError:
        logger.debug("unknown activity %r from a device; recording it as idle", name)
        activity = RobotActivity.IDLE
    detail = data.get("detail")
    return RobotActivityState(activity=activity, detail=str(detail) if isinstance(detail, str) and detail else None)


_BUILDERS: dict[str, Any] = {
    "pose": _pose,
    "motion": _motion,
    "battery": _battery,
    "sensors": _sensors,
    "audio": _audio,
    "vision": _vision,
    "expression": _expression,
    "activity": _activity,
}


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default
